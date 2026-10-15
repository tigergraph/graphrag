"""The structured query tool reports the query it ran, and an empty query
result counts as no rows (GML-2327)."""
import json
import types
import unittest
from unittest import mock

try:
    from tools import graphrag_tools as gt
    from agent import agentic_executor as ex
except ImportError as exc:  # needs the app environment
    raise unittest.SkipTest(f"app environment unavailable: {exc}")


class TestResultIsEmpty(unittest.TestCase):
    def test_empty_results(self):
        for value in (None, "", "  ", "[]", "{}", "null", '{"VS_XXX": []}',
                      {"VS_XXX": []}, [{"VS_XXX": []}], [], {}):
            self.assertTrue(gt._result_is_empty(value), value)

    def test_results_with_rows(self):
        for value in ("0", 0, '{"count": 0}', [{"v_id": "x"}], '[{"v_id": "x"}]',
                      {"VS_XXX": [{"v_id": "x"}]}, "not json text"):
            self.assertFalse(gt._result_is_empty(value), value)


_FUNC = "getVertexCount('Document')"
_CYPHER = "MATCH (d:Document) RETURN d"


def _ctx(gen_result, use_cypher=False, cypher_runs=()):
    mapping = types.SimpleNamespace(
        target_vertex_types=["Document"], target_vertex_attributes={}, target_vertex_ids={},
        target_edge_types=[], target_edge_attributes={},
    )
    gen_func = mock.Mock()
    if isinstance(gen_result, BaseException) and not isinstance(gen_result, ValueError):
        gen_func._run.side_effect = gen_result
    else:
        gen_func._run.return_value = gen_result
    cypher_gen = mock.Mock()
    cypher_gen._run.side_effect = [c for c, _ in cypher_runs] or [_CYPHER]
    conn = mock.Mock()
    # runInterpretedQuery returns result blocks, or raises with TigerGraph's message.
    conn.runInterpretedQuery.side_effect = [r for _, r in cypher_runs] or [[]]
    return types.SimpleNamespace(
        emit=lambda *a, **k: None, conversation=[], use_cypher=use_cypher,
        mq2s=mock.Mock(_run=mock.Mock(return_value=mapping)),
        gen_func=gen_func, cypher_gen=cypher_gen, conn=conn,
    )


def _gsql(results):
    """What runInterpretedQuery returns: the query's result blocks."""
    return results


class TestStructuralRetrieveQuery(unittest.TestCase):
    def test_rows_report_the_query(self):
        out = gt.structural_retrieve(_ctx({"function_call": _FUNC, "result": "4"}), "q")
        self.assertTrue(out["ok"])
        self.assertEqual(out["query"], _FUNC)
        self.assertNotIn("attempts", out)

    def test_empty_rows_are_no_rows_and_keep_the_query(self):
        out = gt.structural_retrieve(_ctx({"function_call": _FUNC, "result": "[]"}), "q")
        self.assertFalse(out["ok"])
        self.assertEqual(out["attempts"], [{"query": _FUNC, "outcome": "no rows"}])

    def test_generation_error_is_recorded(self):
        out = gt.structural_retrieve(_ctx(RuntimeError("bad call")), "q")
        self.assertFalse(out["ok"])
        self.assertEqual(out["attempts"], [{"error": "bad call"}])

    def test_cypher_rows_report_the_query_and_earlier_attempts(self):
        ctx = _ctx({"function_call": _FUNC, "result": "[]"}, use_cypher=True,
                   cypher_runs=[("BAD", RuntimeError("syntax error")), (_CYPHER, _gsql([{"VS": [{"v_id": "d1"}]}]))])
        out = gt.structural_retrieve(ctx, "q")
        self.assertTrue(out["ok"])
        self.assertEqual(out["query"], _CYPHER)
        self.assertEqual([a.get("query") for a in out["attempts"]], [_FUNC, "BAD"])
        self.assertIn("error", out["attempts"][1])

    def test_cypher_no_rows_is_not_retried(self):
        ctx = _ctx({"function_call": _FUNC, "result": "[]"}, use_cypher=True,
                   cypher_runs=[(_CYPHER, _gsql([{"VS_XXX": []}])), ("UNUSED", "")])
        out = gt.structural_retrieve(ctx, "q")
        self.assertFalse(out["ok"])
        self.assertEqual(ctx.cypher_gen._run.call_count, 1)
        self.assertEqual(out["attempts"][-1], {"query": _CYPHER, "outcome": "no rows"})


class TestEmptyFunctionRegistry(unittest.TestCase):
    """Without registered functions, no function call is generated."""

    def test_structural_tool_records_the_stop_and_runs_cypher(self):
        from tools.validation_utils import NoRegisteredFunctionsException
        ctx = _ctx(NoRegisteredFunctionsException("No pyTigerGraph functions are registered."),
                   use_cypher=True, cypher_runs=[(_CYPHER, _gsql([{"VS": [{"v_id": "d1"}]}]))])
        out = gt.structural_retrieve(ctx, "q")
        self.assertTrue(out["ok"])
        self.assertEqual(out["query"], _CYPHER)
        self.assertEqual(out["attempts"], [{"error": "No pyTigerGraph functions are registered."}])

    def _gen(self, registered, llm=None):
        from tools.generate_function import GenerateFunction
        store = mock.Mock()
        store.list_registered_documents.return_value = registered
        return GenerateFunction(mock.Mock(), llm or mock.Mock(), mock.Mock(), store), store

    def test_generator_stops_before_any_model_call(self):
        from tools.validation_utils import NoRegisteredFunctionsException
        llm = mock.Mock()
        gen, store = self._gen([], llm)
        with self.assertRaises(NoRegisteredFunctionsException):
            gen._run("q", ["Document"], None, None, None, None)
        llm.invoke_with_parser.assert_not_called()
        store.retrieve_similar.assert_not_called()

    def test_mapping_without_edges_does_not_fail(self):
        """target_edge_types=None used to raise 'NoneType' object is not iterable."""
        from langchain_core.documents import Document
        llm = mock.Mock(generate_function_prompt="{question}", query_guidance_block="")
        llm.invoke_with_parser.return_value = types.SimpleNamespace(
            connection_func_call="getVertexCount('Document')", func_call_reasoning="count")
        gen, store = self._gen([{"function_header": "getVertexCount"}], llm)
        store.retrieve_similar.return_value = [Document(page_content="getVertexCount docs")]
        gen.conn.getVertexTypes.return_value = ["Document"]
        gen.conn.getEndpoints.return_value = []
        gen.conn.getVertexCount.return_value = 4
        out = gen._run("How many documents?", ["Document"], None, None, None, None)
        self.assertEqual(out["function_call"], "getVertexCount('Document')")
        self.assertEqual(json.loads(out["result"]), 4)


class TestCypherText(unittest.TestCase):
    """The generated query is the Cypher only (GML-2336)."""

    def test_fences_and_language_lines_are_removed(self):
        from tools.generate_cypher import strip_code_fence
        q = "MATCH (i:JiraIssue) RETURN i"
        for reply in (q, f"```cypher\n{q}\n```", f"```opencypher\n{q}\n```", f"```\n{q}\n```",
                      f"opencypher\n{q}", f"```Cypher\n{q}```", f"`{q}`"):
            self.assertEqual(strip_code_fence(reply), q, reply)

    def test_query_runs_interpreted_on_the_connection_graph(self):
        from tools.generate_cypher import run_cypher
        conn = mock.Mock()
        conn.runInterpretedQuery.return_value = [{"T": {"n": 4}}]
        self.assertEqual(run_cypher(conn, "MATCH (d:Document) RETURN count(d) AS n"), [{"T": {"n": 4}}])
        text = conn.runInterpretedQuery.call_args[0][0]
        self.assertTrue(text.startswith("INTERPRET OPENCYPHER QUERY () FOR GRAPH $graphname {"))
        self.assertNotIn("USE GRAPH", text)
        conn.gsql.assert_not_called()

    def test_classic_engine_uses_the_same_runner(self):
        from agent.agent_graph import TigerGraphAgentGraph
        node = object.__new__(TigerGraphAgentGraph)
        node.emit_progress = lambda *a, **k: None
        node.cypher_gen = mock.Mock(_run=mock.Mock(return_value="MATCH (d:Document) RETURN count(d) AS n"))
        node.db_connection = mock.Mock()
        node.db_connection.runInterpretedQuery.return_value = [{"T": {"n": 4}}]
        state = node.generate_cypher({"question": "How many documents?"})
        self.assertEqual(state["context"]["result"], {"T": {"n": 4}})
        self.assertEqual(state["context"]["cypher"], "MATCH (d:Document) RETURN count(d) AS n")
        node.db_connection.gsql.assert_not_called()


class TestTraceShowsTheQuery(unittest.TestCase):
    def test_query_and_attempts_lead_the_step_output(self):
        out = {"ok": True, "summary": "graph query returned rows", "context": {"result": [1]},
               "citations": [], "query": _CYPHER, "attempts": [{"query": _FUNC, "outcome": "no rows"}]}
        step = mock.Mock(id="S1", tool="graphrag__structural_retrieve", kind="structural", rationale="")
        traces = []
        with mock.patch.object(ex.registry, "run", return_value=out):
            ex._run_step(step, {"question": "q"}, mock.Mock(), {}, traces)
        self.assertEqual(list(traces[0]["output"])[:3], ["summary", "query", "attempts"])
        self.assertEqual(traces[0]["output"]["query"], _CYPHER)


if __name__ == "__main__":
    unittest.main()
