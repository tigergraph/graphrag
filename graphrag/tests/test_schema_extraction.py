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
    blob = schema_extraction.concatenate_samples(samples, max_tokens=2_500)
    assert "# report1" in blob
    assert "# report2" in blob
    assert "Hello world." in blob
    assert "Second body." in blob


def test_concatenate_samples_truncates_at_max_budget():
    samples = [
        {"doc_id": "a", "content": "x" * 1_000},
        {"doc_id": "b", "content": "y" * 1_000},
    ]
    # 75 tokens × 4 chars/token = 300 char cap.
    blob = schema_extraction.concatenate_samples(samples, max_tokens=75)
    assert len(blob) <= 300


def test_concatenate_samples_handles_empty_content():
    samples = [{"doc_id": "empty", "content": ""}]
    blob = schema_extraction.concatenate_samples(samples, max_tokens=250)
    assert "# empty" in blob


def test_concatenate_samples_distributes_budget_across_files():
    """Every uploaded file must contribute to the LLM blob even when
    the first file is large — proportional/equal-share split prevents
    later files from being silently dropped.
    """
    samples = [
        {"doc_id": f"f{i}", "content": "x" * 5_000} for i in range(4)
    ]
    # 100 tokens × 4 = 400 char cap; ~100 chars per file.
    blob = schema_extraction.concatenate_samples(samples, max_tokens=100)
    # All four headers must appear; greedy first-fit would only emit f0 / f1.
    for i in range(4):
        assert f"# f{i}" in blob
    assert len(blob) <= 400


def test_concatenate_samples_rolls_unused_budget_forward():
    """A small first file leaves room for later files. The leftover
    budget must flow forward, not be discarded.
    """
    samples = [
        {"doc_id": "small", "content": "tiny"},
        {"doc_id": "big", "content": "y" * 10_000},
    ]
    # 250 tokens × 4 = 1_000 char cap.
    blob = schema_extraction.concatenate_samples(samples, max_tokens=250)
    # Big file should consume most of the leftover from small.
    assert blob.count("y") > 700  # ≥ 700 chars of big-file body
    assert "tiny" in blob


def test_resolve_sample_token_budget_uses_token_limit_when_configured():
    """When llm_service.config.token_limit is set, it drives the
    sample budget — model name is irrelevant.
    """

    class _LLM:
        config = {"token_limit": 50_000, "llm_model": "anything"}

    tokens = schema_extraction._resolve_sample_token_budget(_LLM())
    # 50_000 - 4_000 reserved = 46_000 sample tokens.
    assert tokens == 46_000


def test_resolve_sample_token_budget_uses_known_model_default():
    """Falls back to the per-model default context window when
    token_limit is not configured. Known build → no warning.
    """

    class _LLM:
        config = {"llm_model": "claude-3-5-sonnet-20241022"}

    tokens = schema_extraction._resolve_sample_token_budget(_LLM())
    # claude-3-5-sonnet → 200_000 tokens default - 4_000 reserved.
    assert tokens == 200_000 - 4_000


def test_resolve_sample_token_budget_warns_for_unknown_family_member(caplog):
    """An unrecognized but family-matchable model picks the family's
    default and emits a warning so the operator can update the table.
    """
    import logging

    class _LLM:
        config = {"llm_model": "claude-7-future-2030"}

    caplog.set_level(logging.WARNING, logger=schema_extraction.logger.name)
    tokens = schema_extraction._resolve_sample_token_budget(_LLM())
    # Family fallback for "claude" → 200_000 tokens.
    assert tokens == 200_000 - 4_000
    assert any(
        "claude-7-future-2030" in rec.message and "claude-family default" in rec.message
        for rec in caplog.records
    )


def test_resolve_sample_token_budget_warns_for_completely_unknown_model(caplog):
    """A model that doesn't match any family substring still gets a
    sane budget, but the warning is louder so the operator notices.
    """
    import logging

    class _LLM:
        config = {"llm_model": "homegrown-frobnicator-v3"}

    caplog.set_level(logging.WARNING, logger=schema_extraction.logger.name)
    tokens = schema_extraction._resolve_sample_token_budget(_LLM())
    assert tokens == 128_000 - 4_000
    assert any(
        "unknown" in rec.message and "homegrown-frobnicator" in rec.message
        for rec in caplog.records
    )


def test_resolve_sample_token_budget_handles_missing_config():
    """LLM service without a ``config`` attribute (e.g., test stubs)
    still produces a usable budget via the fallback path.
    """

    class _LLM:
        pass

    tokens = schema_extraction._resolve_sample_token_budget(_LLM())
    # No config → no token_limit, no model_name → fallback 128_000.
    assert tokens == 128_000 - 4_000


def test_extract_schema_gsql_passes_structural_and_keyword_lists_to_llm():
    llm = _CapturingLLM(response="// A company.\nADD VERTEX Company();")
    samples = [{"doc_id": "x", "content": "Acme Corp issues bonds."}]
    out, rendered = schema_extraction.extract_schema_gsql(llm, samples)

    assert out.startswith("// A company.")
    assert "Stub schema-extraction prompt" in rendered
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
    out, _ = schema_extraction.extract_schema_gsql(
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
    gsql, _ = schema_extraction.extract_schema_gsql(
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
    out, _ = schema_extraction.extract_schema_gsql(
        llm, [{"doc_id": "x", "content": "y"}]
    )
    assert "VERTEX V" in out
    inputs = llm.calls[0]["inputs"]
    # The prompt template was rendered against the three required
    # placeholders the stub exposed.
    assert "samples" in inputs
    assert "structural_types" in inputs
    assert "tg_keywords" in inputs


def test_render_type_hints_block_renders_both_categories():
    block = schema_extraction.render_type_hints_block(
        vertex_hints=[
            {"name": "Company", "description": "publicly listed corporation"},
            {"name": "Filing"},
        ],
        edge_hints=[
            {"name": "PUBLISHES", "description": "Company publishes a Filing"},
        ],
    )
    assert "## Suggested types" in block
    assert "Vertex types to include" in block
    assert "- Company: publicly listed corporation" in block
    assert "- Filing" in block
    assert "Edge types to include" in block
    assert "- PUBLISHES: Company publishes a Filing" in block


def test_render_type_hints_block_emits_endpoint_pair_for_edges():
    """Edge hints carrying ``fromType`` / ``toType`` render with the
    ``Name (From → To)`` form so the LLM sees the user's direction.
    Vertex hints ignore endpoint fields even when present (defensive).
    """
    block = schema_extraction.render_type_hints_block(
        vertex_hints=[
            # Endpoint fields on a vertex hint are silently ignored.
            {"name": "Company", "fromType": "X", "toType": "Y"},
        ],
        edge_hints=[
            {
                "name": "PUBLISHES",
                "fromType": "Company",
                "toType": "Filing",
                "description": "Company publishes a Filing",
            },
            {"name": "OWNS", "fromType": "Company", "toType": "Asset"},
            # No endpoints — renders as plain ``- WORKS_AT``.
            {"name": "WORKS_AT"},
        ],
    )
    # Vertex row is unchanged.
    assert "- Company\n" in block or "- Company" in block.split("Edge types")[0]
    # Edge rows include the endpoint arrow.
    assert "- PUBLISHES (Company → Filing): Company publishes a Filing" in block
    assert "- OWNS (Company → Asset)" in block
    assert "- WORKS_AT" in block


def test_render_type_hints_block_empty_returns_empty_string():
    assert schema_extraction.render_type_hints_block(None, None) == ""
    assert schema_extraction.render_type_hints_block([], []) == ""


def test_extract_schema_gsql_injects_hints_block_into_prompt():
    """The hints block must reach the LLM via the rendered prompt
    template, and the rendered template returned to the caller must
    contain the same block (so the UI can persist it as the per-graph
    override after a successful init).
    """

    class _StubLLM(_CapturingLLM):
        @property
        def schema_extraction_prompt(self) -> str:
            return (
                "Schema extraction.\n\n"
                "## Inputs\n"
                "{samples}\n{structural_types}\n{tg_keywords}\n"
            )

    llm = _StubLLM(response="VERTEX V();")
    out, rendered = schema_extraction.extract_schema_gsql(
        llm,
        [{"doc_id": "x", "content": "y"}],
        vertex_hints=[{"name": "Company", "description": "a corp"}],
        edge_hints=[{"name": "OWNS"}],
    )
    assert "VERTEX V" in out
    # Rendered prompt has the hints block injected before the Inputs
    # section so the LLM treats hints as guidance, not as content.
    assert "## Suggested types" in rendered
    assert "- Company: a corp" in rendered
    assert "- OWNS" in rendered
    assert rendered.index("## Suggested types") < rendered.index("## Inputs")


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
