import unittest

try:
    from tools.graphrag_tools import _unstructured_result
except ImportError as exc:  # needs the app environment
    raise unittest.SkipTest(f"app environment unavailable: {exc}")


Q = "GraphRAG_Hybrid_Vector_Search"


def _step(final_retrieval, edges=()):
    return [{"final_retrieval": final_retrieval, "edges": list(edges)}]


class TestRetrievalStepSummary(unittest.TestCase):
    """A search step's summary counts what it retrieved, not the result's keys."""

    def test_counts_chunks(self):
        fr = {f"guide.pdf_chunk_{i}": [f"text {i}"] for i in range(20)}
        out = _unstructured_result(Q, _step(fr, edges=[{"s": "a", "t": "b"}] * 7))
        self.assertTrue(out["ok"])
        self.assertEqual(out["summary"], f"{Q} returned 20 chunk(s)")
        self.assertEqual(out["context"]["result"]["final_retrieval"], fr)

    def test_reports_non_chunk_entries_separately(self):
        fr = {"ticket_42_chunk_0": ["t"], "ticket_42_chunk_1": ["t"],
              "TigerGraph": ["Entity: TigerGraph ..."], "GPE": ["Entity: GPE ..."],
              "Similarity_Context": ["..."]}
        out = _unstructured_result(Q, _step(fr))
        self.assertEqual(out["summary"], f"{Q} returned 2 chunk(s) and 3 other entries")

    def test_one_other_entry_is_singular(self):
        out = _unstructured_result(Q, _step({"a_chunk_0": ["t"], "community_1": ["s"]}))
        self.assertEqual(out["summary"], f"{Q} returned 1 chunk(s) and 1 other entry")

    def test_name_merely_containing_chunk_is_not_a_chunk(self):
        out = _unstructured_result(Q, _step({"chunk_size_setting": ["t"]}))
        self.assertEqual(out["summary"], f"{Q} returned 0 chunk(s) and 1 other entry")

    def test_empty_retrieval_is_reported_as_empty(self):
        out = _unstructured_result(Q, _step({}))
        self.assertFalse(out["ok"])
        self.assertEqual(out["summary"], f"{Q} returned no chunks")
        self.assertIsNone(out["context"])

    def test_missing_result_is_reported_as_empty(self):
        self.assertFalse(_unstructured_result(Q, [])["ok"])
        self.assertFalse(_unstructured_result(Q, None)["ok"])


class TestRetrievalLog(unittest.TestCase):
    """Each retrieval logs its chunk count and size right after the query."""

    def test_line_counts_chunks_and_their_size(self):
        from common.utils.retrieval_stats import describe_retrieval
        fr = {"a_chunk_0": ["x" * 1000], "a_chunk_1": ["y" * 2500, "z" * 500]}
        self.assertEqual(describe_retrieval(Q, fr), f"{Q} retrieved 2 chunk(s), 4,000 chars")

    def test_line_reports_other_entries_and_nested_text(self):
        from common.utils.retrieval_stats import describe_retrieval
        fr = {"a_chunk_0": ["x" * 10], "TigerGraph": ["e" * 300],
              "Similarity_Context": [["s" * 20], ["t" * 30]]}
        self.assertEqual(
            describe_retrieval(Q, fr),
            f"{Q} retrieved 1 chunk(s), 10 chars; 2 other entries, 350 chars",
        )

    def test_retriever_logs_after_the_query(self):
        try:
            from supportai.retrievers.BaseRetriever import BaseRetriever
        except ImportError as exc:
            self.skipTest(f"app environment unavailable: {exc}")
        import logging
        retriever = object.__new__(BaseRetriever)
        retriever.logger = logging.getLogger("test.retrieval")
        with self.assertLogs("test.retrieval", level="INFO") as logs:
            retriever._log_retrieval(Q, _step({"a_chunk_0": ["x" * 42]}))
        self.assertEqual(logs.output, [f"INFO:test.retrieval:{Q} retrieved 1 chunk(s), 42 chars"])


class TestNestedSiblingResults(unittest.TestCase):
    """Contextual search groups chunks under the match they surround."""

    FR = {
        "doc_chunk_14": {
            "doc_chunk_13": {"distance": "-1", "content": "a" * 10},
            "doc_chunk_14": {"distance": "0", "content": "b" * 20},
            "doc_chunk_15": {"distance": "1", "content": "c" * 30},
        },
        "doc_chunk_21": {
            "doc_chunk_21": {"distance": "0", "content": "d" * 40},
            "doc_chunk_15": {"distance": "-6", "content": "c" * 30},  # listed twice
        },
    }

    def test_summary_counts_the_chunks_returned_not_the_matches(self):
        out = _unstructured_result("Chunk_Sibling_Vector_Search", _step(self.FR))
        self.assertEqual(out["summary"], "Chunk_Sibling_Vector_Search returned 4 chunk(s)")

    def test_log_sizes_by_chunk_text(self):
        from common.utils.retrieval_stats import describe_retrieval
        self.assertEqual(
            describe_retrieval("Q", self.FR), "Q retrieved 4 chunk(s), 100 chars"
        )

    def test_trace_records_every_fetched_chunk(self):
        try:
            from agent.agentic_executor import retrieved_chunk_ids
        except ImportError as exc:
            self.skipTest(f"app environment unavailable: {exc}")
        ids = retrieved_chunk_ids({"result": {"final_retrieval": self.FR}})
        self.assertEqual(ids, ["doc_chunk_13", "doc_chunk_14", "doc_chunk_15", "doc_chunk_21"])


if __name__ == "__main__":
    unittest.main()
