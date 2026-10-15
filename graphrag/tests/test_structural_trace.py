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
_CYPHER = "USE GRAPH g\nINTERPRET OPENCYPHER QUERY () {\nMATCH (d:Document) RETURN d\n}"


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
    conn.gsql.side_effect = [r for _, r in cypher_runs] or ["header\n{}"]
    return types.SimpleNamespace(
        emit=lambda *a, **k: None, conversation=[], use_cypher=use_cypher,
        mq2s=mock.Mock(_run=mock.Mock(return_value=mapping)),
        gen_func=gen_func, cypher_gen=cypher_gen, conn=conn,
    )


def _gsql(results):
    return "header\n" + json.dumps({"results": results})


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
                   cypher_runs=[("BAD", "header\nnot json"), (_CYPHER, _gsql([{"VS": [{"v_id": "d1"}]}]))])
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
