"""Installed queries as agent tools: the description format (stage 1), the
stored tool list and admin registration (stage 2)."""

import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from fastapi import HTTPException

from tools import gsql_query_tools as qt

BLOCK = (
    "Owned by the data team.\n"
    "[GRAPHRAG_TOOL]\n"
    "Returns: the number of orders placed in each month\n"
    "Use for: questions about order volume over time\n"
    "Do not use for: revenue amounts\n"
    "Output: month (YYYY-MM), orders (count)\n"
    "[/GRAPHRAG_TOOL]"
)


class FakeConn:
    """getQueryDescription / getQueryMetadata / runInstalledQuery as TigerGraph
    returns them."""

    def __init__(self, queries=None, installed=None):
        self.queries = queries or {}
        self.installed = installed if installed is not None else set(self.queries)
        self.calls = []
        self.graphname = "G"

    def getInstalledQueries(self, fmt="list"):
        self.calls.append("getInstalledQueries")
        return sorted(self.installed)

    def getQueryDescription(self, names):
        self.calls.append(("getQueryDescription", tuple(names)))
        out = []
        for n in names:
            q = self.queries.get(n, {})
            row = {"queryName": n, "parameters": q.get("described", [])}
            if q.get("description") is not None:
                row["description"] = q["description"]
            out.append(row)
        return out

    def getQueryMetadata(self, name):
        self.calls.append(("getQueryMetadata", name))
        return {"input": self.queries.get(name, {}).get("typed", [])}

    def runInstalledQuery(self, name, params=None):
        self.calls.append(("runInstalledQuery", name, params))
        if name not in self.installed:
            raise Exception(f"The query '{name}' does not exist on graph G.")
        return [{"rows": 3}]


ORDERS = {
    "description": BLOCK,
    "described": [
        {"paramName": "since", "description": "First day, YYYY-MM-DD, such as 2024-01-31"},
        {"paramName": "top_k", "defaultValue": "5l", "description": "How many months"},
        {"paramName": "vec", "description": "An embedding"},
        {"paramName": "v", "description": "Any vertex"},
        {"paramName": "doc", "description": "A document id"},
        {"paramName": "ratio", "defaultValue": "1.5"},
        {"paramName": "verbose", "defaultValue": "false"},
    ],
    "typed": [
        {"since": "datetime"}, {"top_k": "uint"}, {"vec": "LIST<float>"},
        {"v": "vertex"}, {"doc": "vertex<Document>"}, {"ratio": "float"}, {"verbose": "bool"},
    ],
}


class TestDescriptionFormat(unittest.TestCase):
    def test_block_is_parsed_and_other_text_kept(self):
        fields, other, problem = qt.parse_tool_block(BLOCK)
        self.assertIsNone(problem)
        self.assertEqual(other, "Owned by the data team.")
        self.assertEqual(fields["returns"], "the number of orders placed in each month")
        self.assertEqual(fields["output"], "month (YYYY-MM), orders (count)")

    def test_value_may_continue_on_the_next_line(self):
        fields, _, _ = qt.parse_tool_block(
            "[GRAPHRAG_TOOL]\nReturns: the orders\nof each month\nUse for: volume\n[/GRAPHRAG_TOOL]")
        self.assertEqual(fields["returns"], "the orders of each month")

    def test_skip_reasons(self):
        cases = {
            "": "no tool block",
            "plain text only": "no tool block",
            "[GRAPHRAG_TOOL]": "no closing",
            "[GRAPHRAG_TOOL][/GRAPHRAG_TOOL]": '"Returns:"',
            "[GRAPHRAG_TOOL]\nReturns: x\n[/GRAPHRAG_TOOL]": '"Use for:"',
            "[GRAPHRAG_TOOL]\nhello\nReturns: x\nUse for: y\n[/GRAPHRAG_TOOL]": "unexpected text",
        }
        for text, reason in cases.items():
            fields, _, problem = qt.parse_tool_block(text)
            self.assertIsNone(fields, text)
            self.assertIn(reason, problem, text)

    def test_marker_alone_is_not_a_tool(self):
        state = qt.evaluate_query("q", {"description": "[GRAPHRAG_TOOL]", "parameters": []}, [])
        self.assertFalse(state["ok"])

    def test_write_keeps_other_text_and_remove_drops_only_the_block(self):
        fields, _, _ = qt.parse_tool_block(BLOCK)
        new = qt.with_tool_block("Owned by the data team.\n[GRAPHRAG_TOOL]\nReturns: old\n[/GRAPHRAG_TOOL]",
                                 {**fields, "returns": "new"})
        self.assertTrue(new.startswith("Owned by the data team.\n[GRAPHRAG_TOOL]\nReturns: new"))
        self.assertEqual(qt.without_tool_block(BLOCK), "Owned by the data team.")
        self.assertEqual(qt.without_tool_block("[GRAPHRAG_TOOL]\nReturns: x\nUse for: y\n[/GRAPHRAG_TOOL]"), "")

    def test_parameter_without_default_needs_a_description(self):
        state = qt.evaluate_query("q", {
            "description": BLOCK,
            "parameters": [{"paramName": "since"}, {"paramName": "k", "defaultValue": "3l"}],
        }, [{"since": "datetime"}, {"k": "int"}])
        self.assertFalse(state["ok"])
        self.assertEqual(state["reason"], "no description for parameter since")


class TestSchema(unittest.TestCase):
    def setUp(self):
        self.params = qt.build_params(ORDERS["described"], ORDERS["typed"])
        self.schema = qt.args_schema(self.params)
        self.props = self.schema["properties"]

    def test_descriptions_and_defaults_come_from_tigergraph(self):
        self.assertIn("YYYY-MM-DD", self.props["since"]["description"])
        self.assertEqual(self.props["top_k"]["default"], 5)
        self.assertEqual(self.props["ratio"]["default"], 1.5)
        self.assertIs(self.props["verbose"]["default"], False)

    def test_only_parameters_without_a_default_are_required(self):
        self.assertEqual(sorted(self.schema["required"]), ["doc", "since", "v", "vec"])

    def test_types(self):
        self.assertEqual(self.props["vec"]["items"], {"type": "number"})
        self.assertEqual(self.props["top_k"]["minimum"], 0)
        self.assertEqual(self.props["v"]["type"], "object")
        self.assertEqual(self.props["v"]["required"], ["id", "type"])
        self.assertEqual(self.props["doc"]["type"], "string")
        self.assertIn("Document", self.props["doc"]["description"])

    def test_untyped_vertex_is_passed_with_its_type(self):
        args = qt._to_query_args(self.params, {"v": {"id": "p1", "type": "Person"},
                                               "doc": "d1", "top_k": None})
        self.assertEqual(args, {"v": ("p1", "Person"), "doc": "d1"})


class _StoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cwd = os.getcwd()
        os.chdir(self.tmp.name)
        qt._cache.clear()

    def tearDown(self):
        os.chdir(self.cwd)
        self.tmp.cleanup()


class TestStoredList(_StoreTest):
    def test_register_saves_the_tool_and_agents_load_it_without_tigergraph(self):
        conn = FakeConn({"orders": ORDERS})
        result = qt.register(conn, "G", ["orders"], "alice")
        self.assertEqual(result, {"registered": ["orders"], "failed": []})
        with open(os.path.join("configs", "graph_configs", "G", "query_tools.json")) as f:
            saved = json.load(f)["tools"][0]
        self.assertEqual(saved["query"], "orders")
        self.assertEqual(saved["registered_by"], "alice")
        self.assertIn("registered_at", saved)
        self.assertEqual([p["name"] for p in saved["params"]][:2], ["since", "top_k"])

        tools = qt.registered_query_tools("G")
        self.assertEqual(list(tools), ["graphrag__gsql__orders"])
        self.assertIn("Returns the number of orders", tools["graphrag__gsql__orders"].description)
        self.assertEqual(tools["graphrag__gsql__orders"].args_schema_json["properties"]["top_k"]["default"], 5)

    def test_register_refuses_a_query_without_a_valid_block(self):
        conn = FakeConn({"plain": {"description": "[GRAPHRAG_TOOL]", "described": [], "typed": []}})
        result = qt.register(conn, "G", ["plain", "gone"], "alice")
        self.assertEqual(result["registered"], [])
        reasons = {f["name"]: f["reason"] for f in result["failed"]}
        self.assertIn("no closing", reasons["plain"])
        self.assertEqual(reasons["gone"], "the query is not installed")
        self.assertEqual(qt.load_registered("G"), {})

    def test_unregister_removes_only_the_stored_tool(self):
        qt.register(FakeConn({"orders": ORDERS}), "G", ["orders"], "alice")
        self.assertEqual(qt.unregister("G", ["orders", "other"]), ["orders"])
        self.assertEqual(qt.registered_query_tools("G"), {})

    def test_discover_reads_descriptions_once_and_marks_changes(self):
        queries = {"orders": dict(ORDERS), "internal": {"description": None}}
        conn = FakeConn(queries)
        qt.register(conn, "G", ["orders"], "alice")
        conn.calls.clear()
        result = qt.discover(conn, "G", qt.load_registered("G"))
        desc_calls = [c for c in conn.calls if c[0] == "getQueryDescription"]
        self.assertEqual(len(desc_calls), 1)
        self.assertEqual(result["matching"][0]["status"], "registered")
        self.assertEqual(result["skipped"], [{"name": "internal", "ok": False, "reason": "no tool block"}])

        queries["orders"] = {**ORDERS, "description": BLOCK.replace("each month", "each week")}
        result = qt.discover(conn, "G", qt.load_registered("G"))
        self.assertEqual(result["matching"][0]["status"], "changed")

        conn.installed = {"internal"}
        result = qt.discover(conn, "G", qt.load_registered("G"))
        self.assertEqual(result["missing"], [{"name": "orders", "reason": "the query is not installed"}])

    def test_running_a_dropped_query_returns_a_clear_error(self):
        conn = FakeConn({"orders": ORDERS})
        qt.register(conn, "G", ["orders"], "alice")
        conn.installed = set()
        tool = qt.registered_query_tools("G")["graphrag__gsql__orders"]
        ctx = SimpleNamespace(conn=conn, emit=lambda msg: None)
        out = tool.fn(ctx, since="2024-01-01", vec=[0.1], v={"id": "1", "type": "P"}, doc="d")
        self.assertFalse(out["ok"])
        self.assertIn("no longer installed", out["summary"])

    def test_tool_runs_with_the_callers_connection(self):
        qt.register(FakeConn({"orders": ORDERS}), "G", ["orders"], "alice")
        user_conn = FakeConn({"orders": ORDERS})
        tool = qt.registered_query_tools("G")["graphrag__gsql__orders"]
        out = tool.fn(SimpleNamespace(conn=user_conn, emit=lambda m: None),
                      since="2024-01-01", vec=[0.1], v={"id": "1", "type": "P"}, doc="d")
        self.assertTrue(out["ok"])
        self.assertEqual(user_conn.calls[-1][0], "runInstalledQuery")


class TestLegacyAgentUsesTheStoredList(_StoreTest):
    def test_no_tigergraph_call_to_find_tools(self):
        qt.register(FakeConn({"orders": ORDERS}), "G", ["orders"], "alice")
        conn = FakeConn({"orders": ORDERS})
        with mock.patch.object(qt, "read_descriptions", side_effect=AssertionError("TG call")):
            tools = qt.registered_query_tools("G")
        self.assertIn("graphrag__gsql__orders", tools)
        self.assertEqual(conn.calls, [])
        import inspect
        from agent import agentic_agent
        src = inspect.getsource(agentic_agent)
        self.assertIn("registered_query_tools(self.conn.graphname)", src)
        self.assertNotIn("discover_tagged_query_tools", src)


class TestAdminOnly(unittest.TestCase):
    """Register, unregister, discover and the description writes need a graph admin."""

    def setUp(self):
        from routers import ui
        self.ui = ui
        self.creds = (["public"], SimpleNamespace(username="bob", password="x"))

    def _call(self, fn, **kw):
        with mock.patch.object(self.ui, "_get_user_role_details",
                               return_value=(["public"], {"G": ["querywriter"]}, None)):
            with self.assertRaises(HTTPException) as cm:
                fn(graphname="G", creds=self.creds, **kw)
        return cm.exception

    def test_non_admin_is_refused(self):
        for fn, kw in [
            (self.ui.ui_register_query_tools, {"payload": {"names": ["q"]}}),
            (self.ui.ui_unregister_query_tools, {"payload": {"names": ["q"]}}),
            (self.ui.ui_discover_query_tools, {}),
            (self.ui.ui_save_query_description, {"payload": {"name": "q", "returns": "x", "use_for": "y"}}),
            (self.ui.ui_remove_query_description, {"payload": {"name": "q"}}),
        ]:
            exc = self._call(fn, **kw)
            self.assertEqual(exc.status_code, 403, fn.__name__)
            self.assertIn("manage its query tools", exc.detail)

    def test_graph_admin_passes_the_role_check(self):
        with mock.patch.object(self.ui, "_get_user_role_details",
                               return_value=(["public"], {"G": ["admin"]}, None)):
            self.ui._require_query_tool_admin(self.creds[1], "G")


if __name__ == "__main__":
    unittest.main()
