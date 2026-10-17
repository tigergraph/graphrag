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
    conn = mock.Mock(graphname="G")
    conn.checkQuerySemantic.return_value = {"warnings": [], "errors": []}
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
        conn = mock.Mock(graphname="G")
        conn.runInterpretedQuery.return_value = [{"T": {"n": 4}}]
        self.assertEqual(run_cypher(conn, "MATCH (d:Document) RETURN count(d) AS n"), [{"T": {"n": 4}}])
        text = conn.runInterpretedQuery.call_args[0][0]
        self.assertTrue(text.startswith("INTERPRET OPENCYPHER QUERY () FOR GRAPH G {"))
        self.assertNotIn("USE GRAPH", text)
        conn.gsql.assert_not_called()

    def test_classic_engine_uses_the_same_runner(self):
        from agent.agent_graph import TigerGraphAgentGraph
        node = object.__new__(TigerGraphAgentGraph)
        node.emit_progress = lambda *a, **k: None
        node.cypher_gen = mock.Mock(_run=mock.Mock(return_value="MATCH (d:Document) RETURN count(d) AS n"))
        node.db_connection = mock.Mock(graphname="G")
        node.db_connection.checkQuerySemantic.return_value = {"warnings": [], "errors": []}
        node.db_connection.runInterpretedQuery.return_value = [{"T": {"n": 4}}]
        state = node.generate_cypher({"question": "How many documents?"})
        self.assertEqual(state["context"]["result"], {"T": {"n": 4}})
        self.assertEqual(state["context"]["cypher"], "MATCH (d:Document) RETURN count(d) AS n")
        node.db_connection.gsql.assert_not_called()


class TestCheckBeforeRun(unittest.TestCase):
    """Generated queries are checked before they run (GML-2336)."""

    def _conn(self, checks, runs=None):
        conn = mock.Mock(graphname="G")
        conn.checkQuerySemantic.side_effect = checks
        conn.runInterpretedQuery.side_effect = runs or [[{"T": {"n": 4}}]]
        return conn

    def _gen(self, queries):
        return mock.Mock(_run=mock.Mock(side_effect=queries))

    def test_failed_check_feeds_the_error_back_and_skips_the_run(self):
        from tools.generate_cypher import generate_and_run_cypher
        bad = 'MATCH (i:JiraIssue)\nWHERE i.created >= datetime("2025-08-01")\nRETURN count(i) AS n'
        good = 'MATCH (i:JiraIssue)\nWHERE i.created >= to_datetime("2025-08-01")\nRETURN count(i) AS n'
        err = {"errors": [{"msg": "no viable alternative at input 'datetime'", "line": 3, "charpositioninline": 18}]}
        conn = self._conn([err, {"errors": []}])
        gen = self._gen([bad, good])
        attempts = []
        cypher, first = generate_and_run_cypher(gen, conn, "q", 10, attempts)
        self.assertEqual((cypher, first), (good, {"T": {"n": 4}}))
        conn.runInterpretedQuery.assert_called_once()  # the bad query never ran
        self.assertEqual(attempts[0]["outcome"], "failed check")
        self.assertIn("line 2, column 18", attempts[0]["error"])
        history = gen._run.call_args_list[1][0][1]
        self.assertIn("to_datetime", history[0])  # the date hint went back to the generator

    def test_stops_when_the_generator_repeats_itself(self):
        from tools.generate_cypher import generate_and_run_cypher
        q = "MATCH (x:Nope) RETURN x"
        conn = self._conn([{"errors": [{"msg": "unknown type"}]}] * 3)
        attempts = []
        self.assertEqual(generate_and_run_cypher(self._gen([q, q, q]), conn, "q", 10, attempts), (None, None))
        self.assertEqual(attempts[-1]["outcome"], "repeated an earlier query; stopped")
        self.assertEqual(len(attempts), 2)

    def test_attempt_limit(self):
        from tools.generate_cypher import generate_and_run_cypher
        conn = self._conn([{"errors": [{"msg": "bad"}]}] * 20)
        gen = self._gen([f"MATCH (n{i}) RETURN n{i}" for i in range(20)])
        generate_and_run_cypher(gen, conn, "q", 4, [])
        self.assertEqual(gen._run.call_count, 4)

    def test_write_queries_are_rejected_without_a_check(self):
        from tools.generate_cypher import check_cypher
        conn = mock.Mock(graphname="G")
        self.assertIn("changes data", check_cypher(conn, "MATCH (n) DETACH DELETE n")[0])
        conn.checkQuerySemantic.assert_not_called()
        conn.checkQuerySemantic.return_value = {"errors": []}
        self.assertEqual(check_cypher(conn, 'MATCH (n {name: "set to delete"}) RETURN n'), [])

    def test_null_test_run_error_gets_a_hint(self):
        from tools.generate_cypher import generate_and_run_cypher, run_error_hints
        bad = 'MATCH (i:JiraIssue)\nWHERE i.issue_key STARTS WITH "GML-" AND i.resolution IS NOT NULL\nRETURN i.issue_key AS k'
        good = 'MATCH (i:JiraIssue)\nWHERE i.issue_key STARTS WITH "GML-" AND i.resolution <> ""\nRETURN i.issue_key AS k'
        err = RuntimeError("Semantic Check Fails: Query [null] cannot be executed in both "
                           "instruction mode and legacy mode at the same time.")
        conn = self._conn([{"errors": []}] * 2, [err, [{"T": [{"k": "GML-1"}]}]])
        gen = self._gen([bad, good])
        attempts = []
        cypher, first = generate_and_run_cypher(gen, conn, "q", 10, attempts)
        self.assertEqual(cypher, good)
        self.assertIn("NULLABLE", attempts[0]["error"])
        history = gen._run.call_args_list[1][0][1]
        self.assertIn('<> ""', history[0])  # the null hint went back to the generator
        # No hint when the query has none of the constructs, or has them only inside a string.
        self.assertEqual(run_error_hints(str(err), 'MATCH (i) WHERE i.k = "x" RETURN i'), [])
        self.assertEqual(run_error_hints(str(err), 'MATCH (i) WHERE i.k = "IS NULL" RETURN i'), [])
        self.assertEqual(run_error_hints("other error", bad), [])

    def test_optional_match_run_error_gets_a_hint(self):
        from tools.generate_cypher import run_error_hints
        err = "Query [null] cannot be executed in both instruction mode and legacy mode at the same time."
        q = ('MATCH (i:JiraIssue) WITH i OPTIONAL MATCH (i)-[:JIRA_BELONGS_TO]-(p:JiraProject) '
             'WHERE toLower(i.issue_key) STARTS WITH "gpe" RETURN count(DISTINCT i) AS n')
        hints = run_error_hints(err, q)
        self.assertTrue(any("Use MATCH" in h for h in hints))
        self.assertTrue(any("Keep the string match" in h for h in hints))
        self.assertFalse(any("IS NULL / IS NOT NULL in this query" in h for h in hints))

    def test_same_run_error_stops_early(self):
        from tools.generate_cypher import generate_and_run_cypher, SAME_RUN_ERROR_LIMIT
        err = RuntimeError("cannot be executed in both instruction mode and legacy mode")
        conn = self._conn([{"errors": []}] * 10, [err] * 10)
        gen = self._gen([f"MATCH (n{i}) RETURN n{i}" for i in range(10)])
        attempts = []
        self.assertEqual(generate_and_run_cypher(gen, conn, "q", 10, attempts), (None, None))
        self.assertEqual(gen._run.call_count, SAME_RUN_ERROR_LIMIT)
        self.assertIn("same run error 3 times in a row", attempts[-1]["outcome"])

    def test_different_run_errors_keep_trying(self):
        from tools.generate_cypher import generate_and_run_cypher
        errs = [RuntimeError(f"error {i}") for i in range(4)]
        conn = self._conn([{"errors": []}] * 5, errs + [[{"T": {"n": 1}}]])
        gen = self._gen([f"MATCH (n{i}) RETURN n{i}" for i in range(5)])
        cypher, first = generate_and_run_cypher(gen, conn, "q", 10, [])
        self.assertEqual(first, {"T": {"n": 1}})

    def test_unavailable_check_runs_the_query(self):
        from tools.generate_cypher import generate_and_run_cypher
        conn = self._conn(RuntimeError("only on TigerGraph >= 4.0"))
        cypher, first = generate_and_run_cypher(self._gen(["MATCH (d:Document) RETURN count(d) AS n"]), conn, "q")
        self.assertEqual(first, {"T": {"n": 4}})

    def test_attempt_setting_reaches_the_structural_tool(self):
        ctx = _ctx({"function_call": _FUNC, "result": "[]"}, use_cypher=True,
                   cypher_runs=[(f"MATCH (n{i}) RETURN n{i}", []) for i in range(6)])
        ctx.conn.checkQuerySemantic.return_value = {"errors": [{"msg": "bad"}]}
        ctx.graphrag_cfg = {"cypher_max_attempts": 3}
        gt.structural_retrieve(ctx, "q")
        self.assertEqual(ctx.cypher_gen._run.call_count, 3)

    def test_prompt_rules(self):
        from common.llm_services.base_llm import LLM_Model
        prompt = LLM_Model.generate_cypher_prompt.fget(types.SimpleNamespace(
            _read_prompt_file=lambda p: None, prompt_path=""))
        self.assertIn('to_datetime("YYYY-MM-DD hh:mm:ss")', prompt)
        supported = prompt.split("## Supported")[1].split("## Unsupported")[0]
        self.assertNotIn("DELETE", supported)
        self.assertIn("`DETACH DELETE`", prompt.split("## Unsupported")[1])
        self.assertIn("only on an attribute that the schema marks `NULLABLE`", prompt)
        self.assertIn("write the literal in lower case", prompt)
        self.assertIn("Do not combine `STARTS WITH`, `ENDS WITH` or `CONTAINS` with `OPTIONAL MATCH`", prompt)


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
