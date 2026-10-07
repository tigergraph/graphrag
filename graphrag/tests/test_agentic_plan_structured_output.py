import ast
import os
import types
import unittest

_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", ".."))
_PLANNER = os.path.join(_ROOT, "graphrag", "app", "agent", "agentic_planner.py")
_AGENT = os.path.join(_ROOT, "graphrag", "app", "agent", "agentic_agent.py")


def _structured_calls(path):
    """(schema name, method keyword or None) for each invoke_structured call."""
    with open(path, encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    calls = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "invoke_structured"
        ):
            schema = ast.unparse(node.args[1]) if len(node.args) > 1 else None
            method = next(
                (ast.unparse(k.value) for k in node.keywords if k.arg == "method"),
                None,
            )
            calls.append((schema, method))
    return calls


class TestPlannerRequestsToolCalling(unittest.TestCase):
    """``Plan`` steps carry free-form ``args`` maps, which OpenAI's default
    strict JSON-schema output rejects — so every plan made a failed request
    before falling back to parsing (GML-2302)."""

    def test_planner_asks_for_tool_calling_only_where_output_is_strict(self):
        """Tool calling is verified for the OpenAI family only; other providers
        keep their default rather than risk a new failure there."""
        methods = [m for schema, m in _structured_calls(_PLANNER) if schema == "Plan"]
        self.assertEqual(len(methods), 1)
        self.assertIn("function_calling", methods[0])
        self.assertIn("strict_structured_output", methods[0])

    def test_triage_keeps_the_default(self):
        """Its schema satisfies strict mode, which enforces it exactly."""
        calls = [c for c in _structured_calls(_AGENT) if c[0] == "_Triage"]
        self.assertTrue(calls, "triage call not found")
        self.assertTrue(all(method is None for _, method in calls))


class TestStrictProviders(unittest.TestCase):
    """Only providers whose structured output is strict JSON schema opt in."""

    @classmethod
    def setUpClass(cls):
        try:
            from common.llm_services.base_llm import LLM_Model
            from common.llm_services.openai_service import OpenAI
            from common.llm_services.azure_openai_service import AzureOpenAI
        except ImportError as exc:
            raise unittest.SkipTest(f"app environment unavailable: {exc}")
        cls.base, cls.openai, cls.azure = LLM_Model, OpenAI, AzureOpenAI

    def test_openai_family_is_strict(self):
        self.assertTrue(self.openai.strict_structured_output)
        self.assertTrue(self.azure.strict_structured_output)

    def test_default_is_not_strict(self):
        self.assertFalse(self.base.strict_structured_output)

    def test_other_providers_keep_the_default(self):
        import importlib
        for module, cls in (
            ("common.llm_services.google_genai_service", "GoogleGenAI"),
            ("common.llm_services.google_vertexai_service", "GoogleVertexAI"),
            ("common.llm_services.aws_bedrock_service", "AWSBedrock"),
            ("common.llm_services.ollama", "Ollama"),
            ("common.llm_services.groq_llm_service", "Groq"),
        ):
            with self.subTest(provider=cls):
                try:
                    provider = getattr(importlib.import_module(module), cls)
                except ImportError:
                    continue  # optional provider SDK not installed
                self.assertFalse(provider.strict_structured_output)


class TestInvokeStructuredPassesMethod(unittest.TestCase):
    """``invoke_structured`` forwards ``method`` only when one is given."""

    @classmethod
    def setUpClass(cls):
        try:
            from common.llm_services.base_llm import LLM_Model
            from common.py_schemas.schemas import Plan
        except ImportError as exc:  # needs the app environment
            raise unittest.SkipTest(f"app environment unavailable: {exc}")
        cls.invoke = staticmethod(LLM_Model.invoke_structured)
        cls.Plan = Plan

    def _run(self, method):
        seen = {}
        plan = self.Plan()

        class FakeLLM:
            def with_structured_output(self, schema, **kwargs):
                seen.update(kwargs)
                return types.SimpleNamespace(invoke=lambda messages: plan)

        host = types.SimpleNamespace(llm=FakeLLM(), config={})
        result = self.invoke(host, [("user", "q")], self.Plan, caller_name="t", method=method)
        self.assertIs(result, plan)
        return seen

    def test_no_method_keeps_the_provider_default(self):
        self.assertEqual(self._run(None), {})

    def test_method_is_forwarded(self):
        self.assertEqual(self._run("function_calling"), {"method": "function_calling"})


class TestOpenAIRequestShape(unittest.TestCase):
    """Built offline with a dummy key: the request that would go to OpenAI."""

    @classmethod
    def setUpClass(cls):
        try:
            from langchain_openai import ChatOpenAI
            from common.py_schemas.schemas import Plan
        except ImportError as exc:
            raise unittest.SkipTest(f"langchain-openai unavailable: {exc}")
        cls.llm = ChatOpenAI(model="gpt-4o-mini", api_key="sk-test")
        cls.Plan = Plan

    @staticmethod
    def _bound_kwargs(runnable):
        first = getattr(runnable, "first", runnable)
        return getattr(first, "kwargs", {})

    def test_function_calling_sends_no_strict_schema(self):
        kwargs = self._bound_kwargs(
            self.llm.with_structured_output(self.Plan, method="function_calling")
        )
        tools = kwargs.get("tools") or []
        self.assertTrue(tools, f"expected a bound tool, got {sorted(kwargs)}")
        self.assertNotEqual(tools[0].get("function", {}).get("strict"), True)
        self.assertNotIn("response_format", kwargs)

    def test_default_takes_the_strict_response_format_path(self):
        """The path that rejects free-form maps — what the planner now avoids."""
        kwargs = self._bound_kwargs(self.llm.with_structured_output(self.Plan))
        self.assertIn("response_format", kwargs)


if __name__ == "__main__":
    unittest.main()
