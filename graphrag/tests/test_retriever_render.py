# Copyright (c) 2024-2026 TigerGraph, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0

"""Unit tests for ``common.db.retriever_render``.

The renderer rewrites stable string anchors in the retriever GSQL
files so domain-VT instances are reachable via their own edges and
walked alongside Entity in the community queries. Tests assert the
substitutions land where expected and that empty domain sets pass the
body through unchanged.
"""

from __future__ import annotations

from common.db.retriever_render import (
    TEMPLATED_RETRIEVERS,
    render_retriever_body,
    render_retrievers,
    resolve_include_entity,
)


_HYBRID_HOP_BODY = """
start = SELECT t FROM start:s -((RELATIONSHIP>|
                                 CONTAINS_ENTITY>|
                                 reverse_CONTAINS_ENTITY>|
                                 IS_AFTER>):e)- :t
"""


_COMMUNITY_WALK_BODY = """
related_chunks = SELECT c FROM Content:c -(<HAS_CONTENT)- DocumentChunk:d -(CONTAINS_ENTITY>)- Entity:v -(IN_COMMUNITY>)- selected_comms:m
"""


def test_render_no_domain_types_passes_through():
    body = _HYBRID_HOP_BODY + _COMMUNITY_WALK_BODY
    out = render_retriever_body(
        body, domain_vts=[], domain_edges=[], include_entity=True
    )
    assert out == body


def test_render_appends_domain_edges_to_hop_pattern():
    body = _HYBRID_HOP_BODY
    out = render_retriever_body(
        body,
        domain_vts=["Company"],
        domain_edges=["PUBLISHES", "INVESTS_IN"],
        include_entity=True,
    )
    # Domain edges land between IS_AFTER> and ):e, sorted alphabetically.
    assert "IS_AFTER>|INVESTS_IN>|PUBLISHES>):e" in out
    # Existing structural edges preserved.
    assert "RELATIONSHIP>" in out
    assert "CONTAINS_ENTITY>" in out


def test_render_expands_community_member_with_entity():
    body = _COMMUNITY_WALK_BODY
    out = render_retriever_body(
        body,
        domain_vts=["Company", "InvestmentFund"],
        domain_edges=[],
        include_entity=True,
    )
    # Member set sorts alphabetically with Entity prepended.
    assert "(Entity|Company|InvestmentFund):v -(IN_COMMUNITY>" in out


def test_render_excludes_entity_when_flag_false():
    body = _COMMUNITY_WALK_BODY
    out = render_retriever_body(
        body,
        domain_vts=["Company", "Report"],
        domain_edges=[],
        include_entity=False,
    )
    assert "(Company|Report):v -(IN_COMMUNITY>" in out
    # Entity must not appear in the substituted member set.
    assert "Entity|" not in out
    assert "|Entity)" not in out


def test_render_single_domain_vt_drops_parens():
    body = _COMMUNITY_WALK_BODY
    out = render_retriever_body(
        body,
        domain_vts=["Company"],
        domain_edges=[],
        include_entity=False,
    )
    # Single VT — no surrounding parens needed.
    assert "Company:v -(IN_COMMUNITY>" in out
    assert "(Company)" not in out


def test_render_empty_edges_no_change_to_hop():
    body = _HYBRID_HOP_BODY
    out = render_retriever_body(
        body, domain_vts=["Company"], domain_edges=[], include_entity=True
    )
    assert out == body


def test_render_retrievers_targets_only_known_set(tmp_path):
    """The renderer ships with a curated retriever list — graphs without
    the file on disk must skip rather than blocking the pipeline.
    """
    (tmp_path / "GraphRAG_Hybrid_Search.gsql").write_text(_HYBRID_HOP_BODY)
    (tmp_path / "GraphRAG_Community_Search.gsql").write_text(_COMMUNITY_WALK_BODY)
    # Hybrid_Vector_Search and Community_Vector_Search intentionally
    # missing — the renderer logs and keeps going.
    rendered = render_retrievers(
        domain_vts=["Company"],
        domain_edges=["PUBLISHES"],
        include_entity=True,
        retriever_dir=str(tmp_path),
    )
    assert "GraphRAG_Hybrid_Search" in rendered
    assert "GraphRAG_Community_Search" in rendered
    # Substitutions reached the rendered output:
    assert "IS_AFTER>|PUBLISHES>):e" in rendered["GraphRAG_Hybrid_Search"]
    assert "(Entity|Company):v -(IN_COMMUNITY>" in rendered[
        "GraphRAG_Community_Search"
    ]
    # Missing files are simply skipped, not raised.
    assert "GraphRAG_Hybrid_Vector_Search" not in rendered


def test_resolve_include_entity_auto_default_off_with_schema():
    """Unset config + domain schema present → auto-default to False
    (typed-purist retrieval). Users who declared a schema get strict
    behaviour without having to flip a flag.
    """
    cfg = {}.get  # nothing configured
    assert resolve_include_entity(cfg, has_domain_schema=True) is False


def test_resolve_include_entity_auto_default_on_without_schema():
    """Unset config + no domain schema → True (moot — Entity is the
    only path the retrievers can walk).
    """
    cfg = {}.get
    assert resolve_include_entity(cfg, has_domain_schema=False) is True


def test_resolve_include_entity_explicit_true_overrides_auto():
    """Even with a domain schema, explicit `True` keeps Entity in the
    traversal — for lenient deployments where unmatched extractions
    still carry useful context.
    """
    cfg = {"retrieval_include_entity": True}.get
    assert resolve_include_entity(cfg, has_domain_schema=True) is True


def test_resolve_include_entity_explicit_false_overrides_auto():
    cfg = {"retrieval_include_entity": False}.get
    assert resolve_include_entity(cfg, has_domain_schema=False) is False


def test_templated_retrievers_list_is_stable():
    """The curated retriever list is part of the public contract for
    schema-apply / post-Louvain trigger hooks. Lock it down so silent
    drift triggers a test failure.
    """
    assert TEMPLATED_RETRIEVERS == (
        "GraphRAG_Hybrid_Search",
        "GraphRAG_Hybrid_Vector_Search",
        "GraphRAG_Community_Search",
        "GraphRAG_Community_Vector_Search",
    )
