"""The agentic engines decide from the conversation before they retrieve
(GML-2325)."""
import json
import types
import unittest
from unittest import mock

try:
    import agent.agentic_graph as g
    import agent.agentic_synthesizer as syn
    from agent.agentic_planner import plan_question
    from agent.conversation_window import CONVERSATION_CHARS, recent_conversation, recent_turns
    from common.llm_services.base_llm import LLM_Model
    from common.py_schemas import ConversationReview, GraphRAGResponse, Plan, PlanStep, StepResult
except ImportError as exc:  # needs the app environment
    raise unittest.SkipTest(f"app environment unavailable: {exc}")


def _turns(n, size=300):
    return [{"query": f"question {i}", "response": f"answer {i} " + "x" * size} for i in range(n)]


class TestConversationWindow(unittest.TestCase):
    def test_newest_turns_are_kept(self):
        convo = _turns(40)
        kept = recent_turns(convo)
        self.assertEqual(kept[-1]["query"], "question 39")
        self.assertLessEqual(len(json.dumps(kept, ensure_ascii=False)), CONVERSATION_CHARS)
        self.assertEqual(kept, convo[-len(kept):])

    def test_planner_sees_the_newest_turns(self):
        seen = {}

        def invoke(messages, schema, **kw):
            seen["user"] = messages[-1][1]
            return Plan(steps=[PlanStep(id="A", kind="answer", tool="")])

        llm = types.SimpleNamespace(agentic_planner_prompt="sys", config={},
                                    invoke_structured=invoke)
        plan_question(llm, "q", _turns(40))
        self.assertIn("question 39", seen["user"])
        self.assertNotIn('"question 0"', seen["user"])

    def test_triage_name_still_works(self):
        from agent.agentic_agent import _recent_conversation
        self.assertEqual(_recent_conversation(_turns(3)), recent_conversation(_turns(3)))


class _Ctx:
    def __init__(self):
        self.graphrag_cfg = {}
        self.emit = lambda *a, **k: None


def _synth(*a, **k):
    return GraphRAGResponse(natural_language_response="ok",
                            answered_question=True, response_type="agentic")


def _answer_only(**review):
    return Plan(conversation_review=ConversationReview(**review),
                steps=[PlanStep(id="A", kind="answer", tool="")])


_REUSE = dict(facts_asked=["iPhone net sales"], stated_by_good_answer=["iPhone net sales"],
              earlier_answer="good", coverage="all")
_BAD = dict(facts_asked=["iPad net sales"], earlier_answer="bad", coverage="none")
_IPAD_SEARCH = Plan(steps=[PlanStep(id="S1", kind="unstructured", tool="graphrag__hybrid_search",
                                    args={"question": "iPad net sales"}),
                           PlanStep(id="A", kind="answer", tool="", depends_on=["S1"])])
_FOUND = {"S1": StepResult(step_id="S1", ok=True, summary="rows", context={"result": [{"row": 1}]})}


class TestPlanCheck(unittest.TestCase):
    """Every plan retrieves, or its conversation_review justifies answering
    without retrieval; otherwise one replan, then the fallback plan."""

    def _run(self, plans, conversation=None, exec_results=None):
        with mock.patch.object(g, "plan_question", side_effect=plans) as planner, \
             mock.patch.object(g, "execute_plan", return_value=(exec_results if exec_results is not None else _FOUND, [])) as execute, \
             mock.patch.object(g, "synthesize", side_effect=_synth), \
             mock.patch.object(g, "_run_step") as run_step:
            resp = g.run_agentic(_Ctx(), llm=object(), question="Add iPad.",
                                 conversation=_turns(1) if conversation is None else conversation)
        return resp, planner, execute, run_step

    def test_justified_reuse_answers_without_retrieval(self):
        resp, planner, execute, run_step = self._run([_answer_only(**_REUSE)])
        self.assertEqual(planner.call_count, 1)
        execute.assert_not_called()
        run_step.assert_not_called()
        steps = resp.query_sources["agent_steps"]
        self.assertEqual(steps[0]["output"]["routing"], "answered without retrieval")
        self.assertEqual(steps[0]["output"]["conversation_review"]["coverage"], "all")
        self.assertIn("earlier_conversation", steps[-1]["input"])

    def test_no_data_question_answers_without_retrieval(self):
        _, planner, execute, _ = self._run([_answer_only(needs_data=False)])
        self.assertEqual(planner.call_count, 1)
        execute.assert_not_called()

    def test_unjustified_answer_only_plan_is_replanned(self):
        resp, planner, execute, _ = self._run([_answer_only(**_BAD), _IPAD_SEARCH])
        self.assertEqual(planner.call_count, 2)
        self.assertIn("iPad net sales", planner.call_args.kwargs["plan_problem"])
        execute.assert_called_once()
        nodes = [s["node"] for s in resp.query_sources["agent_steps"]]
        self.assertEqual(nodes[:2], ["plan", "replan (incomplete plan)"])
        self.assertEqual(resp.query_sources["agent_steps"][0]["output"]["routing"], "no retrieval steps")

    def test_empty_plan_is_replanned(self):
        """No steps at all (the default review) is never a decision."""
        _, planner, execute, _ = self._run([Plan(), _IPAD_SEARCH], conversation=[])
        self.assertEqual(planner.call_count, 2)
        execute.assert_called_once()

    def test_second_incomplete_plan_uses_the_fallback(self):
        resp, planner, execute, _ = self._run([_answer_only(**_BAD), _answer_only(**_BAD)])
        self.assertEqual(planner.call_count, 2)
        plan = execute.call_args[0][0]
        self.assertEqual([s.tool for s in plan.steps], [g._STRUCTURAL_TOOL, g._HYBRID_TOOL, ""])
        self.assertEqual(plan.steps[0].args, {"question": "Add iPad."})
        self.assertIn("no usable retrieval steps", plan.steps[0].rationale)
        nodes = [s["node"] for s in resp.query_sources["agent_steps"]]
        self.assertEqual(nodes[:3], ["plan", "replan (incomplete plan)", "fallback plan"])

    def test_retrieval_plan_is_used_as_is(self):
        _, planner, execute, _ = self._run([_IPAD_SEARCH])
        self.assertEqual(planner.call_count, 1)
        execute.assert_called_once()

    def test_empty_retrieval_still_replans(self):
        _, planner, execute, _ = self._run([_IPAD_SEARCH] * 10, exec_results={})
        self.assertGreater(planner.call_count, 1)
        self.assertGreater(execute.call_count, 1)

    def test_dropped_steps_are_shown_in_the_trace(self):
        from agent.agentic_planner import _sanitize
        plan = Plan(steps=[PlanStep(id="S1", kind="structural", tool="structural_retrieve")])
        with mock.patch("agent.agentic_planner.registry.tool_names", return_value=[g._STRUCTURAL_TOOL]):
            plan = _sanitize(plan)
        out = g._plan_output(plan)
        self.assertEqual(out["dropped_steps"], [{"id": "S1", "tool": "structural_retrieve"}])
        self.assertEqual(out["routing"], "no retrieval steps")
        self.assertNotIn("_dropped_steps", plan.model_dump())

    def test_planner_is_told_what_was_wrong(self):
        seen = {}

        def invoke(messages, schema, **kw):
            seen["user"] = messages[-1][1]
            return _IPAD_SEARCH

        llm = types.SimpleNamespace(agentic_planner_prompt="sys", config={}, invoke_structured=invoke)
        plan_question(llm, "Add iPad.", _turns(1), plan_problem="The previous plan had no retrieval steps.")
        self.assertIn("## Previous plan\nThe previous plan had no retrieval steps.", seen["user"])


class TestPlanSchema(unittest.TestCase):
    def test_model_must_write_review_steps_and_strategy(self):
        schema = Plan.model_json_schema()
        self.assertEqual(schema["required"], ["conversation_review", "steps", "strategy"])
        self.assertEqual(list(schema["properties"])[:2], ["conversation_review", "steps"])
        review = schema["$defs"]["ConversationReview"]["properties"]
        self.assertEqual(set(review["coverage"]["enum"]), {"all", "part", "none"})

    def test_python_code_can_still_build_plans(self):
        self.assertEqual(Plan().conversation_review.coverage, "none")

    def test_review_values_are_bounded(self):
        rv = ConversationReview(facts_asked=["x" * 500] * 20, coverage="ALL", earlier_answer="unknown")
        self.assertEqual(len(rv.facts_asked), 8)
        self.assertEqual(len(rv.facts_asked[0]), 120)
        self.assertEqual((rv.coverage, rv.earlier_answer), ("all", "none"))


class TestAnswerStepSeesTheConversation(unittest.TestCase):
    def _synthesize(self, results, plan, conversation):
        seen = {}

        class Gen:
            def __init__(self, llm):
                pass

            def generate_answer(self, question, context):
                seen["context"] = context
                return types.SimpleNamespace(generated_answer="a", citation=[])

        with mock.patch.object(syn, "TigerGraphAgentGenerator", Gen):
            resp = syn.synthesize(object(), "q", results, plan=plan, conversation=conversation)
        return resp, seen["context"]

    def test_conversation_goes_last(self):
        results = {"S1": StepResult(step_id="S1", ok=True, summary="rows",
                                    context={"result": [{"row": 1}]})}
        plan = Plan(steps=[PlanStep(id="S1", kind="structural", tool=g._STRUCTURAL_TOOL)])
        resp, ctx = self._synthesize(results, plan, _turns(2))
        self.assertEqual(list(ctx)[-1], "earlier_conversation")
        self.assertEqual(ctx["earlier_conversation"][-1]["query"], "question 1")
        # The recorded result is the retrieved context only.
        self.assertNotIn("earlier_conversation", resp.query_sources["result"])

    def test_answer_only_plan_counts_as_answered(self):
        plan = Plan(steps=[PlanStep(id="A", kind="answer", tool="")])
        resp, _ = self._synthesize({}, plan, _turns(1))
        self.assertTrue(resp.answered_question)

    def test_no_conversation_no_key(self):
        plan = Plan(steps=[PlanStep(id="A", kind="answer", tool="")])
        resp, ctx = self._synthesize({}, plan, [])
        self.assertNotIn("earlier_conversation", ctx)
        self.assertFalse(resp.answered_question)


class TestPrompts(unittest.TestCase):
    def test_planner_and_react_check_the_conversation_first(self):
        react = LLM_Model._AGENTIC_AGENT_SYSTEM
        self.assertIn("list the facts the question asks for", react)
        self.assertIn("Some facts are stated", react)
        planner = LLM_Model._AGENTIC_PLANNER_SYSTEM
        self.assertIn('When coverage is "part", plan retrieval steps only for the missing facts', planner)
        for prompt in (planner, react):
            self.assertIn("A related topic or a similar question is not enough", prompt)
            self.assertIn('"now"', prompt)

    def test_bad_earlier_answers_count_as_not_answered(self):
        planner, react = LLM_Model._AGENTIC_PLANNER_SYSTEM, LLM_Model._AGENTIC_AGENT_SYSTEM
        self.assertIn('Before you plan, write "conversation_review"', planner)
        self.assertIn("uses a different attribute than the question asks for", planner)
        self.assertIn('ONLY when coverage is "all" and earlier_answer is "good"', planner)
        self.assertIn("A bad answer counts as not answered", react)
        for prompt in (planner, react):
            self.assertIn("not found, not available, not explicit", prompt)
            self.assertIn("retrieve again with a different or wider approach", prompt)

    def test_answer_prompt_explains_the_conversation_context(self):
        self.assertIn('"earlier_conversation"', LLM_Model._CHATBOT_RESPONSE_SYSTEM)
        self.assertIn("not evidence that it does not exist", LLM_Model._CHATBOT_RESPONSE_SYSTEM)


if __name__ == "__main__":
    unittest.main()
