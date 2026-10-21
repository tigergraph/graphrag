"""Gemini 3 structured calls use temperature 1.0 (GML-2335)."""
import types
import unittest

try:
    from common.llm_services.base_llm import LLM_Model
    from common.llm_services.capabilities import gemini_loops_below_default_temperature
    from common.py_schemas import Plan
except ImportError as exc:  # needs the app environment
    raise unittest.SkipTest(f"app environment unavailable: {exc}")


class _FakeChat:
    """A chat model with a temperature, as LangChain chat models have."""

    def __init__(self, temperature):
        self.temperature = temperature
        self.used = None

    def model_copy(self, update):
        copy = _FakeChat(update.get("temperature", self.temperature))
        return copy

    def with_structured_output(self, schema, **kw):
        outer = self
        return types.SimpleNamespace(invoke=lambda m: (setattr(_FakeChat, "last", outer), Plan())[1])


def _svc(model, temperature=0):
    svc = object.__new__(LLM_Model)
    svc.llm, svc.config = _FakeChat(temperature), {"llm_model": model}
    return svc


class TestRule(unittest.TestCase):
    def test_gemini_3_models_match(self):
        for m in ("gemini-3.5-flash", "gemini-3-pro", "models/gemini-3.5-flash", "Gemini-3.1-Pro"):
            self.assertTrue(gemini_loops_below_default_temperature(m), m)

    def test_other_models_do_not(self):
        for m in ("gemini-2.5-flash", "gpt-5.4-mini", "o3", "claude-sonnet-4", "llama3.1", "", None):
            self.assertFalse(gemini_loops_below_default_temperature(m), m)


class TestStructuredCalls(unittest.TestCase):
    def test_gemini_3_structured_call_uses_1(self):
        svc = _svc("gemini-3.5-flash", 0)
        svc.invoke_structured([("user", "q")], Plan, caller_name="agentic_plan")
        self.assertEqual(_FakeChat.last.temperature, 1.0)
        self.assertEqual(svc.llm.temperature, 0)  # the service model (answers, ECC) is unchanged

    def test_higher_temperature_is_kept(self):
        svc = _svc("gemini-3.5-flash", 1.2)
        self.assertIs(svc.with_structured_temperature(svc.llm), svc.llm)

    def test_other_models_keep_their_temperature(self):
        for model in ("gpt-5.4-mini", "gemini-2.5-flash", "claude-sonnet-4"):
            svc = _svc(model, 0)
            self.assertIs(svc.with_structured_temperature(svc.llm), svc.llm, model)


if __name__ == "__main__":
    unittest.main()
