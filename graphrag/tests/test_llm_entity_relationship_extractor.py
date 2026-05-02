# Copyright (c) 2024-2026 TigerGraph, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0

"""Unit tests for the schema-aware extension of
``LLMEntityRelationshipExtractor`` — definitions are surfaced in the
prompt without breaking the legacy ``allowed_*_types`` arguments.

We do not invoke the LLM. The tests exercise prompt assembly in
``adocument_er_extraction`` / ``document_er_extraction`` by capturing
the messages passed to ``ChatPromptTemplate.from_messages``.
"""

from __future__ import annotations

from common.extractors.LLMEntityRelationshipExtractor import (
    LLMEntityRelationshipExtractor,
)


class _StubLLMService:
    entity_relationship_extraction_prompt = "(system prompt)"
    llm = object()


def _capture_prompt_messages(monkeypatch):
    """Patch ChatPromptTemplate.from_messages so we can inspect what
    messages the extractor assembles. Returns the captured list.
    """
    captured: list = []

    class _FakeTemplate:
        def __init__(self, msgs):
            self._msgs = msgs

        def __or__(self, other):
            return self  # short-circuit chain composition

    def _from_messages(msgs):
        captured.extend(msgs)
        return _FakeTemplate(msgs)

    monkeypatch.setattr(
        "langchain.prompts.ChatPromptTemplate.from_messages", _from_messages
    )
    return captured


def test_extractor_without_definitions_leaves_prompt_unchanged(monkeypatch):
    captured = _capture_prompt_messages(monkeypatch)
    ext = LLMEntityRelationshipExtractor(_StubLLMService())

    # We patch _extract_kg_from_doc to a no-op so the chain isn't actually
    # invoked.
    monkeypatch.setattr(
        ext, "_extract_kg_from_doc", lambda *a, **kw: []
    )

    ext.document_er_extraction("hello world")

    rendered = "\n".join(m[1] for m in captured)
    assert "Schema entity types with definitions" not in rendered
    assert "Schema relationship types with definitions" not in rendered
    assert "(system prompt)" in rendered


def test_extractor_with_entity_definitions_appends_block(monkeypatch):
    captured = _capture_prompt_messages(monkeypatch)
    ext = LLMEntityRelationshipExtractor(
        _StubLLMService(),
        entity_type_definitions={
            "Company": "A corporate entity.",
            "Fund": "An investment vehicle pooling capital.",
        },
    )
    monkeypatch.setattr(ext, "_extract_kg_from_doc", lambda *a, **kw: [])

    ext.document_er_extraction("hello")

    rendered = "\n".join(m[1] for m in captured)
    assert "Schema entity types with definitions" in rendered
    assert "- Company: A corporate entity." in rendered
    assert "- Fund: An investment vehicle pooling capital." in rendered
    # Sorted ordering: Company (C) before Fund (F)
    assert rendered.index("Company") < rendered.index("Fund")
    # Disambiguation tip is present
    assert "disambiguate" in rendered.lower()


def test_extractor_renders_relationship_definitions(monkeypatch):
    captured = _capture_prompt_messages(monkeypatch)
    ext = LLMEntityRelationshipExtractor(
        _StubLLMService(),
        relationship_type_definitions={
            "PUBLISHES": "A company publishes a report.",
        },
    )
    monkeypatch.setattr(ext, "_extract_kg_from_doc", lambda *a, **kw: [])

    ext.document_er_extraction("hello")

    rendered = "\n".join(m[1] for m in captured)
    assert "Schema relationship types with definitions" in rendered
    assert "- PUBLISHES: A company publishes a report." in rendered


def test_extractor_skips_empty_definitions(monkeypatch):
    captured = _capture_prompt_messages(monkeypatch)
    ext = LLMEntityRelationshipExtractor(
        _StubLLMService(),
        entity_type_definitions={"Company": "", "Fund": ""},
    )
    monkeypatch.setattr(ext, "_extract_kg_from_doc", lambda *a, **kw: [])

    ext.document_er_extraction("hello")

    rendered = "\n".join(m[1] for m in captured)
    # No definitions block when every value is empty.
    assert "Schema entity types with definitions" not in rendered


def test_extractor_combines_allowed_types_and_definitions(monkeypatch):
    """Legacy ``allowed_entity_types`` still renders, AND definitions
    block also renders below it. They are not mutually exclusive.
    """
    captured = _capture_prompt_messages(monkeypatch)
    ext = LLMEntityRelationshipExtractor(
        _StubLLMService(),
        allowed_entity_types=["Company", "Fund"],
        entity_type_definitions={"Company": "A corp."},
    )
    monkeypatch.setattr(ext, "_extract_kg_from_doc", lambda *a, **kw: [])

    ext.document_er_extraction("hello")

    rendered = "\n".join(m[1] for m in captured)
    assert "Allowed Node Types" in rendered
    assert "Schema entity types with definitions" in rendered
    assert "- Company: A corp." in rendered
