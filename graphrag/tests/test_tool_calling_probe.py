"""Unit tests for the runtime tool-calling probe + in-memory cache (GML-2169).

Policy: Agentic mode is ON by default. It is disabled ONLY when we are sure the
model can't tool-call — a known-legacy model, a confident "tools not supported"
probe error, or a runtime tool-calling failure. Anything uncertain stays
enabled and is not cached. Cache is in memory (never in config), cleared on
restart, and sticky until the model config changes.
"""
import unittest
from unittest import mock

from common.llm_services import capabilities as cap

CFG_MODERN = {"llm_service": "openai", "llm_model": "gpt-4.1"}
# A brand-new model the old allowlist didn't know — must be optimistic now.
CFG_NEW = {"llm_service": "bedrock", "llm_model": "anthropic.claude-opus-5"}
CFG_LEGACY = {"llm_service": "vertexai", "llm_model": "gemini-1.0-pro"}
CFG_LEGACY2 = {"llm_service": "bedrock", "llm_model": "amazon.titan-text-express-v1"}


class TestToolCallingProbe(unittest.TestCase):
    def setUp(self):
        cap.reset_tool_calling_cache()

    def test_cached_result_wins_without_probing(self):
        cap._probe_cache[cap._probe_key(CFG_MODERN)] = False
        with mock.patch.object(cap, "_run_tool_calling_probe") as probe:
            self.assertFalse(cap.supports_tool_calling(CFG_MODERN, llm_provider=object()))
            probe.assert_not_called()

    def test_known_legacy_is_disabled_without_probing(self):
        with mock.patch.object(cap, "_run_tool_calling_probe") as probe:
            self.assertFalse(cap.supports_tool_calling(CFG_LEGACY, llm_provider=object()))
            self.assertFalse(cap.supports_tool_calling(CFG_LEGACY2, llm_provider=object()))
            probe.assert_not_called()

    def test_probe_confirms_support(self):
        with mock.patch.object(cap, "_run_tool_calling_probe", return_value=True) as probe:
            self.assertTrue(cap.supports_tool_calling(CFG_NEW, llm_provider=object()))
            self.assertTrue(cap.supports_tool_calling(CFG_NEW, llm_provider=object()))
            probe.assert_called_once()  # cached after first

    def test_confident_no_support_disables(self):
        with mock.patch.object(cap, "_run_tool_calling_probe", return_value=False):
            self.assertFalse(cap.supports_tool_calling(CFG_NEW, llm_provider=object()))
        self.assertFalse(cap.supports_tool_calling(CFG_NEW, llm_provider=object()))  # cached

    def test_unknown_probe_stays_enabled_and_is_not_cached(self):
        with mock.patch.object(cap, "_run_tool_calling_probe", return_value=None):
            self.assertTrue(cap.supports_tool_calling(CFG_NEW, llm_provider=object()))
        self.assertNotIn(cap._probe_key(CFG_NEW), cap._probe_cache)  # re-probes later

    def test_no_provider_is_optimistic_except_known_legacy(self):
        self.assertTrue(cap.supports_tool_calling(CFG_MODERN))
        self.assertTrue(cap.supports_tool_calling(CFG_NEW))       # unknown model -> enabled
        self.assertFalse(cap.supports_tool_calling(CFG_LEGACY))   # sure it's legacy -> disabled
        # only the legacy one gets cached
        self.assertNotIn(cap._probe_key(CFG_MODERN), cap._probe_cache)
        self.assertNotIn(cap._probe_key(CFG_NEW), cap._probe_cache)

    def test_mark_unsupported_disables(self):
        cap.mark_tool_calling_unsupported(CFG_MODERN)
        with mock.patch.object(cap, "_run_tool_calling_probe") as probe:
            self.assertFalse(cap.supports_tool_calling(CFG_MODERN, llm_provider=object()))
            probe.assert_not_called()

    def test_known_no_tool_calling_helper(self):
        self.assertTrue(cap._known_no_tool_calling(CFG_LEGACY))
        self.assertTrue(cap._known_no_tool_calling(CFG_LEGACY2))
        self.assertFalse(cap._known_no_tool_calling(CFG_MODERN))
        self.assertFalse(cap._known_no_tool_calling(CFG_NEW))  # Claude 5 -> not legacy

    def test_no_tool_support_error_is_confident(self):
        self.assertTrue(cap._looks_like_no_tool_support(Exception("This model does not support tool use")))
        self.assertFalse(cap._looks_like_no_tool_support(Exception("rate limit exceeded, try again")))


if __name__ == "__main__":
    unittest.main()
