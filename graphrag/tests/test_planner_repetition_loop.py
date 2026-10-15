import types
import unittest

try:
    from common.py_schemas.schemas import Plan, PlanStep, _PLAN_TEXT_MAX, _collapse_repeats
    from common.llm_services.base_llm import LLM_Model
except ImportError as exc:  # needs the app environment
    raise unittest.SkipTest(f"app environment unavailable: {exc}")

# The cycle seen in a real trace (GML-2324), repeated as the model wrote it.
_CYCLE = ("here cleanly built step by step DAG design plan configuration object "
          "instance declaration format correctly structured now ready for "
          "evaluation execution lifecycle flow initialization now complete ")
_LOOP = ("The user is asking for an exact count of Jira tickets. This is a pure "
         "structured-data count query suitable for graphrag__structural_retrieve "
         + _CYCLE * 60)


class TestPlanTextFields(unittest.TestCase):
    """strategy and rationale carry a purpose and a bound."""

    def test_schema_tells_the_model_what_to_write(self):
        props = Plan.model_json_schema()["properties"]
        self.assertIn("One short sentence", props["strategy"]["description"])
        self.assertEqual(props["strategy"]["maxLength"], _PLAN_TEXT_MAX)
        step = Plan.model_json_schema()["$defs"]["PlanStep"]["properties"]
        self.assertIn("One short sentence", step["rationale"]["description"])

    def test_runaway_strategy_is_shortened_not_rejected(self):
        plan = Plan(strategy=_LOOP, steps=[PlanStep(id="A", kind="answer", tool="")])
        self.assertLessEqual(len(plan.strategy), _PLAN_TEXT_MAX)
        self.assertTrue(plan.strategy.startswith("The user is asking"))
        self.assertEqual(len(plan.steps), 1)

    def test_runaway_rationale_is_shortened(self):
        step = PlanStep(id="S1", tool="graphrag__hybrid_search", rationale=_CYCLE * 20)
        self.assertLessEqual(len(step.rationale), _PLAN_TEXT_MAX)

    def test_short_text_is_unchanged(self):
        self.assertEqual(Plan(strategy="Count tickets, then answer.").strategy,
                         "Count tickets, then answer.")

    def test_collapse_keeps_one_copy_of_the_cycle(self):
        out = _collapse_repeats(_LOOP)
        self.assertEqual(out.count("lifecycle flow initialization"), 1)

    def test_collapse_leaves_normal_text_alone(self):
        text = ("Search the documents for the policy, then search the documents "
                "for the exceptions, then answer from the documents.")
        self.assertEqual(_collapse_repeats(text), text)


class TestPlannerPrompt(unittest.TestCase):
    def test_fixed_rule_asks_for_one_short_sentence(self):
        self.assertIn('Then set "strategy" to one short sentence',
                      LLM_Model._AGENTIC_PLANNER_SYSTEM)


class TestOutputLimit(unittest.TestCase):
    """The planner call caps output on every provider's model class."""

    def _svc(self, llm):
        svc = object.__new__(LLM_Model)
        svc.llm, svc.config = llm, {}
        return svc

    def test_openai_uses_max_tokens(self):
        from langchain_openai import ChatOpenAI
        capped = self._svc(ChatOpenAI(model="gpt-4o-mini", api_key="x")).with_output_limit(4000)
        self.assertEqual(capped.max_tokens, 4000)

    def test_existing_lower_limit_is_kept(self):
        from langchain_openai import ChatOpenAI
        llm = ChatOpenAI(model="gpt-4o-mini", api_key="x", max_tokens=1000)
        self.assertEqual(self._svc(llm).with_output_limit(4000).max_tokens, 1000)

    def test_gemini_uses_max_output_tokens(self):
        from langchain_google_genai import ChatGoogleGenerativeAI
        llm = ChatGoogleGenerativeAI(model="gemini-2.5-flash", google_api_key="x")
        self.assertEqual(self._svc(llm).with_output_limit(4000).max_output_tokens, 4000)

    def test_ollama_uses_num_predict(self):
        from langchain_community.llms import Ollama
        self.assertEqual(self._svc(Ollama(model="llama3.1")).with_output_limit(4000).num_predict, 4000)

    def test_limit_in_model_kwargs_is_capped_there(self):
        """Bedrock: the service resolves its limit into model_kwargs."""
        from langchain_openai import ChatOpenAI
        llm = ChatOpenAI(model="gpt-4o-mini", api_key="x")
        llm = llm.model_copy(update={"model_kwargs": {"max_tokens": 8192}})
        capped = self._svc(llm).with_output_limit(4000)
        self.assertEqual(capped.model_kwargs["max_tokens"], 4000)

    def test_service_model_is_not_changed(self):
        from langchain_openai import ChatOpenAI
        svc = self._svc(ChatOpenAI(model="gpt-4o-mini", api_key="x"))
        svc.with_output_limit(4000)
        self.assertIsNone(svc.llm.max_tokens)

    def test_unknown_model_is_returned_unchanged(self):
        sentinel = types.SimpleNamespace()
        self.assertIs(self._svc(sentinel).with_output_limit(4000), sentinel)

    def test_structured_call_uses_the_capped_model(self):
        seen = {}

        class FakeLLM:
            max_tokens = None
            model_fields = {"max_tokens": None}

            def model_copy(self, update):
                seen.update(update)
                return self

            def with_structured_output(self, schema, **kw):
                return types.SimpleNamespace(invoke=lambda m: Plan())

        svc = self._svc(FakeLLM())
        svc.invoke_structured([("user", "q")], Plan, caller_name="t", max_output_tokens=4000)
        self.assertEqual(seen, {"max_tokens": 4000})


class TestPlannerLimit(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            from agent.agentic_planner import _planner_output_limit
        except ImportError as exc:
            raise unittest.SkipTest(f"app environment unavailable: {exc}")
        cls.limit = staticmethod(_planner_output_limit)

    def _llm(self, service, model):
        return types.SimpleNamespace(config={"llm_service": service, "llm_model": model})

    def test_default(self):
        self.assertEqual(self.limit(self._llm("openai", "gpt-4o-mini"), types.SimpleNamespace(graphrag_cfg={})), 4000)

    def test_setting_overrides(self):
        ctx = types.SimpleNamespace(graphrag_cfg={"planner_max_output_tokens": 6000})
        self.assertEqual(self.limit(self._llm("openai", "gpt-4o-mini"), ctx), 6000)

    def test_reasoning_model_gets_room_to_think(self):
        self.assertEqual(self.limit(self._llm("openai", "o3"), types.SimpleNamespace(graphrag_cfg={})), 16000)


if __name__ == "__main__":
    unittest.main()
