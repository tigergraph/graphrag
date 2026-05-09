# Copyright (c) 2024-2026 TigerGraph, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0

"""Unit tests for ``common.db.schema_extraction`` — the sample-doc
schema-extraction prompt + concatenation helper.

We do not invoke the LLM. The tests verify (a) the concat helper's
truncation policy, (b) the prompt template renders the reserved-types
list, (c) ``extract_schema_gsql`` calls ``invoke_with_parser`` with
the right inputs and returns the LLM's text verbatim.
"""

from __future__ import annotations

from common.db import schema_extraction


_GENERIC_PROMPT_TEMPLATE = (
    "Stub schema-extraction prompt for tests.\n"
    "STRUCTURAL: {structural_types}\n"
    "KEYWORDS: {tg_keywords}\n"
    "SAMPLES:\n{samples}\n"
)


class _CapturingLLM:
    def __init__(self, response: str = ""):
        self.response = response
        self.calls: list = []

    @property
    def schema_extraction_prompt(self) -> str:
        return _GENERIC_PROMPT_TEMPLATE

    def invoke_with_parser(self, prompt, parser, inputs, caller_name="x"):
        self.calls.append({"prompt": prompt, "inputs": inputs, "caller_name": caller_name})
        return self.response


def test_concatenate_samples_joins_doc_id_headers():
    samples = [
        {"doc_id": "report1", "content": "Hello world."},
        {"doc_id": "report2", "content": "Second body."},
    ]
    blob = schema_extraction.concatenate_samples(samples, max_chars=10_000)
    assert "# report1" in blob
    assert "# report2" in blob
    assert "Hello world." in blob
    assert "Second body." in blob


def test_concatenate_samples_truncates_at_max_chars():
    samples = [
        {"doc_id": "a", "content": "x" * 1_000},
        {"doc_id": "b", "content": "y" * 1_000},
    ]
    blob = schema_extraction.concatenate_samples(samples, max_chars=300)
    assert len(blob) <= 300


def test_concatenate_samples_handles_empty_content():
    samples = [{"doc_id": "empty", "content": ""}]
    blob = schema_extraction.concatenate_samples(samples, max_chars=1_000)
    assert "# empty" in blob


def test_extract_schema_gsql_passes_structural_and_keyword_lists_to_llm():
    llm = _CapturingLLM(response="// A company.\nADD VERTEX Company();")
    samples = [{"doc_id": "x", "content": "Acme Corp issues bonds."}]
    out = schema_extraction.extract_schema_gsql(llm, samples)

    assert out.startswith("// A company.")
    assert len(llm.calls) == 1
    inputs = llm.calls[0]["inputs"]
    assert "samples" in inputs
    assert "structural_types" in inputs
    assert "tg_keywords" in inputs
    # Structural-type names appear in the structural list — both vertex
    # and edge types so the LLM doesn't propose either category.
    assert "Document" in inputs["structural_types"]
    assert "EntityType" in inputs["structural_types"]
    assert "HAS_CONTENT" in inputs["structural_types"]
    # GSQL keywords sourced from pyTigerGraph appear in the
    # tg_keywords list — at least the high-frequency ones must be
    # present so the LLM avoids common business-name collisions.
    keyword_blob = inputs["tg_keywords"]
    assert "TYPE" in keyword_blob
    assert "VERTEX" in keyword_blob
    assert "FROM" in keyword_blob
    # Sample text is present in the rendered samples blob.
    assert "Acme Corp" in inputs["samples"]
    assert llm.calls[0]["caller_name"] == "schema_extraction"


def test_extract_schema_gsql_returns_str_for_object_response():
    """If the LLM returns a non-string (e.g. a Pydantic object), the
    helper must coerce to str so the GSQL parser can consume it.
    """

    class _ObjResp:
        def __str__(self):  # noqa: D401
            return "ADD VERTEX Foo();"

    llm = _CapturingLLM(response=_ObjResp())
    out = schema_extraction.extract_schema_gsql(
        llm, [{"doc_id": "x", "content": "y"}]
    )
    assert "ADD VERTEX Foo" in out


def test_extract_schema_gsql_round_trips_through_parser():
    """End-to-end: the LLM's GSQL output, when fed back through the
    permissive parser, produces a non-empty SchemaProposal. This pins
    the contract between schema_extraction and schema_utils.

    Exercises both ``ADD``-prefixed and bare ``VERTEX`` / ``EDGE``
    forms (the new prompt asks the LLM for the bare form, the parser
    still has to accept whichever the LLM produces) and both
    ``DIRECTED`` and ``UNDIRECTED`` edges.
    """
    from common.db.schema_utils import parse_gsql_schema

    response = (
        "// A natural person.\n"
        "VERTEX Person(name STRING, role STRING);\n"
        "// An organization.\n"
        "VERTEX Organization(name STRING);\n"
        "// A person works for an organization.\n"
        "DIRECTED EDGE WORKS_FOR(FROM Person, TO Organization, role STRING);\n"
        "// Two people are colleagues.\n"
        "UNDIRECTED EDGE COLLEAGUE_OF(FROM Person, TO Person);\n"
    )
    llm = _CapturingLLM(response=response)
    gsql = schema_extraction.extract_schema_gsql(
        llm, [{"doc_id": "x", "content": "y"}]
    )
    proposal = parse_gsql_schema(gsql)
    proposal.drop_dangling_pairs()
    assert {v.name for v in proposal.vertices} == {"Person", "Organization"}
    edge_names = {e.name for e in proposal.edges}
    assert "WORKS_FOR" in edge_names
    assert "COLLEAGUE_OF" in edge_names
    works_for = next(e for e in proposal.edges if e.name == "WORKS_FOR")
    colleague_of = next(e for e in proposal.edges if e.name == "COLLEAGUE_OF")
    assert works_for.directed is True
    assert colleague_of.directed is False


def test_extract_schema_gsql_uses_llm_service_prompt_getter():
    """Prompt loading is delegated to llm_service.schema_extraction_prompt
    (the centralized base_llm getter that handles per-graph override
    resolution). The extract helper must read it via that property —
    no duplicate path-resolution code in this module.
    """

    class _StubLLM(_CapturingLLM):
        @property
        def schema_extraction_prompt(self) -> str:
            return (
                "STUB PROMPT\n"
                "{structural_types}\n"
                "{tg_keywords}\n"
                "{samples}\n"
            )

    llm = _StubLLM(response="// V.\nVERTEX V();")
    out = schema_extraction.extract_schema_gsql(
        llm, [{"doc_id": "x", "content": "y"}]
    )
    assert "VERTEX V" in out
    inputs = llm.calls[0]["inputs"]
    # The prompt template was rendered against the three required
    # placeholders the stub exposed.
    assert "samples" in inputs
    assert "structural_types" in inputs
    assert "tg_keywords" in inputs


def test_extract_schema_gsql_propagates_missing_prompt_file():
    """If llm_service.schema_extraction_prompt raises FileNotFoundError,
    extract_schema_gsql must propagate — no silent fallback. The
    file is expected to be present in every shipped provider dir.
    """

    class _MissingPromptLLM(_CapturingLLM):
        @property
        def schema_extraction_prompt(self) -> str:
            raise FileNotFoundError("schema_extraction.txt not found")

    import pytest as _pytest
    llm = _MissingPromptLLM()
    with _pytest.raises(FileNotFoundError):
        schema_extraction.extract_schema_gsql(
            llm, [{"doc_id": "x", "content": "y"}]
        )
