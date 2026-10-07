import json
import unittest
from unittest import mock

try:
    from agent import agentic_executor as ex
except ImportError as exc:  # needs the app environment
    raise unittest.SkipTest(f"app environment unavailable: {exc}")


def _size(obj):
    return len(json.dumps(obj, ensure_ascii=False))


def _retrieval(n_chunks, chunk_chars, text="x"):
    return {
        "function_call": "GraphRAG_Hybrid_Vector_Search",
        "result": {
            "final_retrieval": {
                f"doc_{i}_chunk_{i}": [text * chunk_chars] for i in range(n_chunks)
            },
            "edges": [],
        },
    }


class TestFitForTrace(unittest.TestCase):
    """The trace keeps the tool's own data, shortened to fit when needed."""

    def test_small_value_is_unchanged(self):
        value, cut = ex.fit_for_trace({"question": "q"})
        self.assertEqual(value, {"question": "q"})
        self.assertIsNone(cut)

    def test_large_retrieval_keeps_its_shape(self):
        ctx = _retrieval(20, 3400)
        value, cut = ex.fit_for_trace(ctx)
        self.assertEqual(cut["full_chars"], _size(ctx))
        self.assertLessEqual(cut["shown_chars"], ex._TRACE_FIELD_CAP)
        fr = value["result"]["final_retrieval"]
        self.assertEqual(set(fr), set(ctx["result"]["final_retrieval"]))  # every chunk id kept
        for cid, texts in fr.items():
            original = ctx["result"]["final_retrieval"][cid][0]
            shown, _, marker = texts[0].partition("…(+")
            self.assertTrue(original.startswith(shown))
            self.assertEqual(marker, f"{len(original) - len(shown):,} chars)")
        self.assertNotIn("_truncated", json.dumps(value))

    def test_many_entries_are_cut_to_fit(self):
        value, cut = ex.fit_for_trace(_retrieval(2000, 200))
        self.assertLessEqual(_size(value), ex._TRACE_FIELD_CAP)
        self.assertIn("final_retrieval", value["result"])

    def test_non_english_text_is_counted_as_written(self):
        ctx = _retrieval(20, 1000, text="日")
        _, cut = ex.fit_for_trace(ctx)
        self.assertEqual(cut["full_chars"], _size(ctx))
        self.assertLess(cut["full_chars"], len(json.dumps(ctx)))  # escaped form is larger

    def test_short_texts_get_no_marker(self):
        ctx = _retrieval(20, 3400)
        ctx["result"]["final_retrieval"]["short_chunk_0"] = ["brief"]
        value, _ = ex.fit_for_trace(ctx)
        self.assertEqual(value["result"]["final_retrieval"]["short_chunk_0"], ["brief"])

    def test_note_wording(self):
        self.assertEqual(
            ex.trace_note({"full_chars": 68331, "shown_chars": 11958}),
            "Truncated from 68,331 to 11,958 characters due to the trace log size limit.",
        )


class TestStepTrace(unittest.TestCase):
    """A step's trace output is the tool's output, plus a note when shortened."""

    def _run(self, context):
        out = {"ok": True, "summary": "returned 20 chunk(s)", "context": context, "citations": []}
        step = mock.Mock(id="S1", tool="graphrag__hybrid_search", kind="unstructured", rationale="")
        results, traces = {}, []
        with mock.patch.object(ex.registry, "run", return_value=out):
            ex._run_step(step, {"question": "q"}, mock.Mock(), results, traces)
        self.assertEqual(results["S1"].context, context)  # the answer still gets everything
        return traces[0]

    def test_large_output_gets_a_note(self):
        ctx = _retrieval(20, 3400)
        entry = self._run(ctx)
        self.assertEqual(set(entry["output"]), {"summary", "result", "note"})
        self.assertEqual(entry["output"]["summary"], "returned 20 chunk(s)")
        self.assertIn(f"Truncated from {_size(ctx):,} to", entry["output"]["note"])
        self.assertIn("final_retrieval", entry["output"]["result"]["result"])
        self.assertEqual(entry["input"], {"question": "q"})  # small input, no note

    def test_small_output_has_no_note(self):
        entry = self._run(_retrieval(2, 50))
        self.assertEqual(set(entry["output"]), {"summary", "result"})


if __name__ == "__main__":
    unittest.main()
