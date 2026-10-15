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
    from common.py_schemas import ConversationCheck, GraphRAGResponse, Plan, PlanStep, StepResult
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


class TestAnswerOnlyPlan(unittest.TestCase):
    def _run(self, plans, check=ConversationCheck(needs_retrieval=False)):
        with mock.patch.object(g, "check_conversation", return_value=check), \
             mock.patch.object(g, "plan_question", side_effect=plans) as planner, \
             mock.patch.object(g, "execute_plan", return_value=({}, [])) as execute, \
             mock.patch.object(g, "synthesize", side_effect=_synth), \
             mock.patch.object(g, "_run_step") as run_step:
            resp = g.run_agentic(_Ctx(), llm=object(), question="q", conversation=_turns(1))
        return resp, planner, execute, run_step

    def test_answer_only_plan_skips_retrieval_and_replanning(self):
        answer_only = Plan(steps=[PlanStep(id="A", kind="answer", tool="")])
        resp, planner, execute, run_step = self._run([answer_only])
        self.assertEqual(planner.call_count, 1)
        execute.assert_not_called()
        run_step.assert_not_called()
        steps = resp.query_sources["agent_steps"]
        self.assertEqual(steps[0]["output"]["routing"], "answered without retrieval")
        self.assertIn("earlier_conversation", steps[-1]["input"])

    def test_empty_retrieval_still_replans(self):
        search = Plan(steps=[PlanStep(id="S1", kind="unstructured", tool=g._HYBRID_TOOL),
                             PlanStep(id="A", kind="answer", tool="", depends_on=["S1"])])
        _, planner, execute, _ = self._run([search] * 10)
        self.assertGreater(planner.call_count, 1)
        self.assertGreater(execute.call_count, 1)

    def test_trace_marks_the_decision(self):
        answer_only = Plan(steps=[PlanStep(id="A", kind="answer", tool="")])
        self.assertEqual(g._plan_output(answer_only)["routing"], "answered without retrieval")
        search = Plan(steps=[PlanStep(id="S1", kind="unstructured", tool=g._HYBRID_TOOL)])
        self.assertNotIn("routing", g._plan_output(search))


_ANSWER_ONLY = Plan(steps=[PlanStep(id="A", kind="answer", tool="")])
_IPAD_SEARCH = Plan(steps=[PlanStep(id="S1", kind="unstructured", tool="graphrag__hybrid_search",
                                    args={"question": "iPad net sales"}),
                           PlanStep(id="A", kind="answer", tool="", depends_on=["S1"])])
_FOUND = {"S1": StepResult(step_id="S1", ok=True, summary="rows", context={"result": [{"row": 1}]})}


class TestConversationCheck(unittest.TestCase):
    """A plan that skips retrieval gets a second check."""

    def _run(self, plans, check=None, conversation=None, check_side_effect=None):
        with mock.patch.object(g, "check_conversation", return_value=check,
                               side_effect=check_side_effect) as checker, \
             mock.patch.object(g, "plan_question", side_effect=plans) as planner, \
             mock.patch.object(g, "execute_plan", return_value=(_FOUND, [])) as execute, \
             mock.patch.object(g, "synthesize", side_effect=_synth):
            resp = g.run_agentic(_Ctx(), llm=object(), question="Add iPad.",
                                 conversation=_turns(1) if conversation is None else conversation)
        return resp, checker, planner, execute

    def test_missing_fact_replans_with_the_gap(self):
        check = ConversationCheck(missing="iPad net sales", needs_retrieval=True)
        resp, checker, planner, execute = self._run([_ANSWER_ONLY, _IPAD_SEARCH], check)
        self.assertEqual(planner.call_count, 2)
        self.assertEqual(planner.call_args.kwargs["missing_from_conversation"], "iPad net sales")
        execute.assert_called_once()
        nodes = [s["node"] for s in resp.query_sources["agent_steps"]]
        self.assertEqual(nodes[:3], ["plan", "conversation check", "replan (conversation check)"])

    def test_second_skip_searches_anyway(self):
        check = ConversationCheck(missing="iPhone net sales", needs_retrieval=True)
        _, _, planner, execute = self._run([_ANSWER_ONLY, _ANSWER_ONLY], check)
        self.assertEqual(planner.call_count, 2)
        execute.assert_called_once()
        plan = execute.call_args[0][0]
        self.assertEqual([s.tool for s in plan.steps], [g._HYBRID_TOOL, ""])
        self.assertEqual(plan.steps[0].args, {"question": "Add iPad."})

    def test_confirmed_plan_answers_without_retrieval(self):
        _, _, planner, execute = self._run([_ANSWER_ONLY], ConversationCheck(needs_retrieval=False))
        self.assertEqual(planner.call_count, 1)
        execute.assert_not_called()

    def test_failed_check_keeps_the_plan(self):
        _, _, planner, execute = self._run([_ANSWER_ONLY], None)
        self.assertEqual(planner.call_count, 1)
        execute.assert_not_called()

    def test_check_runs_without_conversation(self):
        """A data question planned without retrieval still gets retrieval."""
        check = ConversationCheck(missing="iPhone net sales", needs_retrieval=True)
        _, checker, planner, execute = self._run([_ANSWER_ONLY, _IPAD_SEARCH], check, conversation=[])
        checker.assert_called_once()
        self.assertEqual(planner.call_count, 2)
        execute.assert_called_once()

    def test_no_check_for_a_retrieval_plan(self):
        _, checker, _, execute = self._run([_IPAD_SEARCH])
        checker.assert_not_called()
        execute.assert_called_once()

    def test_check_errors_keep_the_plan(self):
        from agent.agentic_planner import check_conversation
        self.assertIsNone(check_conversation(object(), "q", _turns(1)))

    def test_planner_is_told_what_is_missing(self):
        seen = {}

        def invoke(messages, schema, **kw):
            seen["user"] = messages[-1][1]
            return _IPAD_SEARCH

        llm = types.SimpleNamespace(agentic_planner_prompt="sys", config={}, invoke_structured=invoke)
        plan_question(llm, "Add iPad.", _turns(1), missing_from_conversation="iPad net sales")
        self.assertIn("## Conversation does not state: iPad net sales", seen["user"])

    def test_check_prompt_covers_fresh_data_requests(self):
        self.assertIn('"now"', LLM_Model._AGENTIC_CONVERSATION_CHECK_SYSTEM)


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
        for prompt in (LLM_Model._AGENTIC_PLANNER_SYSTEM, LLM_Model._AGENTIC_AGENT_SYSTEM):
            self.assertIn("list the facts the question asks for", prompt)
            self.assertIn("Some facts are stated", prompt)
            self.assertIn("A related topic or a similar question is not enough", prompt)
            self.assertIn('"now"', prompt)

    def test_answer_prompt_explains_the_conversation_context(self):
        self.assertIn('"earlier_conversation"', LLM_Model._CHATBOT_RESPONSE_SYSTEM)


if __name__ == "__main__":
    unittest.main()
