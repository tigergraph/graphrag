# Copyright (c) 2024-2026 TigerGraph, Inc.
#
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0).
# See the LICENSE file or <https://www.gnu.org/licenses/agpl-3.0.html>
# for the full license text.

import asyncio
import json
import re
import unittest
from unittest.mock import MagicMock, patch, AsyncMock

from langchain_core.exceptions import OutputParserException
from langchain_core.output_parsers import PydanticOutputParser, StrOutputParser
from langchain_core.prompts import PromptTemplate
from pydantic import BaseModel, Field

from common.llm_services.base_llm import LLM_Model


class SampleResponse(BaseModel):
    answer: str = Field(description="The answer")
    score: int = Field(description="Quality score")


def _make_llm_model():
    """Create an LLM_Model with a mocked LLM."""
    config = {"prompt_path": ""}
    model = LLM_Model(config)
    model.llm = MagicMock()
    return model


def _make_prompt():
    return PromptTemplate(
        template="Question: {question}\n{format_instructions}",
        input_variables=["question"],
        partial_variables={
            "format_instructions": PydanticOutputParser(
                pydantic_object=SampleResponse
            ).get_format_instructions()
        },
    )


def _setup_chain_mock(model, raw_content, mock_cb_ctx, async_mode=False):
    """Set up mock chain invocation with callback context."""
    mock_output = MagicMock()
    mock_output.content = raw_content

    mock_cb = MagicMock()
    mock_cb.prompt_tokens = 10
    mock_cb.completion_tokens = 5
    mock_cb.total_tokens = 15
    mock_cb.total_cost = 0.001
    mock_cb_ctx.return_value.__enter__ = MagicMock(return_value=mock_cb)
    mock_cb_ctx.return_value.__exit__ = MagicMock(return_value=False)

    mock_chain = MagicMock()
    if async_mode:
        mock_chain.ainvoke = AsyncMock(return_value=mock_output)
    else:
        mock_chain.invoke.return_value = mock_output

    # patch prompt.__or__ so that (prompt | self.llm) returns mock_chain
    return mock_chain


class TestInvokeWithParser(unittest.TestCase):
    """Tests for LLM_Model.invoke_with_parser."""

    @patch("common.llm_services.base_llm.get_openai_callback")
    def test_clean_json_parses_directly(self, mock_cb_ctx):
        """When LLM returns clean JSON, parser succeeds on first attempt."""
        model = _make_llm_model()
        prompt = _make_prompt()
        parser = PydanticOutputParser(pydantic_object=SampleResponse)

        raw_json = '{"answer": "hello", "score": 95}'
        mock_chain = _setup_chain_mock(model, raw_json, mock_cb_ctx)

        with patch.object(type(prompt), "__or__", return_value=mock_chain):
            result = model.invoke_with_parser(
                prompt, parser, {"question": "test"}, caller_name="test_clean"
            )
        self.assertEqual(result.answer, "hello")
        self.assertEqual(result.score, 95)

    @patch("common.llm_services.base_llm.get_openai_callback")
    def test_preamble_text_falls_back_to_regex(self, mock_cb_ctx):
        """When LLM wraps JSON with preamble text, regex fallback extracts it."""
        model = _make_llm_model()
        prompt = _make_prompt()
        parser = PydanticOutputParser(pydantic_object=SampleResponse)

        raw_text = 'Here is the result:\n{"answer": "world", "score": 80}\nHope this helps!'
        mock_chain = _setup_chain_mock(model, raw_text, mock_cb_ctx)

        with patch.object(type(prompt), "__or__", return_value=mock_chain):
            result = model.invoke_with_parser(
                prompt, parser, {"question": "test"}, caller_name="test_preamble"
            )
        self.assertEqual(result.answer, "world")
        self.assertEqual(result.score, 80)

    @patch("common.llm_services.base_llm.get_openai_callback")
    def test_no_json_raises_exception(self, mock_cb_ctx):
        """When LLM returns no JSON at all, raises OutputParserException."""
        model = _make_llm_model()
        prompt = _make_prompt()
        parser = PydanticOutputParser(pydantic_object=SampleResponse)

        mock_chain = _setup_chain_mock(
            model, "I cannot answer this question.", mock_cb_ctx
        )

        with patch.object(type(prompt), "__or__", return_value=mock_chain):
            with self.assertRaises(OutputParserException):
                model.invoke_with_parser(
                    prompt, parser, {"question": "test"}, caller_name="test_no_json"
                )

    @patch("common.llm_services.base_llm.get_openai_callback")
    def test_str_output_parser(self, mock_cb_ctx):
        """StrOutputParser returns raw text without JSON parsing."""
        model = _make_llm_model()
        prompt = PromptTemplate(
            template="Generate query for: {question}",
            input_variables=["question"],
        )
        parser = StrOutputParser()

        raw_text = "SELECT * FROM table WHERE id = 1"
        mock_chain = _setup_chain_mock(model, raw_text, mock_cb_ctx)

        with patch.object(type(prompt), "__or__", return_value=mock_chain):
            result = model.invoke_with_parser(
                prompt, parser, {"question": "test"}, caller_name="test_str"
            )
        self.assertEqual(result, "SELECT * FROM table WHERE id = 1")

    @patch("common.llm_services.base_llm.get_openai_callback")
    def test_usage_tracking_logged(self, mock_cb_ctx):
        """Token usage is logged via get_openai_callback."""
        model = _make_llm_model()
        prompt = PromptTemplate(
            template="{question}", input_variables=["question"]
        )
        parser = StrOutputParser()

        mock_chain = _setup_chain_mock(model, "result", mock_cb_ctx)

        with patch("common.llm_services.base_llm.logger") as mock_logger:
            with patch.object(type(prompt), "__or__", return_value=mock_chain):
                model.invoke_with_parser(
                    prompt, parser, {"question": "test"}, caller_name="test_usage"
                )
            log_calls = [str(c) for c in mock_logger.info.call_args_list]
            self.assertTrue(
                any("test_usage usage" in c for c in log_calls),
                f"Expected usage log, got: {log_calls}",
            )


class TestAinvokeWithParser(unittest.TestCase):
    """Tests for LLM_Model.ainvoke_with_parser."""

    @patch("common.llm_services.base_llm.get_openai_callback")
    def test_async_clean_json(self, mock_cb_ctx):
        """Async version parses clean JSON correctly."""
        model = _make_llm_model()
        prompt = _make_prompt()
        parser = PydanticOutputParser(pydantic_object=SampleResponse)

        raw_json = '{"answer": "async_hello", "score": 90}'
        mock_chain = _setup_chain_mock(model, raw_json, mock_cb_ctx, async_mode=True)

        with patch.object(type(prompt), "__or__", return_value=mock_chain):
            result = asyncio.new_event_loop().run_until_complete(
                model.ainvoke_with_parser(
                    prompt, parser, {"question": "test"}, caller_name="test_async"
                )
            )
        self.assertEqual(result.answer, "async_hello")
        self.assertEqual(result.score, 90)

    @patch("common.llm_services.base_llm.get_openai_callback")
    def test_async_regex_fallback(self, mock_cb_ctx):
        """Async version falls back to regex extraction on preamble."""
        model = _make_llm_model()
        prompt = _make_prompt()
        parser = PydanticOutputParser(pydantic_object=SampleResponse)

        raw_text = 'Sure!\n{"answer": "async_world", "score": 75}'
        mock_chain = _setup_chain_mock(model, raw_text, mock_cb_ctx, async_mode=True)

        with patch.object(type(prompt), "__or__", return_value=mock_chain):
            result = asyncio.new_event_loop().run_until_complete(
                model.ainvoke_with_parser(
                    prompt,
                    parser,
                    {"question": "test"},
                    caller_name="test_async_fallback",
                )
            )
        self.assertEqual(result.answer, "async_world")
        self.assertEqual(result.score, 75)


def _parse_json_output(content: str) -> dict:
    """Standalone copy of LLMEntityRelationshipExtractor._parse_json_output
    for testing without importing the full extractor dependency chain."""
    try:
        return json.loads(content.strip("content="))
    except (json.JSONDecodeError, ValueError):
        pass
    if "```json" in content:
        try:
            return json.loads(
                content.split("```")[1].strip("```").strip("json").strip()
            )
        except (json.JSONDecodeError, ValueError, IndexError):
            pass
    match = re.search(r'\{[\s\S]*\}', content)
    if match:
        return json.loads(match.group())
    raise ValueError(f"Could not extract JSON from LLM output: {content[:200]}")


class TestParseJsonOutput(unittest.TestCase):
    """Tests for the _parse_json_output fallback logic
    (same algorithm used in LLMEntityRelationshipExtractor)."""

    def test_direct_json(self):
        result = _parse_json_output('{"nodes": [], "rels": []}')
        self.assertEqual(result, {"nodes": [], "rels": []})

    def test_json_code_fence(self):
        text = 'Here is the output:\n```json\n{"nodes": [{"id": "A", "node_type": "Person", "definition": "test"}], "rels": []}\n```'
        result = _parse_json_output(text)
        self.assertEqual(len(result["nodes"]), 1)
        self.assertEqual(result["nodes"][0]["id"], "A")

    def test_preamble_regex_fallback(self):
        text = 'Based on the input, I extracted:\n{"nodes": [], "rels": [{"source": "A", "target": "B", "relation_type": "knows", "definition": ""}]}'
        result = _parse_json_output(text)
        self.assertEqual(len(result["rels"]), 1)

    def test_nested_json(self):
        text = '{"nodes": [{"id": "X", "node_type": "Org", "definition": "a company"}], "rels": [{"source": {"id": "X"}, "target": "Y", "relation_type": "owns", "definition": ""}]}'
        result = _parse_json_output(text)
        self.assertEqual(result["nodes"][0]["id"], "X")
        self.assertIsInstance(result["rels"][0]["source"], dict)

    def test_no_json_raises(self):
        with self.assertRaises((ValueError, json.JSONDecodeError)):
            _parse_json_output("No JSON content here at all")

    def test_preamble_before_code_fence(self):
        text = 'Let me extract the entities for you:\n```json\n{"nodes": [{"id": "B", "node_type": "City", "definition": "a city"}], "rels": []}\n```\nDone!'
        result = _parse_json_output(text)
        self.assertEqual(result["nodes"][0]["id"], "B")


class TestSalvageAnswerOutput(unittest.TestCase):
    """Tests for LLM_Model._salvage_answer_output — recovering a usable answer
    from malformed model JSON instead of dumping raw context or a blob."""

    def test_recovers_answer_with_bad_escape(self):
        """An illegal \\' escape that defeats strict parsing still yields the
        prose answer (and an intact citation list)."""
        raw = '{"generated_answer": "The team\\\'s revenue rose 12%.", "citation": ["chunk_1", "chunk_2"]}'
        out = LLM_Model._salvage_answer_output(raw)
        self.assertIn("revenue rose 12%", out.generated_answer)
        self.assertEqual(out.citation, ["chunk_1", "chunk_2"])

    def test_recovers_answer_drops_broken_citation(self):
        """When the citation array is unrecoverable, keep the answer, lose the
        list (acceptable per requirement)."""
        raw = '{"generated_answer": "Plain answer text.", "citation": [oops broken'
        out = LLM_Model._salvage_answer_output(raw)
        self.assertEqual(out.generated_answer, "Plain answer text.")
        self.assertEqual(out.citation, [])

    def test_raw_text_is_last_resort_answer(self):
        """With no recognizable JSON, the raw model text becomes the answer —
        never the retrieved context, never a blank."""
        raw = "Here is the answer in plain prose, no JSON at all."
        out = LLM_Model._salvage_answer_output(raw)
        self.assertEqual(out.generated_answer, raw)
        self.assertEqual(out.citation, [])

    def test_empty_input_safe(self):
        out = LLM_Model._salvage_answer_output("   ")
        self.assertEqual(out.generated_answer, "(no answer produced)")
        self.assertEqual(out.citation, [])


class TestOnParseErrorHook(unittest.TestCase):
    """invoke_with_parser salvages via on_parse_error instead of raising."""

    @patch("common.llm_services.base_llm.get_openai_callback")
    def test_on_parse_error_salvages(self, mock_cb_ctx):
        model = _make_llm_model()
        prompt = _make_prompt()
        parser = PydanticOutputParser(pydantic_object=SampleResponse)

        # Unparseable for SampleResponse; the hook returns a sentinel object.
        mock_chain = _setup_chain_mock(model, "totally not json", mock_cb_ctx)
        sentinel = object()

        with patch.object(type(prompt), "__or__", return_value=mock_chain):
            result = model.invoke_with_parser(
                prompt, parser, {"question": "test"},
                caller_name="test_salvage",
                on_parse_error=lambda raw: sentinel,
            )
        self.assertIs(result, sentinel)


if __name__ == "__main__":
    unittest.main()
