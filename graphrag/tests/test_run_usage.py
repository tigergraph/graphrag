"""Time, model calls, tokens and cost of ingest and rebuild runs (GML-2381)."""
import asyncio
import contextvars
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest import mock

from common.utils import run_usage


class _InTempDir:
    def __enter__(self):
        self.prev = os.getcwd()
        self.tmp = tempfile.mkdtemp(prefix="run_usage_test_")
        os.chdir(self.tmp)
        return self.tmp

    def __exit__(self, *exc):
        os.chdir(self.prev)


class TestRunUsage(unittest.TestCase):
    def test_totals_by_kind_and_phases(self):
        with _InTempDir(), run_usage.track_run(run_usage.REBUILD, "G") as run:
            run_usage.record("entity_extraction", 100, 20, 0.01)
            run_usage.record("entity_extraction", 50, 10, 0.005)
            run_usage.record("embedding", 30, items=3)
            run_usage.add_phase("documents", 12.5)
        s = run.summary()
        self.assertEqual(s["status"], "completed")
        self.assertEqual(s["by_kind"]["entity_extraction"]["calls"], 2)
        self.assertEqual(s["by_kind"]["entity_extraction"]["input_tokens"], 150)
        self.assertEqual(s["by_kind"]["embedding"]["items"], 3)
        self.assertEqual(s["totals"], {"calls": 3, "input_tokens": 180, "output_tokens": 30, "cost": 0.015})
        self.assertEqual(s["phases"], {"documents": 12.5})

    def test_log_line_starts_with_the_kind_of_run(self):
        with _InTempDir():
            with run_usage.track_run(run_usage.REBUILD, "G") as run:
                run_usage.record("entity_extraction", 10, 2)
            self.assertTrue(run.log_line().startswith("Rebuild Usage: G completed in "))
            self.assertIn("entity_extraction 1 calls 10/2 tokens", run.log_line())
        self.assertEqual(run_usage.run_label(run_usage.INGEST), "Ingest Usage")
        self.assertEqual(run_usage.run_label("regenerate_chunk_summaries"), "Chunk Summaries Usage")
        self.assertEqual(run_usage.run_label("something_new"), "Something New Usage")

    def test_summary_is_saved_per_kind_and_survives(self):
        with _InTempDir():
            with run_usage.track_run(run_usage.INGEST, "G"):
                run_usage.record("image_description", 10, 5)
            with run_usage.track_run(run_usage.REBUILD, "G"):
                pass
            runs = run_usage.load_summaries("G")
            self.assertEqual(set(runs), {"ingest", "rebuild"})
            self.assertEqual(runs["ingest"]["totals"]["calls"], 1)

    def test_a_failed_run_is_saved_as_failed(self):
        with _InTempDir():
            with self.assertRaises(RuntimeError):
                with run_usage.track_run(run_usage.REBUILD, "G"):
                    raise RuntimeError("boom")
            self.assertEqual(run_usage.load_summaries("G")["rebuild"]["status"], "failed")

    def test_a_nested_run_adds_to_the_outer_run(self):
        with _InTempDir(), run_usage.track_run(run_usage.INGEST, "G") as outer:
            with run_usage.track_run(run_usage.CONVERSION, "G") as inner:
                run_usage.record("image_description", 1, 1)
            self.assertIs(inner, outer)
            self.assertEqual(set(run_usage.load_summaries("G")), set())
        self.assertEqual(outer.totals()["calls"], 1)

    def test_nothing_is_recorded_outside_a_run(self):
        run_usage.record("entity_extraction", 1, 1)
        self.assertIsNone(run_usage.current_run())

    def test_child_tasks_and_executor_threads_count_toward_the_run(self):
        async def call():
            run_usage.record("entity_extraction", 1, 1)

        async def pipeline():
            async with asyncio.TaskGroup() as grp:
                for _ in range(3):
                    grp.create_task(call())
            await asyncio.to_thread(run_usage.record, "embedding", 1)

        with _InTempDir(), run_usage.track_run(run_usage.REBUILD, "G") as run:
            asyncio.run(pipeline())
            contexts = [contextvars.copy_context() for _ in range(4)]
            with ThreadPoolExecutor(4) as ex:
                list(ex.map(lambda c: c.run(run_usage.record, "image_description", 1, 1), contexts))
        self.assertEqual(run.summary()["by_kind"]["entity_extraction"]["calls"], 3)
        self.assertEqual(run.summary()["by_kind"]["embedding"]["calls"], 1)
        self.assertEqual(run.summary()["by_kind"]["image_description"]["calls"], 4)


class TestTokenCounts(unittest.TestCase):
    def test_counts_from_the_response_when_the_callback_has_none(self):
        from common.llm_services.base_llm import usage_from
        cb = SimpleNamespace(prompt_tokens=0, completion_tokens=0, total_tokens=0, total_cost=0.0)
        msg = SimpleNamespace(usage_metadata={"input_tokens": 120, "output_tokens": 30, "total_tokens": 150})
        self.assertEqual(usage_from(cb, msg)["input_tokens"], 120)
        self.assertEqual(usage_from(cb, msg)["output_tokens"], 30)

    def test_callback_counts_win_when_present(self):
        from common.llm_services.base_llm import usage_from
        cb = SimpleNamespace(prompt_tokens=7, completion_tokens=3, total_tokens=10, total_cost=0.1)
        msg = SimpleNamespace(usage_metadata={"input_tokens": 999, "output_tokens": 1, "total_tokens": 1000})
        self.assertEqual(usage_from(cb, msg)["total_tokens"], 10)

    def test_recorded_usage_reaches_the_run_with_the_configured_rates(self):
        from common.llm_services.base_llm import _record_usage
        with _InTempDir(), run_usage.track_run(run_usage.CHUNK_SUMMARIES, "G") as run:
            _record_usage("chunk_summary", {"input_tokens": 1_000_000, "output_tokens": 0, "cost": 0.0},
                          {"input_cost_per_1m": "2", "output_cost_per_1m": "8"})
        self.assertAlmostEqual(run.totals()["cost"], 2.0)


class TestEmbeddingCalls(unittest.TestCase):
    def _model(self):
        from common.embeddings.embedding_services import EmbeddingModel
        model = EmbeddingModel.__new__(EmbeddingModel)
        model.config = {}
        model.token_calculator = SimpleNamespace(count_tokens=lambda t: len(t.split()))
        return model

    def test_counted_only_inside_a_run(self):
        model = self._model()
        model._record_run_usage(["a b c"])  # no run: nothing to count into
        with _InTempDir(), run_usage.track_run(run_usage.REBUILD, "G") as run:
            model._record_run_usage(["a b c", "d e"])
        emb = run.summary()["by_kind"]["embedding"]
        self.assertEqual((emb["calls"], emb["items"], emb["input_tokens"]), (1, 2, 5))


class TestEntityExtraction(unittest.TestCase):
    def test_each_extraction_call_is_counted(self):
        from common.extractors.LLMEntityRelationshipExtractor import LLMEntityRelationshipExtractor
        ext = LLMEntityRelationshipExtractor.__new__(LLMEntityRelationshipExtractor)
        ext.llm_service = SimpleNamespace(config={})
        out = SimpleNamespace(content="{}", usage_metadata={"input_tokens": 40, "output_tokens": 8,
                                                             "total_tokens": 48})

        class _Chain:
            async def ainvoke(self, _):
                return out

        parser = SimpleNamespace(get_format_instructions=lambda: "")
        with _InTempDir(), run_usage.track_run(run_usage.REBUILD, "G") as run:
            asyncio.run(ext._aextract_kg_from_doc("text", _Chain(), parser))
        calls = run.summary()["by_kind"]["entity_extraction"]
        self.assertEqual((calls["calls"], calls["input_tokens"], calls["output_tokens"]), (1, 40, 8))
