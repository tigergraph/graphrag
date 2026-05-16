# Copyright (c) 2024-2026 TigerGraph, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0

"""Unit tests for ``common.db.schema_utils``.

Covers the permissive GSQL parser, the additive GSQL emitter, the
structural-type filter, comment-as-description extraction, and the
``gsql ls`` output form.
"""

from __future__ import annotations

import pytest

from common.db.schema_utils import (
    ExistingSchema,
    SchemaProposal,
    apply_proposal,
    build_schema_change_job,
    emit_add_statements,
    emit_preview_gsql,
    emit_structural_link_alters,
    is_structural_type,
    parse_gsql_schema,
    read_existing_schema,
    read_type_metadata,
    summarize,
    upsert_type_metadata,
)


class _FakeConn:
    """Minimal pyTigerGraph-shaped connection for read_existing_schema tests."""

    def __init__(
        self,
        vertex_types,
        edge_metadata,
        gsql_response="OK",
        vertex_counts=None,
    ):
        self._vertex_types = list(vertex_types)
        self._edge_metadata = dict(edge_metadata)
        self._gsql_response = gsql_response
        self._vertex_counts = dict(vertex_counts or {})
        self.gsql_calls = []
        self.upsert_calls = []

    def getVertexTypes(self):
        return list(self._vertex_types)

    def getEdgeTypes(self):
        return list(self._edge_metadata.keys())

    def getEdgeType(self, name):
        return self._edge_metadata.get(name, {})

    def gsql(self, command):
        self.gsql_calls.append(command)
        # Minimal schema-change simulation so post-apply reads see the
        # newly-added vertex / edge types. Just enough for tests that
        # exercise the retriever-install hook downstream of
        # apply_proposal.
        import re
        for m in re.finditer(r"\bADD VERTEX (\w+)", command):
            vt = m.group(1)
            if vt not in self._vertex_types:
                self._vertex_types.append(vt)
        for m in re.finditer(
            r"\bADD (?:DIRECTED|UNDIRECTED) EDGE (\w+)", command
        ):
            et = m.group(1)
            self._edge_metadata.setdefault(et, {"EdgePairs": []})
        return self._gsql_response

    def upsertVertex(self, vertex_type, vertex_id, attributes=None):
        self.upsert_calls.append((vertex_type, vertex_id, dict(attributes or {})))

    def getVertexCount(self, vertex_type):
        return int(self._vertex_counts.get(vertex_type, 0))


class _FakeConnWithVertices(_FakeConn):
    """Extends _FakeConn with a getVertices() that returns canned rows."""

    def __init__(self, vertex_types, edge_metadata, vertices_by_type):
        super().__init__(vertex_types, edge_metadata)
        self._vertices_by_type = dict(vertices_by_type)

    def getVertices(self, vertexType, **kwargs):
        return list(self._vertices_by_type.get(vertexType, []))


# ---------------------------------------------------------------------------
# Structural-type filter
# ---------------------------------------------------------------------------


def test_is_structural_type_recognises_canonical_set():
    assert is_structural_type("Document")
    assert is_structural_type("EntityType")
    assert is_structural_type("HAS_CONTENT")
    # Case-insensitive
    assert is_structural_type("document")
    assert is_structural_type("entitytype")


def test_is_structural_type_recognises_reverse_companions():
    assert is_structural_type("reverse_HAS_CONTENT")
    assert is_structural_type("reverse_RELATIONSHIP")
    # Reverse of arbitrary edge names too — we don't try to parse them
    assert is_structural_type("reverse_PUBLISHES")


def test_is_structural_type_rejects_domain_names():
    assert not is_structural_type("Company")
    assert not is_structural_type("Fund")
    assert not is_structural_type("PUBLISHES")
    assert not is_structural_type("")


# ---------------------------------------------------------------------------
# Parser — happy paths
# ---------------------------------------------------------------------------


def test_parse_simple_add_form():
    text = """
    ADD VERTEX Company();
    ADD VERTEX Report();
    ADD DIRECTED EDGE PUBLISHES(FROM Company, TO Report);
    """
    proposal = parse_gsql_schema(text)
    assert {v.name for v in proposal.vertices} == {"Company", "Report"}
    assert len(proposal.edges) == 1
    assert proposal.edges[0].name == "PUBLISHES"
    assert proposal.edges[0].pairs == [("Company", "Report")]


def test_parse_with_attributes_captures_attrs_and_strips_primary_id():
    """The parser captures primitive (name, type) attributes and skips
    PRIMARY_ID (which is system-generated). WITH-clauses are ignored.
    """
    text = """
    ADD VERTEX Company(PRIMARY_ID id STRING, name STRING, founded_year INT)
        WITH PRIMARY_ID_AS_ATTRIBUTE="true", STATS="OUTDEGREE_BY_EDGETYPE";
    ADD DIRECTED EDGE OWNS(FROM Company, TO Company, percent_owned DOUBLE)
        WITH REVERSE_EDGE="reverse_OWNS";
    """
    proposal = parse_gsql_schema(text)
    company = proposal.find_vertex("Company")
    assert company is not None
    attr_names = [a.name for a in company.attributes]
    attr_types = [a.type for a in company.attributes]
    assert attr_names == ["name", "founded_year"]
    assert attr_types == ["STRING", "INT"]
    # PRIMARY_ID's "id" must NOT appear as a regular attribute.
    assert "id" not in [a.name for a in company.attributes]

    edge = proposal.find_edge("OWNS")
    assert edge is not None and edge.pairs == [("Company", "Company")]
    edge_attrs = [(a.name, a.type) for a in edge.attributes]
    assert edge_attrs == [("percent_owned", "DOUBLE")]


def test_emit_includes_attributes_in_add_vertex_and_edge():
    """The emitter renders attributes after the auto-added PRIMARY_ID
    on vertices and after the FROM/TO pairs on edges.
    """
    proposal = SchemaProposal()
    proposal.add_vertex(
        "Company",
        attributes=[("name", "STRING"), ("founded_year", "INT")],
    )
    proposal.add_vertex("Report", attributes=[("title", "STRING")])
    proposal.add_edge_pair(
        "PUBLISHES",
        "Company",
        "Report",
        attributes=[("effective_date", "STRING")],
    )

    stmts = emit_add_statements(proposal)
    company_stmt = next(s for s in stmts if "ADD VERTEX Company" in s)
    assert "PRIMARY_ID id STRING" in company_stmt
    assert "name STRING" in company_stmt
    assert "founded_year INT" in company_stmt

    edge_stmt = next(s for s in stmts if "ADD DIRECTED EDGE PUBLISHES" in s)
    assert "FROM Company, TO Report" in edge_stmt
    assert "effective_date STRING" in edge_stmt


def test_emit_filters_unknown_primitive_types():
    """Attributes whose type isn't a known GSQL primitive are dropped
    silently — they would error at schema-change time otherwise.
    """
    proposal = SchemaProposal()
    proposal.add_vertex(
        "Company",
        attributes=[
            ("name", "STRING"),
            ("note", "VARCHAR"),  # not a GSQL primitive
            ("count", "INT"),
        ],
    )
    company = proposal.find_vertex("Company")
    assert [a.name for a in company.attributes] == ["name", "count"]


def test_parse_multi_pair_edge_produces_one_edge_with_multiple_pairs():
    text = """
    ADD VERTEX Company();
    ADD VERTEX Report();
    ADD VERTEX Filing();
    ADD DIRECTED EDGE PUBLISHES(FROM Company, TO Report | FROM Company, TO Filing);
    """
    proposal = parse_gsql_schema(text)
    edge = proposal.find_edge("PUBLISHES")
    assert edge is not None
    assert edge.pairs == [("Company", "Report"), ("Company", "Filing")]


def test_parse_descriptions_from_double_slash_comments():
    text = """
    // A corporate or business entity.
    ADD VERTEX Company();

    // A formal document
    // summarising performance.
    ADD VERTEX Report();

    // Company publishes a Report.
    ADD DIRECTED EDGE PUBLISHES(FROM Company, TO Report);
    """
    proposal = parse_gsql_schema(text)
    assert proposal.find_vertex("Company").description == \
        "A corporate or business entity."
    # Multi-line // comments are joined
    assert "summarising performance" in proposal.find_vertex("Report").description
    assert proposal.find_edge("PUBLISHES").description.startswith(
        "Company publishes"
    )


def test_parse_descriptions_from_block_comment():
    text = """
    /*
     * A corporate or business entity.
     */
    ADD VERTEX Company();
    """
    proposal = parse_gsql_schema(text)
    assert "corporate or business entity" in (
        proposal.find_vertex("Company").description
    )


# ---------------------------------------------------------------------------
# Parser — `gsql ls` output form
# ---------------------------------------------------------------------------


def test_parse_ls_output_form():
    text = """
    Vertex Types:
      - VERTEX DocumentChunk(PRIMARY_ID id STRING) WITH STATS="..."
      - VERTEX Company(PRIMARY_ID id STRING, name STRING) WITH PRIMARY_ID_AS_ATTRIBUTE="true"
      - VERTEX Report(PRIMARY_ID id STRING) WITH PRIMARY_ID_AS_ATTRIBUTE="true"
    Edge Types:
      - DIRECTED EDGE HAS_CONTENT(FROM DocumentChunk, TO Content) WITH REVERSE_EDGE="reverse_HAS_CONTENT"
      - DIRECTED EDGE reverse_HAS_CONTENT(FROM Content, TO DocumentChunk) WITH REVERSE_EDGE="HAS_CONTENT"
      - DIRECTED EDGE PUBLISHES(FROM Company, TO Report) WITH REVERSE_EDGE="reverse_PUBLISHES"
      - DIRECTED EDGE reverse_PUBLISHES(FROM Report, TO Company) WITH REVERSE_EDGE="PUBLISHES"
    Indexes:
      - some_index:Foo(bar)
    """
    proposal = parse_gsql_schema(text)
    # Structural types (DocumentChunk, HAS_CONTENT, reverse_HAS_CONTENT)
    # silently dropped; only Company, Report, PUBLISHES survive.
    assert {v.name for v in proposal.vertices} == {"Company", "Report"}
    assert [e.name for e in proposal.edges] == ["PUBLISHES"]
    assert proposal.find_edge("PUBLISHES").pairs == [("Company", "Report")]
    # Section headers and Indexes block silently ignored — no errors raised.


# ---------------------------------------------------------------------------
# Parser — permissive: noise is silently dropped
# ---------------------------------------------------------------------------


def test_parse_silently_ignores_unrelated_statements():
    text = """
    CREATE GRAPH FooBar(*);
    INSTALL QUERY ALL;
    USE GRAPH FooBar;

    Some prose preamble line.

    ADD VERTEX Company();
    ADD DIRECTED EDGE PUBLISHES(FROM Company, TO Report);
    ADD VERTEX Report();

    DROP QUERY ALL;
    """
    proposal = parse_gsql_schema(text)
    # Order of vertex declarations doesn't matter — pairs are filtered after.
    assert {v.name for v in proposal.vertices} == {"Company", "Report"}
    assert proposal.find_edge("PUBLISHES").pairs == [("Company", "Report")]


def test_parse_drops_pairs_with_unknown_endpoint():
    text = """
    ADD VERTEX Company();
    // Report is missing on purpose
    ADD DIRECTED EDGE PUBLISHES(FROM Company, TO Report | FROM Company, TO Filing);
    ADD VERTEX Filing();
    """
    proposal = parse_gsql_schema(text)
    # The Company → Report pair has a dangling endpoint → dropped.
    # Company → Filing survives because Filing is declared.
    assert proposal.find_edge("PUBLISHES").pairs == [("Company", "Filing")]


def test_parse_drops_structural_type_collisions():
    text = """
    ADD VERTEX Document();           // dropped — structural
    ADD VERTEX Company();            // kept
    ADD VERTEX HAS_CONTENT();        // dropped — structural edge name
    ADD DIRECTED EDGE HAS_CONTENT(FROM Document, TO Content);  // dropped
    ADD DIRECTED EDGE PUBLISHES(FROM Company, TO Report);
    ADD VERTEX Report();
    """
    proposal = parse_gsql_schema(text)
    names = {v.name for v in proposal.vertices}
    assert "Document" not in names
    assert "HAS_CONTENT" not in names
    assert names == {"Company", "Report"}
    assert [e.name for e in proposal.edges] == ["PUBLISHES"]


def test_parse_empty_input_yields_empty_proposal():
    proposal = parse_gsql_schema("")
    assert proposal.vertices == []
    assert proposal.edges == []
    proposal2 = parse_gsql_schema("Just some prose, no GSQL here.")
    assert proposal2.vertices == []
    assert proposal2.edges == []


# ---------------------------------------------------------------------------
# Emitter — diff against existing
# ---------------------------------------------------------------------------


def test_emit_against_empty_existing_adds_everything():
    proposal = SchemaProposal()
    proposal.add_vertex("Company")
    proposal.add_vertex("Report")
    proposal.add_edge_pair("PUBLISHES", "Company", "Report")

    stmts = emit_add_statements(proposal, existing=ExistingSchema())
    assert any(s.startswith("ADD VERTEX Company") for s in stmts)
    assert any(s.startswith("ADD VERTEX Report") for s in stmts)
    assert any(
        s.startswith("ADD DIRECTED EDGE PUBLISHES") and "FROM Company, TO Report" in s
        for s in stmts
    )


def test_emit_skips_vertices_already_in_graph():
    proposal = SchemaProposal()
    proposal.add_vertex("Company")
    proposal.add_vertex("Report")

    existing = ExistingSchema(vertex_types={"Company"})
    stmts = emit_add_statements(proposal, existing=existing)
    assert not any("ADD VERTEX Company" in s for s in stmts)
    assert any("ADD VERTEX Report" in s for s in stmts)


def test_emit_alter_edge_when_pair_is_new_on_existing_edge():
    proposal = SchemaProposal()
    proposal.add_vertex("Company")
    proposal.add_vertex("Filing")
    proposal.add_edge_pair("PUBLISHES", "Company", "Filing")

    existing = ExistingSchema(
        vertex_types={"Company"},
        edge_pairs={"PUBLISHES": {("Company", "Report")}},
    )
    stmts = emit_add_statements(proposal, existing=existing)
    # New vertex:
    assert any("ADD VERTEX Filing" in s for s in stmts)
    # No fresh ADD DIRECTED EDGE — PUBLISHES already exists:
    assert not any(s.startswith("ADD DIRECTED EDGE PUBLISHES") for s in stmts)
    # ALTER … ADD PAIR for the new pair:
    assert any(
        "ALTER EDGE PUBLISHES ADD PAIR (FROM Company, TO Filing)" in s
        for s in stmts
    )


def test_emit_skips_pair_already_on_edge():
    proposal = SchemaProposal()
    proposal.add_vertex("Company")
    proposal.add_vertex("Report")
    proposal.add_edge_pair("PUBLISHES", "Company", "Report")

    existing = ExistingSchema(
        vertex_types={"Company", "Report"},
        edge_pairs={"PUBLISHES": {("Company", "Report")}},
    )
    stmts = emit_add_statements(proposal, existing=existing)
    assert stmts == []  # everything already exists


# ---------------------------------------------------------------------------
# Preview rendering
# ---------------------------------------------------------------------------


def test_emit_preview_gsql_round_trips_through_parser():
    proposal = SchemaProposal(domain_label="Corp Gov")
    proposal.add_vertex("Company", description="A corporate entity.")
    proposal.add_vertex("Report", description="A formal document.")
    proposal.add_edge_pair(
        "PUBLISHES", "Company", "Report",
        description="Company publishes a Report.",
    )

    preview = emit_preview_gsql(proposal)
    assert "ADD VERTEX Company" in preview
    assert "ADD DIRECTED EDGE PUBLISHES" in preview
    assert "FROM Company, TO Report" in preview

    # Round-trip: parse the preview back and verify it reconstructs the
    # same vertex / edge / pair set (descriptions are preserved too).
    reparsed = parse_gsql_schema(preview)
    assert {v.name for v in reparsed.vertices} == {"Company", "Report"}
    assert [(e.name, e.pairs) for e in reparsed.edges] == [
        ("PUBLISHES", [("Company", "Report")])
    ]
    assert reparsed.find_vertex("Company").description == "A corporate entity."
    assert reparsed.find_edge("PUBLISHES").description.startswith(
        "Company publishes"
    )


# ---------------------------------------------------------------------------
# Summary helper
# ---------------------------------------------------------------------------


def test_summarize_returns_counts_and_names():
    proposal = SchemaProposal(domain_label="Corp")
    proposal.add_vertex("Company")
    proposal.add_vertex("Report")
    proposal.add_edge_pair("PUBLISHES", "Company", "Report")
    proposal.add_edge_pair("PUBLISHES", "Company", "Filing")

    summary = summarize(proposal)
    assert summary["vertex_count"] == 2
    assert summary["edge_count"] == 1
    assert summary["edge_pair_count"] == 2
    assert summary["domain_label"] == "Corp"
    assert set(summary["vertex_names"]) == {"Company", "Report"}
    assert summary["edge_names"] == ["PUBLISHES"]


# ---------------------------------------------------------------------------
# from_dict / to_dict round-trip
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# read_existing_schema — TigerGraph-side reader
# ---------------------------------------------------------------------------


def test_read_existing_schema_empty_graph():
    conn = _FakeConn(vertex_types=[], edge_metadata={})
    snapshot = read_existing_schema(conn)
    assert snapshot.vertex_types == set()
    assert snapshot.edge_pairs == {}


def test_read_existing_schema_single_pair_edge():
    conn = _FakeConn(
        vertex_types=["Document", "Entity", "Company"],
        edge_metadata={
            "PUBLISHES": {
                "Name": "PUBLISHES",
                "FromVertexTypeName": "Company",
                "ToVertexTypeName": "Report",
            },
        },
    )
    snapshot = read_existing_schema(conn)
    assert snapshot.has_vertex("Company")
    assert snapshot.has_edge("PUBLISHES")
    assert snapshot.has_edge_pair("PUBLISHES", "Company", "Report")


def test_read_existing_schema_multi_pair_edge():
    """When FromVertexTypeName/ToVertexTypeName are '*', the metadata's
    EdgePairs list carries the actual (FROM, TO) pairs.
    """
    conn = _FakeConn(
        vertex_types=["DocumentChunk", "Entity", "Document"],
        edge_metadata={
            "CONTAINS_ENTITY": {
                "Name": "CONTAINS_ENTITY",
                "FromVertexTypeName": "*",
                "ToVertexTypeName": "*",
                "EdgePairs": [
                    {"From": "DocumentChunk", "To": "Entity"},
                    {"From": "Document", "To": "Entity"},
                ],
            },
        },
    )
    snapshot = read_existing_schema(conn)
    assert snapshot.has_edge_pair("CONTAINS_ENTITY", "DocumentChunk", "Entity")
    assert snapshot.has_edge_pair("CONTAINS_ENTITY", "Document", "Entity")


def test_read_existing_schema_feeds_emit_add_statements_diff():
    """End-to-end: existing graph has Company + PUBLISHES(Company→Report),
    proposal wants to add Filing + PUBLISHES(Company→Filing). The emitter
    should produce one ADD VERTEX (for Filing) and one ALTER EDGE … ADD PAIR.
    """
    conn = _FakeConn(
        vertex_types=["Company", "Report"],
        edge_metadata={
            "PUBLISHES": {
                "FromVertexTypeName": "Company",
                "ToVertexTypeName": "Report",
            },
        },
    )
    existing = read_existing_schema(conn)

    proposal = SchemaProposal()
    proposal.add_vertex("Company")
    proposal.add_vertex("Report")
    proposal.add_vertex("Filing")
    proposal.add_edge_pair("PUBLISHES", "Company", "Report")  # already there
    proposal.add_edge_pair("PUBLISHES", "Company", "Filing")  # new pair

    stmts = emit_add_statements(proposal, existing=existing)
    assert any("ADD VERTEX Filing" in s for s in stmts)
    assert not any("ADD VERTEX Company" in s for s in stmts)
    assert any(
        "ALTER EDGE PUBLISHES ADD PAIR (FROM Company, TO Filing)" in s
        for s in stmts
    )
    # No fresh ADD DIRECTED EDGE since PUBLISHES already exists.
    assert not any(s.startswith("ADD DIRECTED EDGE PUBLISHES") for s in stmts)


def test_to_from_dict_round_trip():
    original = SchemaProposal(domain_label="Corp")
    original.add_vertex("Company", description="A corporate entity.")
    original.add_vertex("Report")
    original.add_edge_pair(
        "PUBLISHES", "Company", "Report",
        description="Company publishes a Report.",
    )

    data = original.to_dict()
    reconstructed = SchemaProposal.from_dict(data)
    assert reconstructed.domain_label == "Corp"
    assert {v.name for v in reconstructed.vertices} == {"Company", "Report"}
    edge = reconstructed.find_edge("PUBLISHES")
    assert edge.pairs == [("Company", "Report")]
    assert edge.description.startswith("Company publishes")


# ---------------------------------------------------------------------------
# build_schema_change_job
# ---------------------------------------------------------------------------


def test_build_schema_change_job_wraps_statements_in_atomic_block():
    stmts = [
        'ADD VERTEX Company (PRIMARY_ID id STRING) WITH PRIMARY_ID_AS_ATTRIBUTE="true"',
        "ALTER EDGE PUBLISHES ADD PAIR (FROM Company, TO Filing)",
    ]
    block, job_name = build_schema_change_job("MyGraph", stmts)

    assert job_name.startswith("add_domain_schema_")
    assert "USE GRAPH MyGraph" in block
    assert f"CREATE SCHEMA_CHANGE JOB {job_name} FOR GRAPH MyGraph" in block
    assert f"RUN SCHEMA_CHANGE JOB {job_name}" in block
    assert f"DROP JOB {job_name}" in block
    # All inner statements appear and are terminated with ';'
    for s in stmts:
        assert s in block
    assert block.count(";") == len(stmts)


def test_build_schema_change_job_strips_trailing_semicolons_to_avoid_duplicates():
    stmts = ["ADD VERTEX Company (PRIMARY_ID id STRING);"]
    block, _ = build_schema_change_job("g", stmts)
    # Exactly one terminator inside the body — the helper added it back.
    assert block.count(";;") == 0
    assert block.count(";") == 1


def test_build_schema_change_job_respects_explicit_job_name():
    block, job_name = build_schema_change_job(
        "g",
        ["ADD VERTEX Company (PRIMARY_ID id STRING)"],
        job_name="fixed_job",
    )
    assert job_name == "fixed_job"
    assert "CREATE SCHEMA_CHANGE JOB fixed_job" in block
    assert "RUN SCHEMA_CHANGE JOB fixed_job" in block


def test_build_schema_change_job_empty_statements_raises():
    with pytest.raises(ValueError):
        build_schema_change_job("g", [])


@pytest.mark.parametrize(
    "bad_name",
    [
        "g raph",          # whitespace
        'g"x',             # quote — closes a STRING literal
        "g; DROP GRAPH x", # statement separator + injection attempt
        "g\n",             # newline
        "1graph",          # leading digit
        "",                # empty
    ],
)
def test_build_schema_change_job_rejects_invalid_graphname(bad_name):
    with pytest.raises(ValueError, match="Invalid graph name"):
        build_schema_change_job(bad_name, ["ADD VERTEX X(PRIMARY_ID id STRING)"])


@pytest.mark.parametrize(
    "bad_job",
    [
        "job name",
        'job"x',
        "job; DROP JOB other",
        "1job",
    ],
)
def test_build_schema_change_job_rejects_invalid_job_name(bad_job):
    with pytest.raises(ValueError, match="Invalid job name"):
        build_schema_change_job(
            "g",
            ["ADD VERTEX X(PRIMARY_ID id STRING)"],
            job_name=bad_job,
        )


# ---------------------------------------------------------------------------
# apply_proposal
# ---------------------------------------------------------------------------


def test_apply_proposal_no_op_when_diff_is_empty():
    """If the existing graph already has every type in the proposal, the
    helper must not run a SCHEMA_CHANGE JOB and must report
    status='no-op'. Retriever re-installs are still expected because
    they're keyed off the live schema, not the proposal diff.
    """
    conn = _FakeConn(
        vertex_types=["Company", "Report"],
        edge_metadata={
            "PUBLISHES": {
                "FromVertexTypeName": "Company",
                "ToVertexTypeName": "Report",
            },
        },
    )
    proposal = SchemaProposal()
    proposal.add_vertex("Company")
    proposal.add_vertex("Report")
    proposal.add_edge_pair("PUBLISHES", "Company", "Report")

    result = apply_proposal(conn, "g", proposal)

    assert result["status"] == "no-op"
    assert result["statements"] == []
    assert result["job_name"] is None
    assert result["gsql_output"] == ""
    # No SCHEMA_CHANGE JOB block — but retriever installs may run.
    assert not any("SCHEMA_CHANGE JOB" in c for c in conn.gsql_calls)
    assert result["summary"]["vertex_count"] == 2


def test_apply_proposal_runs_single_gsql_call_with_diff():
    """On a partially-populated graph the helper should issue exactly one
    gsql() call — the wrapped CREATE / RUN / DROP block — containing only
    the missing ADD/ALTER statements.
    """
    conn = _FakeConn(
        vertex_types=["Company"],
        edge_metadata={},
        gsql_response="JOB add_domain_schema_xxx COMPLETED",
    )
    proposal = SchemaProposal()
    proposal.add_vertex("Company")
    proposal.add_vertex("Filing")
    proposal.add_edge_pair("OWNS", "Company", "Filing")

    result = apply_proposal(conn, "MyGraph", proposal)

    assert result["status"] == "applied"
    schema_calls = [c for c in conn.gsql_calls if "SCHEMA_CHANGE JOB" in c]
    assert len(schema_calls) == 1
    cmd = schema_calls[0]
    assert "USE GRAPH MyGraph" in cmd
    assert "ADD VERTEX Filing" in cmd
    assert "ADD DIRECTED EDGE OWNS" in cmd
    # Must NOT re-add the existing Company vertex.
    assert "ADD VERTEX Company" not in cmd
    assert "JOB COMPLETED" in result["gsql_output"] or "COMPLETED" in result["gsql_output"]
    assert result["job_name"].startswith("add_domain_schema_")
    assert any("ADD VERTEX Filing" in s for s in result["statements"])


def test_apply_proposal_drops_structural_collisions_before_diff():
    """parse_gsql_schema is the public contract for ingesting user input,
    but apply_proposal also receives proposals constructed in code. If a
    caller builds a proposal that names a structural type, the diff
    against an empty graph should still skip it (no ADD VERTEX Document)
    because read_existing_schema's snapshot won't contain it on a fresh
    graph — so we rely on parse_gsql_schema's filter, *or* the caller
    must filter manually. This test pins the contract: apply_proposal
    does NOT silently filter; the caller is responsible. Used to detect
    regressions if someone adds 'helpful' filtering to apply_proposal.
    """
    conn = _FakeConn(vertex_types=[], edge_metadata={})
    proposal = SchemaProposal()
    proposal.add_vertex("Document")  # structural — caller's mistake

    result = apply_proposal(conn, "g", proposal)
    # Statement is emitted because apply_proposal does NOT re-filter;
    # the parse step is where structural collisions are dropped.
    assert any("ADD VERTEX Document" in s for s in result["statements"])


def test_apply_proposal_end_to_end_from_pasted_gsql():
    """Driving the full Slice 1c happy path: pasted GSQL → parser →
    drop_dangling_pairs → apply_proposal against a fresh graph runs one
    job containing everything.
    """
    pasted = """
    // A corporate entity.
    ADD VERTEX Company();
    // A regulatory filing.
    ADD VERTEX Filing();
    // A company publishes a filing.
    ADD DIRECTED EDGE PUBLISHES(FROM Company, TO Filing);
    """
    proposal = parse_gsql_schema(pasted)
    proposal.drop_dangling_pairs()

    conn = _FakeConn(vertex_types=[], edge_metadata={})
    result = apply_proposal(conn, "FreshGraph", proposal)

    assert result["status"] == "applied"
    schema_calls = [c for c in conn.gsql_calls if "SCHEMA_CHANGE JOB" in c]
    assert len(schema_calls) == 1
    cmd = schema_calls[0]
    assert "USE GRAPH FreshGraph" in cmd
    assert "ADD VERTEX Company" in cmd
    assert "ADD VERTEX Filing" in cmd
    assert "ADD DIRECTED EDGE PUBLISHES" in cmd
    assert "FROM Company, TO Filing" in cmd
    assert result["summary"]["vertex_count"] == 2
    assert result["summary"]["edge_count"] == 1


# ---------------------------------------------------------------------------
# Transitional graph (pre-existing Entity data + new domain schema)
# ---------------------------------------------------------------------------


def test_apply_proposal_transitional_forces_include_entity():
    """Upgrade case: existing graph has Entity-layer data, user declares
    a domain schema for the first time. The retriever installer must
    flip ``include_entity`` to True regardless of config so existing
    Entity rows stay reachable until re-ingest. The result surfaces a
    transitional payload the dialog can render.
    """
    conn = _FakeConn(
        vertex_types=[
            "Document", "DocumentChunk", "Entity",
            "EntityType", "RelationshipType", "Community",
        ],
        edge_metadata={},
        # Existing Entity-layer data — 1742 entities, no domain VTs yet.
        vertex_counts={"Entity": 1742},
    )
    proposal = SchemaProposal()
    proposal.add_vertex("Company")
    proposal.add_vertex("Report")

    result = apply_proposal(conn, "g", proposal)

    assert result["status"] == "applied"
    retrievers = result["retrievers"]
    # Forced True regardless of config default.
    assert retrievers["include_entity"] is True
    # Transitional payload surfaced for the caller / UI.
    transitional = retrievers.get("transitional")
    assert transitional is not None
    assert transitional["entity_count"] == 1742
    assert transitional["new_domain_vts"] == ["Company", "Report"]
    assert "re-ingest" in transitional["recommendation"].lower() or \
        "re-run" in transitional["recommendation"].lower()


def test_apply_proposal_no_entity_data_keeps_typed_purist_default():
    """Fresh graph (no Entity rows yet) gets the normal auto-default —
    typed-purist when domain schema exists.
    """
    conn = _FakeConn(
        vertex_types=[
            "Document", "DocumentChunk", "Entity",
            "EntityType", "RelationshipType", "Community",
        ],
        edge_metadata={},
        vertex_counts={"Entity": 0},
    )
    proposal = SchemaProposal()
    proposal.add_vertex("Company")

    result = apply_proposal(conn, "g", proposal)

    retrievers = result["retrievers"]
    # No Entity data → auto-default fires, typed-purist.
    assert retrievers["include_entity"] is False
    assert "transitional" not in retrievers


def test_apply_proposal_no_new_domain_vts_skips_transitional_check():
    """Re-applying an unchanged proposal against a graph that already
    has the domain VTs is not transitional — no new VTs introduced. The
    Entity count is irrelevant; auto-default behaviour applies.
    """
    conn = _FakeConn(
        vertex_types=[
            "Document", "DocumentChunk", "Entity",
            "EntityType", "RelationshipType", "Community",
            "Company",  # already on the graph
        ],
        edge_metadata={
            "CONTAINS_ENTITY": {
                "FromVertexTypeName": "Document",
                "ToVertexTypeName": "Company",
            },
            "IN_COMMUNITY": {
                "FromVertexTypeName": "Company",
                "ToVertexTypeName": "Community",
            },
        },
        vertex_counts={"Entity": 9000, "Company": 100},
    )
    proposal = SchemaProposal()
    proposal.add_vertex("Company")  # already on graph — no new VT

    result = apply_proposal(conn, "g", proposal)

    retrievers = result["retrievers"]
    # No new VTs in proposal → not a transitional apply, normal default.
    assert "transitional" not in retrievers


def test_apply_proposal_error_path_returns_uniform_shape():
    """When ``conn.gsql`` reports a server-side failure, the result
    payload must carry the same keys as the success / no-op paths so
    callers can read ``status / statements / retrievers / metadata``
    uniformly without a per-status branch.
    """
    conn = _FakeConn(
        vertex_types=[
            "Document", "DocumentChunk", "Entity",
            "EntityType", "RelationshipType", "Community",
        ],
        edge_metadata={},
        gsql_response="SEMANTIC ERROR: simulated failure",
    )
    proposal = SchemaProposal()
    proposal.add_vertex("Company")

    result = apply_proposal(conn, "g", proposal)

    assert result["status"] == "error"
    # Full uniform shape — same key set as success / no-op.
    assert set(result.keys()) == {
        "status", "statements", "job_name", "job_names", "gsql_output",
        "error", "summary", "metadata", "retrievers",
    }
    assert result["retrievers"] == {
        "status": "skipped",
        "reason": "schema apply failed",
    }


# ---------------------------------------------------------------------------
# upsert_type_metadata
# ---------------------------------------------------------------------------


def test_upsert_type_metadata_writes_entity_and_relationship_rows():
    proposal = SchemaProposal()
    proposal.add_vertex("Company", description="A corporate entity.")
    proposal.add_vertex("Report")  # no description → omits attribute
    proposal.add_edge_pair(
        "PUBLISHES", "Company", "Report",
        description="Company publishes a Report.",
    )
    conn = _FakeConn(vertex_types=[], edge_metadata={})

    result = upsert_type_metadata(conn, proposal)

    assert result == {
        "entity_types": ["Company", "Report"],
        "relationship_types": ["PUBLISHES"],
    }
    types_written = [(c[0], c[1]) for c in conn.upsert_calls]
    assert ("EntityType", "Company") in types_written
    assert ("EntityType", "Report") in types_written
    assert ("RelationshipType", "PUBLISHES") in types_written

    company_call = next(c for c in conn.upsert_calls if c[1] == "Company")
    assert company_call[2]["description"] == "A corporate entity."
    assert "epoch_added" in company_call[2]

    report_call = next(c for c in conn.upsert_calls if c[1] == "Report")
    assert "description" not in report_call[2]
    assert "epoch_added" in report_call[2]

    pub_call = next(c for c in conn.upsert_calls if c[1] == "PUBLISHES")
    assert pub_call[2]["definition"] == "Company publishes a Report."
    assert pub_call[2]["short_name"] == "publishes"
    assert "epoch_added" in pub_call[2]


def test_apply_proposal_populates_metadata_on_apply():
    """End-to-end Slice 1d: when apply_proposal runs the schema-change
    job, metadata vertices must also be written for every type in the
    proposal. The result dict must surface the upsert summary so callers
    can log / return it.
    """
    conn = _FakeConn(vertex_types=[], edge_metadata={})
    proposal = SchemaProposal()
    proposal.add_vertex("Company", description="A corp.")
    proposal.add_edge_pair(
        "OWNS", "Company", "Company", description="Self-ownership."
    )

    result = apply_proposal(conn, "g", proposal)

    assert result["status"] == "applied"
    assert result["metadata"]["entity_types"] == ["Company"]
    assert result["metadata"]["relationship_types"] == ["OWNS"]
    assert any(c[0] == "EntityType" and c[1] == "Company" for c in conn.upsert_calls)
    assert any(
        c[0] == "RelationshipType" and c[1] == "OWNS" for c in conn.upsert_calls
    )


def test_apply_proposal_populates_metadata_on_no_op():
    """Even when the schema diff is empty, metadata vertices must be
    upserted so descriptions edited on the review screen land in the
    graph (the schema is already there from a prior init).
    """
    conn = _FakeConn(
        vertex_types=["Company"],
        edge_metadata={
            "OWNS": {
                "FromVertexTypeName": "Company",
                "ToVertexTypeName": "Company",
            },
        },
    )
    proposal = SchemaProposal()
    proposal.add_vertex("Company", description="Updated description.")
    proposal.add_edge_pair(
        "OWNS", "Company", "Company", description="Updated definition."
    )

    result = apply_proposal(conn, "g", proposal)

    assert result["status"] == "no-op"
    assert result["metadata"]["entity_types"] == ["Company"]
    assert result["metadata"]["relationship_types"] == ["OWNS"]
    company = next(c for c in conn.upsert_calls if c[1] == "Company")
    assert company[2]["description"] == "Updated description."
    owns = next(c for c in conn.upsert_calls if c[1] == "OWNS")
    assert owns[2]["definition"] == "Updated definition."


# ---------------------------------------------------------------------------
# read_type_metadata
# ---------------------------------------------------------------------------


def test_read_type_metadata_returns_descriptions_and_definitions():
    conn = _FakeConnWithVertices(
        vertex_types=[],
        edge_metadata={},
        vertices_by_type={
            "EntityType": [
                {"v_id": "Company", "attributes": {"description": "A corp."}},
                {"v_id": "Report", "attributes": {"description": ""}},
                {"v_id": "Filing", "attributes": {"description": "A filing."}},
            ],
            "RelationshipType": [
                {
                    "v_id": "PUBLISHES",
                    "attributes": {"definition": "Company publishes a Report."},
                },
                {
                    "v_id": "OWNS",
                    "attributes": {"definition": ""},
                },
            ],
        },
    )

    entity_descs, rel_defs = read_type_metadata(conn)

    assert entity_descs == {"Company": "A corp.", "Filing": "A filing."}
    assert rel_defs == {"PUBLISHES": "Company publishes a Report."}


def test_read_type_metadata_returns_empty_on_missing_method():
    """When conn lacks getVertices (older mock / stub), behave gracefully
    rather than raising — schema_rep must still render.
    """
    conn = _FakeConn(vertex_types=[], edge_metadata={})  # no getVertices
    entity_descs, rel_defs = read_type_metadata(conn)
    assert entity_descs == {}
    assert rel_defs == {}


# ---------------------------------------------------------------------------
# emit_structural_link_alters
# ---------------------------------------------------------------------------


def test_emit_structural_links_for_new_domain_vertices():
    """Each new domain vertex must get CONTAINS_ENTITY pairs from
    Document and DocumentChunk, plus an IN_COMMUNITY pair to Community.
    IS_HEAD_OF / HAS_TAIL are NOT added per-domain-vertex — they live
    at the EntityType ↔ RelationshipType meta-schema layer and the
    original schema declaration covers the only pair we ever traverse.
    """
    proposal = SchemaProposal()
    proposal.add_vertex("Company")
    proposal.add_vertex("Report")

    existing = ExistingSchema(
        vertex_types={
            "Document", "DocumentChunk", "Entity",
            "EntityType", "RelationshipType", "Community",
            "Company", "Report",
        },
        edge_pairs={
            "CONTAINS_ENTITY": {("Document", "Entity"), ("DocumentChunk", "Entity")},
            "IN_COMMUNITY": {("Entity", "Community")},
            "IS_HEAD_OF": {("EntityType", "RelationshipType")},
            "HAS_TAIL": {("RelationshipType", "EntityType")},
        },
    )

    stmts = emit_structural_link_alters(proposal, existing)

    # Each vertex gets two CONTAINS_ENTITY pair-additions plus one
    # IN_COMMUNITY pair-addition: 2*(2+1) = 6.
    assert "ALTER EDGE CONTAINS_ENTITY ADD PAIR (FROM Document, TO Company)" in stmts
    assert "ALTER EDGE CONTAINS_ENTITY ADD PAIR (FROM DocumentChunk, TO Company)" in stmts
    assert "ALTER EDGE CONTAINS_ENTITY ADD PAIR (FROM Document, TO Report)" in stmts
    assert "ALTER EDGE CONTAINS_ENTITY ADD PAIR (FROM DocumentChunk, TO Report)" in stmts
    assert "ALTER EDGE IN_COMMUNITY ADD PAIR (FROM Company, TO Community)" in stmts
    assert "ALTER EDGE IN_COMMUNITY ADD PAIR (FROM Report, TO Community)" in stmts
    # No per-domain-vertex IS_HEAD_OF / HAS_TAIL emitted.
    assert not any("IS_HEAD_OF" in s for s in stmts)
    assert not any("HAS_TAIL" in s for s in stmts)
    assert len(stmts) == 6


def test_emit_structural_links_skips_in_community_when_already_present():
    proposal = SchemaProposal()
    proposal.add_vertex("Company")

    existing = ExistingSchema(
        vertex_types={
            "Document", "DocumentChunk", "Entity",
            "EntityType", "RelationshipType", "Community",
            "Company",
        },
        edge_pairs={
            "CONTAINS_ENTITY": {
                ("Document", "Entity"), ("DocumentChunk", "Entity"),
                ("Document", "Company"), ("DocumentChunk", "Company"),
            },
            "IN_COMMUNITY": {
                ("Entity", "Community"),
                ("Company", "Community"),  # already there
            },
        },
    )

    stmts = emit_structural_link_alters(proposal, existing)
    # CONTAINS_ENTITY pairs already present, IN_COMMUNITY pair already
    # present — nothing left to emit.
    assert stmts == []


def test_emit_structural_links_skips_in_community_when_community_missing():
    """Bare-graph defensive case: if Community isn't on the graph yet,
    don't emit IN_COMMUNITY pairs that would reference an undeclared
    endpoint and fail at schema-change time.
    """
    proposal = SchemaProposal()
    proposal.add_vertex("Company")

    existing = ExistingSchema(
        vertex_types={
            "Document", "DocumentChunk", "Entity",
            "EntityType", "RelationshipType",
            # Community deliberately omitted.
            "Company",
        },
        edge_pairs={
            "CONTAINS_ENTITY": {("Document", "Entity"), ("DocumentChunk", "Entity")},
        },
    )

    stmts = emit_structural_link_alters(proposal, existing)
    assert not any("IN_COMMUNITY" in s for s in stmts)
    # CONTAINS_ENTITY pairs still emitted.
    assert "ALTER EDGE CONTAINS_ENTITY ADD PAIR (FROM Document, TO Company)" in stmts
    assert "ALTER EDGE CONTAINS_ENTITY ADD PAIR (FROM DocumentChunk, TO Company)" in stmts


def test_emit_structural_links_skips_already_present_pairs():
    proposal = SchemaProposal()
    proposal.add_vertex("Company")

    existing = ExistingSchema(
        vertex_types={
            "Document", "DocumentChunk", "Entity",
            "EntityType", "RelationshipType", "Community",
            "Company",
        },
        edge_pairs={
            "CONTAINS_ENTITY": {
                ("Document", "Entity"),
                ("DocumentChunk", "Entity"),
                ("Document", "Company"),  # already there
            },
            "IN_COMMUNITY": {("Entity", "Community")},
            "IS_HEAD_OF": {("EntityType", "RelationshipType")},
            "HAS_TAIL": {("RelationshipType", "EntityType")},
        },
    )

    stmts = emit_structural_link_alters(proposal, existing)
    # Missing CONTAINS_ENTITY pair (DocumentChunk → Company) and the
    # missing IN_COMMUNITY pair (Company → Community).
    assert stmts == [
        "ALTER EDGE CONTAINS_ENTITY ADD PAIR (FROM DocumentChunk, TO Company)",
        "ALTER EDGE IN_COMMUNITY ADD PAIR (FROM Company, TO Community)",
    ]


def test_apply_proposal_emits_structural_links_alongside_domain_adds():
    """End-to-end: apply_proposal runs emit_add_statements (phase 1)
    and emit_structural_link_alters (phase 2) as two separate
    schema-change jobs so TG's job validator never sees an ALTER that
    references a vertex type created in the same job. The fake graph
    has the GraphRAG structural types in place (production invariant —
    init_supportai runs before apply_proposal).
    """
    conn = _FakeConn(
        vertex_types=[
            "Document", "DocumentChunk", "Entity",
            "EntityType", "RelationshipType", "Community",
        ],
        edge_metadata={},
    )
    proposal = SchemaProposal()
    proposal.add_vertex("Company")

    result = apply_proposal(conn, "g", proposal)
    assert result["status"] == "applied"
    schema_calls = [c for c in conn.gsql_calls if "SCHEMA_CHANGE JOB" in c]
    # Two phases: phase 1 = ADD VERTEX, phase 2 = ALTER EDGE ADD PAIR.
    assert len(schema_calls) == 2
    add_cmd, alter_cmd = schema_calls
    # Phase 1 carries the domain ADD VERTEX.
    assert "ADD VERTEX Company" in add_cmd
    assert "ALTER EDGE" not in add_cmd
    # Phase 2 carries every structural-link ALTER, no ADD VERTEX.
    assert "ADD VERTEX" not in alter_cmd
    assert "ALTER EDGE CONTAINS_ENTITY ADD PAIR (FROM Document, TO Company)" in alter_cmd
    assert "ALTER EDGE CONTAINS_ENTITY ADD PAIR (FROM DocumentChunk, TO Company)" in alter_cmd
    # IN_COMMUNITY pair-addition for Company —
    # community retrievers walking domain VTs need this edge present.
    assert "ALTER EDGE IN_COMMUNITY ADD PAIR (FROM Company, TO Community)" in alter_cmd
    # No per-domain-vertex IS_HEAD_OF / HAS_TAIL — those live at
    # EntityType ↔ RelationshipType in the structural schema.
    assert "IS_HEAD_OF ADD PAIR" not in alter_cmd
    assert "HAS_TAIL ADD PAIR" not in alter_cmd
    # Result surfaces both phase job names; the legacy ``job_name`` key
    # remains the first phase for callers that haven't migrated.
    assert len(result["job_names"]) == 2
    assert result["job_name"] == result["job_names"][0]


def test_apply_proposal_skips_structural_links_when_core_types_missing():
    """Defensive: if Document / DocumentChunk / RelationshipType aren't
    on the graph yet, the structural-link emitter must NOT emit
    references that would fail at schema-change time.
    """
    conn = _FakeConn(vertex_types=[], edge_metadata={})
    proposal = SchemaProposal()
    proposal.add_vertex("Company")

    result = apply_proposal(conn, "g", proposal)
    cmd = conn.gsql_calls[0] if conn.gsql_calls else ""
    assert "ALTER EDGE CONTAINS_ENTITY" not in cmd
    assert "ALTER EDGE IN_COMMUNITY" not in cmd
    assert "ALTER EDGE IS_HEAD_OF" not in cmd
    assert "ALTER EDGE HAS_TAIL" not in cmd


# ---------------------------------------------------------------------------
# Reserved-words + gsql-output error checks
# ---------------------------------------------------------------------------


def test_get_gsql_reserved_words_returns_pytigergraph_set():
    from common.db.schema_utils import get_gsql_reserved_words

    words = get_gsql_reserved_words()
    assert isinstance(words, frozenset)
    # Sanity check — pyTigerGraph's set must include core GSQL keywords.
    assert "VERTEX" in words
    assert "FROM" in words
    assert "TYPE" in words


def test_is_reserved_word_case_insensitive():
    from common.db.schema_utils import is_reserved_word

    assert is_reserved_word("VERTEX")
    assert is_reserved_word("vertex")
    assert is_reserved_word("Vertex")
    assert not is_reserved_word("Company")
    assert not is_reserved_word("")


def test_is_structural_type_now_blocks_reserved_words():
    """The structural-type filter — used by parse_gsql_schema to drop
    LLM-proposed names that would error at schema-change time — now
    also drops GSQL reserved words.
    """
    assert is_structural_type("Document")  # graphrag structural — blocked
    assert is_structural_type("VERTEX")  # GSQL keyword — blocked
    assert is_structural_type("Vertex")  # case-insensitive
    assert not is_structural_type("Company")  # ordinary domain name
    assert not is_structural_type("Filing")


def test_gsql_output_error_catches_transport_failures():
    from common.db.schema_utils import gsql_output_error

    # Premature disconnect from the TG gsql server — pyTigerGraph
    # returns this as a string, not an exception.
    err = gsql_output_error("Response ended prematurely")
    assert err is not None
    assert "Response ended prematurely" in err

    err = gsql_output_error("Connection refused")
    assert err is not None
    assert "Connection refused" in err

    # Empty / None inputs are not errors.
    assert gsql_output_error("") is None
    assert gsql_output_error(None) is None


def test_gsql_output_error_catches_pytg_server_errors():
    """The server-error pattern list must flag server-reported errors —
    semantic, syntax, "Failed to create".
    """
    from common.db.schema_utils import gsql_output_error

    err = gsql_output_error("...\nFailed to create vertex types: …\n")
    assert err is not None and "GSQL server error" in err

    err = gsql_output_error("Encountered \"FROM\" at line 12, column 3.\nSyntax Error.")
    assert err is not None

    # A successful "OK" output is not an error.
    assert gsql_output_error("Using graph 'foo'\nOK\n") is None


def test_apply_proposal_returns_error_status_on_gsql_failure():
    """When ``conn.gsql()`` returns a failure-marker string instead of
    raising, apply_proposal must return ``status=error`` rather than
    falsely reporting "applied".
    """
    conn = _FakeConn(
        vertex_types=["Document", "DocumentChunk", "Entity", "RelationshipType"],
        edge_metadata={},
        gsql_response="Response ended prematurely",
    )
    proposal = SchemaProposal()
    proposal.add_vertex("Company")

    result = apply_proposal(conn, "g", proposal)
    assert result["status"] == "error"
    assert "Response ended prematurely" in result["error"]
    # Metadata upsert is skipped on failure.
    assert result["metadata"] == {"entity_types": [], "relationship_types": []}
    assert not conn.upsert_calls


# ---------------------------------------------------------------------------
# UNDIRECTED EDGE support
# ---------------------------------------------------------------------------


def test_parser_recognises_undirected_edge():
    text = """
    ADD VERTEX Company();
    ADD VERTEX Investor();
    ADD UNDIRECTED EDGE PARTNERS_WITH(FROM Company, TO Investor);
    """
    proposal = parse_gsql_schema(text)
    edge = proposal.find_edge("PARTNERS_WITH")
    assert edge is not None
    assert edge.directed is False
    assert edge.pairs == [("Company", "Investor")]


def test_parser_keeps_directed_edge_directed():
    text = """
    ADD VERTEX A();
    ADD VERTEX B();
    ADD DIRECTED EDGE PUSHES(FROM A, TO B);
    """
    edge = parse_gsql_schema(text).find_edge("PUSHES")
    assert edge is not None
    assert edge.directed is True


def test_emitter_writes_undirected_edge_without_reverse_clause():
    proposal = SchemaProposal()
    proposal.add_vertex("A")
    proposal.add_vertex("B")
    proposal.add_edge_pair("PARTNERS_WITH", "A", "B", directed=False)

    stmts = emit_add_statements(proposal)
    edge_stmt = next(s for s in stmts if "EDGE PARTNERS_WITH" in s)
    assert edge_stmt.startswith("ADD UNDIRECTED EDGE")
    assert "REVERSE_EDGE" not in edge_stmt


def test_preview_writes_undirected_edge():
    proposal = SchemaProposal()
    proposal.add_vertex("A")
    proposal.add_vertex("B")
    proposal.add_edge_pair("PARTNERS_WITH", "A", "B", directed=False)

    preview = emit_preview_gsql(proposal)
    assert "ADD UNDIRECTED EDGE PARTNERS_WITH" in preview
    assert "REVERSE_EDGE" not in preview


def test_to_from_dict_preserves_directed_flag():
    p = SchemaProposal()
    p.add_vertex("A")
    p.add_vertex("B")
    p.add_edge_pair("E1", "A", "B", directed=True)
    p.add_edge_pair("E2", "A", "B", directed=False)
    data = p.to_dict()
    p2 = SchemaProposal.from_dict(data)
    assert p2.find_edge("E1").directed is True
    assert p2.find_edge("E2").directed is False
