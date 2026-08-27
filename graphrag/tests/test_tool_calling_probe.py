"""Unit tests for the runtime tool-calling probe + in-memory cache (GML-2169).

The probe is authoritative; the static per-vendor heuristic is only the
pre-probe / transient default. Results are cached in memory (never in config)
and cleared on restart.
"""
import unittest
from unittest import mock

from common.llm_services import capabilities as cap

# openai gpt-4.1 -> heuristic True; bedrock claude-opus-5 -> heuristic False
# (the allowlist only knows claude-3/4, so the probe is what rescues Claude 5).
CFG_HEUR_TRUE = {"llm_service": "openai", "llm_model": "gpt-4.1"}
CFG_HEUR_FALSE = {"llm_service": "bedrock", "llm_model": "anthropic.claude-opus-5"}


class TestToolCallingProbe(unittest.TestCase):
    def setUp(self):
        cap.reset_tool_calling_cache()

    def test_cached_result_skips_probe(self):
        cap._probe_cache[cap._probe_key(CFG_HEUR_TRUE)] = True
        with mock.patch.object(cap, "_run_tool_calling_probe") as probe:
            self.assertTrue(cap.supports_tool_calling(CFG_HEUR_TRUE, llm_provider=object()))
            probe.assert_not_called()

    def test_probe_true_is_cached(self):
        with mock.patch.object(cap, "_run_tool_calling_probe", return_value=True) as probe:
            self.assertTrue(cap.supports_tool_calling(CFG_HEUR_FALSE, llm_provider=object()))
            self.assertTrue(cap.supports_tool_calling(CFG_HEUR_FALSE, llm_provider=object()))
            probe.assert_called_once()  # second call served from cache

    def test_probe_false_disables_even_when_heuristic_true(self):
        with mock.patch.object(cap, "_run_tool_calling_probe", return_value=False):
            self.assertFalse(cap.supports_tool_calling(CFG_HEUR_TRUE, llm_provider=object()))
        # cached False persists (until config change / restart), overriding the
        # heuristic which would say True for gpt-4.1
        self.assertFalse(cap.supports_tool_calling(CFG_HEUR_TRUE, llm_provider=object()))

    def test_transient_falls_back_to_heuristic_and_is_not_cached(self):
        with mock.patch.object(cap, "_run_tool_calling_probe", return_value=None):
            self.assertTrue(cap.supports_tool_calling(CFG_HEUR_TRUE, llm_provider=object()))
        self.assertNotIn(cap._probe_key(CFG_HEUR_TRUE), cap._probe_cache)  # re-probes next time

    def test_no_provider_uses_heuristic_without_caching(self):
        self.assertTrue(cap.supports_tool_calling(CFG_HEUR_TRUE))
        self.assertFalse(cap.supports_tool_calling(CFG_HEUR_FALSE))  # Claude 5 not in allowlist
        self.assertEqual(cap._probe_cache, {})

    def test_mark_unsupported_disables(self):
        cap.mark_tool_calling_unsupported(CFG_HEUR_TRUE)
        with mock.patch.object(cap, "_run_tool_calling_probe") as probe:
            self.assertFalse(cap.supports_tool_calling(CFG_HEUR_TRUE, llm_provider=object()))
            probe.assert_not_called()

    def test_distinct_models_cache_independently(self):
        with mock.patch.object(cap, "_run_tool_calling_probe", return_value=True):
            cap.supports_tool_calling(CFG_HEUR_FALSE, llm_provider=object())
        # a different model id is a cache miss -> heuristic (no provider)
        self.assertNotIn(cap._probe_key(CFG_HEUR_TRUE), cap._probe_cache)


if __name__ == "__main__":
    unittest.main()
