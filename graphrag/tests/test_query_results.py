"""One empty-result check for the agentic and classic engines (GML-2327)."""
import unittest

try:
    from common.utils.query_results import is_empty_result
    from tools import graphrag_tools as gt
    from agent.agent_graph import TigerGraphAgentGraph
except ImportError as exc:  # needs the app environment
    raise unittest.SkipTest(f"app environment unavailable: {exc}")

EMPTY = [None, "", "  ", "[]", "{}", "null", " [ ] ", '{"VS_XXX": []}', '[{"VS_XXX": []}]',
         {"VS_XXX": []}, [{"VS_XXX": []}], [], {}, (), set(), range(0), [None, ""]]
ROWS = [0, "0", 4, "4", False, '{"count": 0}', {"count": 0}, [{"v_id": "x"}],
        '[{"v_id": "x"}]', {"VS_XXX": [{"v_id": "x"}]}, "not json text", [0]]


class TestCountRows(unittest.TestCase):
    def test_shapes(self):
        from common.utils.query_results import count_rows, describe_rows
        cases = [({"VS_XXX": [{"a": 1}] * 4}, 4), ({"T": {"document_count": 4}}, 1),
                 ([{"T_1": [{"t": 1}, {"t": 2}]}], 2), ('[{"v": 1}, {"v": 2}, {"v": 3}]', 3),
                 ("4", 1), ({"VS": []}, 0), ([], 0), (None, 0)]
        for value, n in cases:
            self.assertEqual(count_rows(value), n, repr(value))
        self.assertEqual(describe_rows("graph query", {"VS": [{"a": 1}]}),
                         'graph query returned 1 row, 18 chars')


class TestIsEmptyResult(unittest.TestCase):
    def test_empty(self):
        for value in EMPTY:
            self.assertTrue(is_empty_result(value), repr(value))

    def test_rows(self):
        for value in ROWS:
            self.assertFalse(is_empty_result(value), repr(value))

    def test_both_engines_agree(self):
        classic = TigerGraphAgentGraph.is_query_result_empty
        for value in EMPTY + ROWS:
            self.assertEqual(gt._result_is_empty(value), is_empty_result(value), repr(value))
            self.assertEqual(classic(None, value), is_empty_result(value), repr(value))


if __name__ == "__main__":
    unittest.main()
