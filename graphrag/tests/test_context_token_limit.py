import types
import unittest

try:
    from common.utils.token_calculator import PROMPT_RESERVE_MARGIN, TokenCalculator
except ImportError as exc:  # needs the app environment
    raise unittest.SkipTest(f"app environment unavailable: {exc}")


def _calc(limit):
    return TokenCalculator(token_limit=limit, model_name="gpt-4o-mini")


class TestFitContext(unittest.TestCase):
    """The context is trimmed so the whole prompt fits, by token count (GML-2311)."""

    PROMPT = "Answer the question from the context below.\nQuestion: q\nContext:\n"

    def _budget(self, calc):
        return calc.max_context_tokens - calc.count_tokens(self.PROMPT) - PROMPT_RESERVE_MARGIN

    def test_no_limit_leaves_context_untouched(self):
        text = "word " * 5000
        self.assertEqual(_calc(0).fit_context(text, self.PROMPT), text)

    def test_context_within_budget_is_unchanged(self):
        self.assertEqual(_calc(2000).fit_context("short context", self.PROMPT), "short context")

    def test_reserve_is_the_measured_prompt_not_a_flat_amount(self):
        calc = _calc(2000)
        long_prompt = self.PROMPT + ("rule. " * 600)
        trimmed = calc.fit_context("word " * 5000, long_prompt)
        total = calc.count_tokens(trimmed) + calc.count_tokens(long_prompt)
        self.assertLessEqual(total, 2000 - PROMPT_RESERVE_MARGIN)
        self.assertGreater(calc.count_tokens(trimmed), 0)

    def test_cjk_context_is_trimmed_by_tokens_not_characters(self):
        """Fewer characters than the limit, but more tokens than the budget.
        Non-OpenAI models count with cl100k_base, where Japanese runs over one
        token per character."""
        calc = TokenCalculator(token_limit=1500, model_name="gemini-3.5-flash")
        text = "東京都の書類管理における情報資産" * 80
        self.assertLess(len(text), calc.max_context_tokens)
        self.assertGreater(calc.count_tokens(text), self._budget(calc))
        trimmed = calc.fit_context(text, self.PROMPT)
        self.assertLessEqual(calc.count_tokens(trimmed), self._budget(calc))

    def test_list_of_passages_is_trimmed_in_order(self):
        calc = _calc(1500)
        passages = [f"passage {i}: " + "detail " * 200 for i in range(10)]
        trimmed = calc.fit_context(passages, self.PROMPT)
        self.assertIsInstance(trimmed, list)
        self.assertLess(len(trimmed), len(passages))
        self.assertEqual(trimmed[0], passages[0])
        self.assertLessEqual(sum(calc.count_tokens(p) for p in trimmed), self._budget(calc))

    def test_prompt_larger_than_limit_leaves_no_context(self):
        calc = _calc(300)
        self.assertEqual(calc.fit_context("word " * 500, "rule " * 400), "")
        self.assertEqual(calc.fit_context(["word " * 500], "rule " * 400), [])
        self.assertEqual(calc.fit_context({"a": "word " * 500}, "rule " * 400), {})


class TestContextTokenLimit(unittest.TestCase):
    """Without a configured token_limit, the model's own input limit applies."""

    @classmethod
    def setUpClass(cls):
        try:
            from common.llm_services import base_llm
        except ImportError as exc:
            raise unittest.SkipTest(f"app environment unavailable: {exc}")
        cls.base_llm = base_llm

    def _service(self, config, profile=None):
        svc = object.__new__(self.base_llm.LLM_Model)
        svc.config = config
        svc.llm = types.SimpleNamespace(profile=profile)
        return svc

    def test_configured_limit_wins(self):
        svc = self._service({"token_limit": 50000}, {"max_input_tokens": 1_000_000})
        self.assertEqual(svc.context_token_limit(), 50000)

    def test_profile_limit_less_answer_room_and_margin(self):
        svc = self._service(
            {"llm_model": "m"}, {"max_input_tokens": 128000, "max_output_tokens": 16384}
        )
        self.assertEqual(
            svc.context_token_limit(),
            int(128000 * self.base_llm._TOKENIZER_MARGIN) - self.base_llm._ANSWER_RESERVE_CAP,
        )

    def test_large_output_budgets_reserve_only_a_capped_answer(self):
        """Reasoning models declare 100K+ output budgets; reserving all of it
        left gpt-5 about 35K and o3 about 20K tokens for context."""
        for max_in, max_out in ((272000, 128000), (200000, 100000), (272000, 272000)):
            svc = self._service({"llm_model": "m"},
                                {"max_input_tokens": max_in, "max_output_tokens": max_out})
            self.assertEqual(
                svc.context_token_limit(),
                int(max_in * self.base_llm._TOKENIZER_MARGIN) - self.base_llm._ANSWER_RESERVE_CAP,
            )

    def test_small_window_keeps_at_least_half_for_context(self):
        svc = self._service({"llm_model": "m"},
                            {"max_input_tokens": 8192, "max_output_tokens": 8192})
        budget = int(8192 * self.base_llm._TOKENIZER_MARGIN)
        self.assertEqual(svc.context_token_limit(), budget - budget // 2)

    def test_unknown_model_keeps_no_limit_and_warns_once(self):
        self.base_llm._warned_no_token_limit.discard("custom-model")
        svc = self._service({"llm_model": "custom-model", "token_limit": 0}, None)
        with self.assertLogs(self.base_llm.logger, level="WARNING") as logs:
            self.assertEqual(svc.context_token_limit(), 0)
            self.assertEqual(svc.context_token_limit(), 0)
            self.base_llm.logger.warning("sentinel")
        self.assertEqual(sum("custom-model" in line for line in logs.output), 1)

    def test_real_openai_profile_is_read_offline(self):
        try:
            from langchain_openai import ChatOpenAI
        except ImportError as exc:
            self.skipTest(f"langchain-openai unavailable: {exc}")
        svc = self._service({"llm_model": "gpt-4o-mini"})
        svc.llm = ChatOpenAI(model="gpt-4o-mini", api_key="sk-test")
        self.assertGreater(svc.context_token_limit(), 50000)


class TestAnswerGenerationTrimsContext(unittest.TestCase):
    """The answer prompt sent to the model fits the limit, prompt included."""

    @classmethod
    def setUpClass(cls):
        try:
            from agent.agent_generation import TigerGraphAgentGenerator
        except ImportError as exc:
            raise unittest.SkipTest(f"app environment unavailable: {exc}")
        cls.Generator = TigerGraphAgentGenerator

    def test_sent_context_fits_with_the_prompt(self):
        seen = {}

        class FakeService:
            config = {"llm_model": "gpt-4o-mini"}
            chatbot_response_prompt = (
                "Rules " * 300
                + "\n{format_instructions}\nQuestion: {question}\nQuery: {query}\nContext: {context}"
            )

            def context_token_limit(self):
                return 3000

            @staticmethod
            def _salvage_answer_output(raw):
                return raw

            def invoke_with_parser(self, prompt, parser, inputs, **kwargs):
                seen["prompt"] = prompt.format(**inputs)
                seen["context"] = inputs["context"]
                return "ok"

        gen = self.Generator(FakeService())
        gen.generate_answer("What is X?", "fact " * 10000)
        calc = gen.token_calculator
        self.assertLess(calc.count_tokens(seen["context"]), calc.count_tokens("fact " * 10000))
        self.assertLessEqual(calc.count_tokens(seen["prompt"]), 3000)

    def test_prompt_that_cannot_render_gets_the_fallback_answer(self):
        class BracesService:
            config = {"llm_model": "gpt-4o-mini"}
            # A custom prompt with stray braces cannot be rendered.
            chatbot_response_prompt = "{format_instructions}\n{question}\n{query}\n{context}\nUse {oops}"

            def context_token_limit(self):
                return 3000

            @staticmethod
            def _salvage_answer_output(raw):
                return raw

            def invoke_with_parser(self, *args, **kwargs):
                raise AssertionError("not reached")

        answer = self.Generator(BracesService()).generate_answer("q", "ctx")
        self.assertIn("I wasn't able to generate an answer", answer.generated_answer)

    def test_failure_log_names_the_provider_error(self):
        class FailingService:
            config = {"llm_model": "gpt-4o-mini"}
            chatbot_response_prompt = "{format_instructions}\n{question}\n{query}\n{context}"

            def context_token_limit(self):
                return 0

            @staticmethod
            def _salvage_answer_output(raw):
                return raw

            def invoke_with_parser(self, *args, **kwargs):
                raise ValueError("400 INVALID_ARGUMENT: request contains an invalid argument")

        import logging
        gen = self.Generator(FailingService())
        with self.assertLogs("agent.agent_generation", level=logging.WARNING) as logs:
            answer = gen.generate_answer("q", "ctx")
        self.assertIn("I wasn't able to generate an answer", answer.generated_answer)
        self.assertTrue(any(
            "ValueError: 400 INVALID_ARGUMENT: request contains an invalid argument" in line
            for line in logs.output
        ))


if __name__ == "__main__":
    unittest.main()
