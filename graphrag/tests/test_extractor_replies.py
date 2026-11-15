"""Entity extraction reads the reply of every supported provider: the
content shapes LangChain returns per provider, and the JSON wrappings and
small mistakes models make."""
import asyncio
import json
import unittest
from types import SimpleNamespace

try:
    from common.extractors.LLMEntityRelationshipExtractor import LLMEntityRelationshipExtractor
    from common.llm_services.base_llm import LLM_Model
except ImportError as exc:  # needs the app environment
    raise unittest.SkipTest(f"app environment unavailable: {exc}")

PAYLOAD = {
    "nodes": [{"id": "Acme Corp", "node_type": "Company", "definition": "a company"}],
    "rels": [{"source": {"id": "Acme Corp", "node_type": "Company"},
              "target": {"id": "Widget", "node_type": "Product"},
              "relation_type": "MAKES", "definition": "makes"}],
    "summary": "",
}
JSON = json.dumps(PAYLOAD)

# Reply content as LangChain returns it for each provider.
PROVIDER_REPLIES = {
    "openai_chat": JSON,
    "openai_responses": [{"type": "output_text", "text": JSON, "annotations": []}],
    "anthropic_thinking": [
        {"type": "thinking", "thinking": "Let me find the entities.", "signature": "abc"},
        {"type": "text", "text": JSON},
    ],
    "gemini_3": [{"type": "text", "text": f"```json\n{JSON}\n```", "extras": {"signature": "xyz"}}],
    "gemini_untyped_part": [{"text": JSON}],
    "gemini_string_parts": [f"```json\n{JSON[:40]}", f"{JSON[40:]}\n```"],
    "bedrock_reasoning": [
        {"type": "reasoning_content", "reasoning_content": {"text": "thinking..."}},
        {"type": "text", "text": JSON},
    ],
    "ollama_prose": f"Here are the entities I found:\n{JSON}\nLet me know if you need more.",
}

# Wrappings and small mistakes in the JSON text itself.
TEXT_VARIANTS = {
    "fence_without_language": f"```\n{JSON}\n```",
    "fence_upper_case": f"```JSON\n{JSON}\n```",
    # \( and \d are not valid JSON escapes.
    "invalid_escape": JSON.replace('"a company"', '"a company \\(see C:\\docs)"'),
    "trailing_comma": JSON[:-1] + ",}",
    "trailing_comma_in_list": JSON.replace('}], "rels"', '},], "rels"'),
}


def _extractor():
    ext = LLMEntityRelationshipExtractor.__new__(LLMEntityRelationshipExtractor)
    ext.llm_service = SimpleNamespace(config={}, _message_text=LLM_Model._message_text)
    ext.strict_mode = False
    return ext


def _extract(content):
    out = SimpleNamespace(content=content, usage_metadata=None)

    class _Chain:
        async def ainvoke(self, _):
            return out

        def invoke(self, _):
            return out

    parser = SimpleNamespace(get_format_instructions=lambda: "")
    ext = _extractor()
    a = asyncio.run(ext._aextract_kg_from_doc("text", _Chain(), parser))
    s = ext._extract_kg_from_doc("text", _Chain(), parser)
    return a, s


class TestProviderReplies(unittest.TestCase):
    def test_every_provider_reply_yields_the_entities(self):
        for name, content in PROVIDER_REPLIES.items():
            with self.subTest(provider=name):
                for docs in _extract(content):  # async and sync paths
                    self.assertEqual([n.id for n in docs[0].nodes], ["Acme Corp"], name)
                    self.assertEqual(len(docs[0].relationships), 1, name)

    def test_wrappings_and_small_json_mistakes_are_read(self):
        for name, text in TEXT_VARIANTS.items():
            with self.subTest(variant=name):
                docs, _ = _extract(text)
                self.assertEqual([n.id for n in docs[0].nodes], ["Acme Corp"], name)

    def test_an_unreadable_reply_is_logged_not_silent(self):
        with self.assertLogs("common.extractors.LLMEntityRelationshipExtractor", level="WARNING") as logs:
            docs, _ = _extract("I could not find any entities.")
        self.assertEqual(docs[0].nodes, [])
        self.assertIn("could not be read", "\n".join(logs.output))


class TestReplyText(unittest.TestCase):
    """The shared reply-text reader, also used for image descriptions."""

    def test_text_parts_are_kept_and_thinking_parts_dropped(self):
        for name, content in PROVIDER_REPLIES.items():
            with self.subTest(provider=name):
                text = LLM_Model._message_text(SimpleNamespace(content=content))
                self.assertIsInstance(text, str)
                self.assertNotIn("thinking", text.lower().split("```")[0][:20])
                self.assertIn('"Acme Corp"', text)

    def test_image_description_is_plain_text(self):
        reply = SimpleNamespace(content=[
            {"type": "thinking", "thinking": "Looking at the chart."},
            {"type": "text", "text": "A bar chart of quarterly revenue."},
        ])
        self.assertEqual(LLM_Model._message_text(reply), "A bar chart of quarterly revenue.")


class TestSeveralObjects(unittest.TestCase):
    """A reply can hold several JSON objects; those with nodes or rels are
    combined."""

    def _parse(self, text):
        return _extractor()._parse_json_output(text)

    def test_nodes_and_rels_in_separate_objects_are_combined(self):
        nodes = json.dumps({"nodes": PAYLOAD["nodes"]})
        rels = json.dumps({"rels": PAYLOAD["rels"], "summary": "S"})
        out = self._parse(f"Entities:\n{nodes}\nRelationships:\n{rels}")
        self.assertEqual([n["id"] for n in out["nodes"]], ["Acme Corp"])
        self.assertEqual(len(out["rels"]), 1)
        self.assertEqual(out["summary"], "S")

    def test_duplicates_across_objects_are_kept_once(self):
        a = json.dumps({"nodes": [{"id": "Acme Corp", "node_type": "Company"}], "rels": PAYLOAD["rels"]})
        b = json.dumps({"nodes": [{"id": "acme corp", "node_type": "company"},
                                  {"id": "Widget", "node_type": "Product"}], "rels": PAYLOAD["rels"]})
        out = self._parse(f"```json\n{a}\n```\n```json\n{b}\n```")
        self.assertEqual([n["id"] for n in out["nodes"]], ["Acme Corp", "Widget"])
        self.assertEqual(len(out["rels"]), 1)

    def test_an_echoed_schema_is_not_taken_as_the_answer(self):
        schema = json.dumps({"properties": {"nodes": {"type": "array"}}, "required": ["nodes"]})
        out = self._parse(f"The format is {schema}. My answer: {JSON}")
        self.assertEqual([n["id"] for n in out["nodes"]], ["Acme Corp"])

    def test_one_object_with_an_invalid_escape_is_still_combined(self):
        bad = json.dumps({"nodes": [{"id": "Widget", "node_type": "Product",
                                     "definition": "see C:DOCS"}]}).replace("C:DOCS", "C:\\docs")
        good = json.dumps({"nodes": [{"id": "Acme Corp", "node_type": "Company"}]})
        out = self._parse(f"{good}\n{bad}")
        self.assertEqual(sorted(n["id"] for n in out["nodes"]), ["Acme Corp", "Widget"])

    def test_without_extraction_objects_the_first_object_is_returned(self):
        self.assertEqual(self._parse('note {"a": 1} and {"b": 2}'), {"a": 1})
