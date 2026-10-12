import json
import unittest


class TestClarifyBeforePlanning(unittest.TestCase):
    """An unclear follow-up is clarified at triage, which can reply to the
    user; the planner's output is a plan, so it cannot ask."""

    @classmethod
    def setUpClass(cls):
        try:
            from common.llm_services.base_llm import LLM_Model
            from agent.agentic_agent import _Triage
        except ImportError as exc:  # needs the app environment
            raise unittest.SkipTest(f"app environment unavailable: {exc}")
        cls.M, cls.Triage = LLM_Model, _Triage

    def test_triage_policy_asks_on_an_unclear_follow_up(self):
        policy = self.M._AGENTIC_TRIAGE_USER_DEFAULT
        self.assertIn("UNCLEAR FOLLOW-UP", policy)
        self.assertIn("clarifying question", policy)
        self.assertIn("clarifying question", self.M._AGENTIC_TRIAGE_SYSTEM)

    def test_triage_output_allows_a_question_instead_of_retrieval(self):
        fields = self.Triage.model_fields
        self.assertIn("clarifying question", fields["answer"].description)
        self.assertIn("too unclear", fields["needs_retrieval"].description)

    def test_planner_no_longer_plans_a_question(self):
        planner = self.M._AGENTIC_PLANNER_USER_DEFAULT
        self.assertNotIn("plan only a final answer step (no retrieval)", planner)
        self.assertIn("use the most recent one", planner)



class TestTriageSeesTheLatestTurns(unittest.TestCase):
    """Follow-ups refer to the latest turns, so those are what triage gets."""

    @classmethod
    def setUpClass(cls):
        try:
            from agent.agentic_agent import _recent_conversation, _TRIAGE_CONVO_CHARS
        except ImportError as exc:
            raise unittest.SkipTest(f"app environment unavailable: {exc}")
        cls.recent, cls.limit = staticmethod(_recent_conversation), _TRIAGE_CONVO_CHARS

    @staticmethod
    def _turn(i, size=1500):
        return {"query": f"question {i}", "response": f"answer {i} " + "x" * size}

    def test_short_conversation_is_sent_whole(self):
        convo = [self._turn(1, 100), self._turn(2, 100)]
        self.assertEqual(json.loads(self.recent(convo)), convo)

    def test_long_conversation_keeps_the_newest_turns_in_order(self):
        convo = [self._turn(i) for i in range(1, 8)]
        sent = self.recent(convo)
        self.assertLessEqual(len(sent), self.limit)
        kept = json.loads(sent)
        self.assertEqual(kept, convo[-len(kept):])
        self.assertEqual(kept[-1]["query"], "question 7")
        self.assertGreaterEqual(len(kept), 2)

    def test_oversized_newest_turn_keeps_its_question(self):
        convo = [self._turn(1), {"query": "the latest question", "response": 'say "hi"\n' * 2000}]
        sent = self.recent(convo)
        self.assertLessEqual(len(sent), self.limit)
        kept = json.loads(sent)
        self.assertEqual([t["query"] for t in kept], ["the latest question"])

    def test_non_english_text_is_counted_as_written(self):
        convo = [{"query": "質問", "response": "日" * 1500}]
        self.assertIn("日" * 1500, self.recent(convo))

    def test_empty_conversation(self):
        self.assertEqual(self.recent([]), "[]")
        self.assertEqual(self.recent(None), "[]")


if __name__ == "__main__":
    unittest.main()
