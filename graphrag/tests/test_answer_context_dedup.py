import copy
import unittest

try:
    from agent.agentic_synthesizer import _gather
    from common.py_schemas import StepResult
except ImportError as exc:  # needs the app environment
    raise unittest.SkipTest(f"app environment unavailable: {exc}")


def _step(sid, query, final_retrieval):
    return StepResult(step_id=sid, ok=True, summary="", citations=[], context={
        "function_call": query, "result": {"final_retrieval": final_retrieval, "edges": []},
    })


def _fr(ctx):
    return ctx["result"]["final_retrieval"]


class TestAnswerContextDedup(unittest.TestCase):
    """Each passage reaches the answer model once, whichever step found it."""

    def test_overlapping_searches_keep_the_first_copy(self):
        results = {
            "S1": _step("S1", "GraphRAG_Hybrid_Vector_Search", {"a_chunk_1": ["one"], "a_chunk_2": ["two"]}),
            "S2": _step("S2", "GraphRAG_Hybrid_Vector_Search", {"a_chunk_2": ["two"], "a_chunk_3": ["three"]}),
        }
        unstructured = _gather(results)["unstructured"]
        self.assertEqual(_fr(unstructured[0]), {"a_chunk_1": ["one"], "a_chunk_2": ["two"]})
        self.assertEqual(_fr(unstructured[1]), {"a_chunk_3": ["three"]})

    def test_step_with_nothing_new_is_dropped(self):
        results = {
            "S1": _step("S1", "GraphRAG_Hybrid_Vector_Search", {"a_chunk_1": ["one"]}),
            "S2": _step("S2", "Content_Similarity_Vector_Search", {"a_chunk_1": ["one"]}),
        }
        self.assertEqual(len(_gather(results)["unstructured"]), 1)

    def test_grouped_results_lose_only_repeated_chunks(self):
        results = {
            "S1": _step("S1", "GraphRAG_Hybrid_Vector_Search", {"a_chunk_14": ["fourteen"]}),
            "S2": _step("S2", "Chunk_Sibling_Vector_Search", {
                "a_chunk_14": {
                    "a_chunk_13": {"distance": "-1", "content": "thirteen"},
                    "a_chunk_14": {"distance": "0", "content": "fourteen"},
                },
                "a_chunk_30": {"a_chunk_14": {"distance": "-16", "content": "fourteen"}},
            }),
        }
        grouped = _fr(_gather(results)["unstructured"][1])
        self.assertEqual(grouped, {"a_chunk_14": {"a_chunk_13": {"distance": "-1", "content": "thirteen"}}})

    def test_same_id_with_different_text_is_kept(self):
        """Hybrid seeds and communities can return the same id with other text."""
        results = {
            "S1": _step("S1", "GraphRAG_Community_Vector_Search", {"c_1": "summary v1"}),
            "S2": _step("S2", "GraphRAG_Community_Vector_Search", {"c_1": "summary v2"}),
        }
        unstructured = _gather(results)["unstructured"]
        self.assertEqual([_fr(c) for c in unstructured], [{"c_1": "summary v1"}, {"c_1": "summary v2"}])

    def test_only_new_pieces_of_a_list_are_kept(self):
        results = {
            "S1": _step("S1", "GraphRAG_Hybrid_Vector_Search", {"a_chunk_1": ["text", "Entity: A"]}),
            "S2": _step("S2", "GraphRAG_Hybrid_Vector_Search",
                        {"a_chunk_1": ["text", "Entity: A", "Entity: B"]}),
        }
        self.assertEqual(_fr(_gather(results)["unstructured"][1]), {"a_chunk_1": ["Entity: B"]})

    def test_flat_and_grouped_copies_of_one_chunk_dedupe(self):
        results = {
            "S1": _step("S1", "Chunk_Sibling_Vector_Search",
                        {"a_chunk_2": {"a_chunk_2": {"distance": "0", "content": "two"}}}),
            "S2": _step("S2", "GraphRAG_Hybrid_Vector_Search", {"a_chunk_2": ["two"], "a_chunk_3": ["three"]}),
        }
        self.assertEqual(_fr(_gather(results)["unstructured"][1]), {"a_chunk_3": ["three"]})

    def test_step_results_are_not_modified(self):
        results = {
            "S1": _step("S1", "GraphRAG_Hybrid_Vector_Search", {"a_chunk_1": ["one"]}),
            "S2": _step("S2", "GraphRAG_Hybrid_Vector_Search", {"a_chunk_1": ["one"], "a_chunk_2": ["two"]}),
        }
        before = copy.deepcopy(results["S2"].context)
        _gather(results)
        self.assertEqual(results["S2"].context, before)

    def test_structural_results_are_untouched(self):
        structural = StepResult(step_id="S1", ok=True, summary="", citations=[],
                                context={"function_call": "tg__run_query", "result": [{"n": 1}]})
        results = {"S1": structural, "S2": copy.deepcopy(structural)}
        results["S2"].step_id = "S2"
        self.assertEqual(len(_gather(results)["structural"]), 2)


if __name__ == "__main__":
    unittest.main()
