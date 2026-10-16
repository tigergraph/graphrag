"""A question without an answer tells the user how to ask again."""
import unittest

try:
    from common.utils.user_messages import ANSWER_FAILED, NO_ANSWER, RETRY_HINT
    from common.llm_services.base_llm import LLM_Model
    from routers import ui
except Exception as exc:  # needs the app environment
    raise unittest.SkipTest(f"app environment unavailable: {exc}")


class TestNoAnswerMessages(unittest.TestCase):
    def test_messages_ask_for_more_specific_information(self):
        for message in (NO_ANSWER, ANSWER_FAILED):
            self.assertIn("more specific information or a narrower scope", message)
        self.assertIn("rephrase your question with more specific information",
                      ui._agent_error_text(RuntimeError("x")))

    def test_messages_do_not_expose_internals(self):
        for message in (NO_ANSWER, ANSWER_FAILED, ui._agent_error_text(RuntimeError("x"))):
            for word in ("schema mapping", "search)", "corpus", "context"):
                self.assertNotIn(word, message.lower() if word != "search)" else message)

    def test_answer_prompts_ask_to_rephrase_when_not_answered(self):
        self.assertIn("ask the user to rephrase the question with more specific information",
                      LLM_Model._CHATBOT_RESPONSE_SYSTEM)
        self.assertIn("When the tool results do not answer the question",
                      LLM_Model._AGENTIC_AGENT_SYSTEM)


if __name__ == "__main__":
    unittest.main()
