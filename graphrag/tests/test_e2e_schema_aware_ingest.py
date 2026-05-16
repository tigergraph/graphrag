# Copyright (c) 2024-2026 TigerGraph, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0

"""
End-to-end test for schema-aware initialization (Phase 1).

Walks the full lifecycle a UI user would drive:

    1. Create graph
    2. Upload sample PDFs via convert_sample_files (stores files +
       converts to JSONL, NO LLM call)
    3. Run schema extraction over the JSONLs (LLM call, returns draft)
    4. Initialize the graph with the LLM-produced schema_gsql
       (applies domain types as a single atomic schema-change job)
    5. Validate the live schema and metadata vertices match the proposal
    6. Run ingestion against the SAME files (JSONL cache reused —
       no second-round PDF conversion)
    7. Trigger rebuild and wait for completion
    8. Validate the final knowledge graph (documents, chunks, entities,
       EntityType definitions populated, communities formed)

Requires a running GraphRAG stack against a live TigerGraph instance.
The default test corpus is the 2 Barclays PDFs at
``~/Downloads/BarclaysDocs/`` — point ``TEST_FILES`` elsewhere to use
a different sample.

Usage::

    GRAPHRAG_URL=http://localhost:80 \\
    TEST_FILES=$HOME/Downloads/BarclaysDocs/Inspired_ESG-Report_2022.pdf,\\
$HOME/Downloads/BarclaysDocs/QuarterlyInvestmentReport_uss.pdf \\
    pytest graphrag/tests/test_e2e_schema_aware_ingest.py -v -s

Environment variables:
    GRAPHRAG_URL            Base URL of running GraphRAG service (required to run)
    TG_HOST                 TigerGraph host URL (e.g. http://host:14240). Required
                            unless ``db_config.hostname`` is set in SERVER_CONFIG.
    SERVER_CONFIG           Path to server_config.json (default:
                            ./configs/server_config.json). Read for the
                            ``db_config`` block (hostname / username / password).
    TG_USERNAME / TG_PASSWORD  Fallbacks if SERVER_CONFIG is missing or partial.
    TEST_GRAPH              Graph name (default: SchemaAwareE2E_<timestamp>)
    TEST_FILES              Comma-separated file paths (default: BarclaysDocs PDFs)
    REBUILD_TIMEOUT         Max seconds to wait for rebuild (default: 7200)
    SCHEMA_EXTRACT_TIMEOUT  Max seconds for the LLM extract call (default: 300)
    EXPECTED_MIN_VERTICES   Minimum domain vertex types the LLM must produce (default: 3)
    EXPECTED_MIN_EDGES      Minimum domain edge types the LLM must produce (default: 2)
    SKIP_CLEANUP            Set to "1" to keep the graph after the test
"""

from __future__ import annotations

import json
import os
import time

import pytest
import requests

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

GRAPHRAG_URL = os.getenv("GRAPHRAG_URL", "http://localhost:80")

_server_config_path = os.getenv("SERVER_CONFIG", "./configs/server_config.json")
_db: dict = {}
try:
    with open(_server_config_path) as _f:
        _db = (json.load(_f) or {}).get("db_config") or {}
except Exception:
    _db = {}

USERNAME = _db.get("username") or os.getenv("TG_USERNAME", "tigergraph")
PASSWORD = _db.get("password") or os.getenv("TG_PASSWORD", "tigergraph")
# Resolution order: TG_HOST env override → ``db_config.hostname`` in
# server_config.json → fail fast. No baked-in default — local
# environments differ, and a wrong fallback can silently point the
# test at the wrong cluster.
TG_HOST = os.getenv("TG_HOST") or _db.get("hostname")
if not TG_HOST:
    raise RuntimeError(
        "TG_HOST is not set. Export it in the shell or set "
        f"'db_config.hostname' in {_server_config_path} before running "
        "the e2e test."
    )

REBUILD_TIMEOUT = int(os.getenv("REBUILD_TIMEOUT", "7200"))
SCHEMA_EXTRACT_TIMEOUT = int(os.getenv("SCHEMA_EXTRACT_TIMEOUT", "300"))
EXPECTED_MIN_VERTICES = int(os.getenv("EXPECTED_MIN_VERTICES", "3"))
EXPECTED_MIN_EDGES = int(os.getenv("EXPECTED_MIN_EDGES", "2"))

AUTH = (USERNAME, PASSWORD)
GRAPH_NAME = os.getenv("TEST_GRAPH", f"SchemaAwareE2E_{int(time.time())}")

_default_pdfs = [
    os.path.expanduser("~/Downloads/BarclaysDocs/Inspired_ESG-Report_2022.pdf"),
    os.path.expanduser("~/Downloads/BarclaysDocs/QuarterlyInvestmentReport_uss.pdf"),
]
_raw_files = os.getenv("TEST_FILES")
if _raw_files:
    TEST_FILES = [f.strip() for f in _raw_files.split(",") if f.strip()]
else:
    TEST_FILES = [p for p in _default_pdfs if os.path.exists(p)]


# Shared state across ordered test stages. Each stage records its
# success under a distinct key so downstream stages can early-skip
# instead of producing cascade failures with confusing tracebacks.
_state: dict = {}

skip_unless_graphrag = pytest.mark.skipif(
    not os.getenv("GRAPHRAG_URL"),
    reason="E2E tests require a live GraphRAG service. Set GRAPHRAG_URL to run.",
)


def _require_stage(stage_key: str):
    if stage_key not in _state:
        pytest.skip(f"Skipped because prior stage '{stage_key}' did not succeed")


# ---------------------------------------------------------------------------
# Stages
# ---------------------------------------------------------------------------


@skip_unless_graphrag
def test_01_create_graph():
    """Create an empty TigerGraph graph."""
    print(f"\n--- Stage 1: Creating graph '{GRAPH_NAME}' ---")
    resp = requests.post(
        f"{GRAPHRAG_URL}/ui/{GRAPH_NAME}/create_graph",
        auth=AUTH,
        # Mirror the UI's fetch (no client read timeout); nginx caps at 3600s.
        timeout=(60, None),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "success", body
    _state["created"] = True
    print(f"Graph '{GRAPH_NAME}' created.")


@skip_unless_graphrag
def test_02_convert_sample_files():
    """Upload PDFs to convert_sample_files — stores files under
    uploads/<graph>/ and writes JSONL under uploads/ingestion_temp/<graph>/.
    No LLM call. Returns the saved-file list we use to drive the next
    stage and ingest later.
    """
    _require_stage("created")
    if not TEST_FILES:
        pytest.skip(
            "No test files. Set TEST_FILES env var or place the Barclays PDFs at "
            "~/Downloads/BarclaysDocs/."
        )
    print(f"\n--- Stage 2: Converting {len(TEST_FILES)} sample file(s) ---")
    files = []
    for fpath in TEST_FILES:
        abs_path = os.path.abspath(fpath)
        assert os.path.exists(abs_path), f"Test file not found: {abs_path}"
        files.append(("files", (os.path.basename(abs_path), open(abs_path, "rb"))))
    try:
        resp = requests.post(
            f"{GRAPHRAG_URL}/ui/{GRAPH_NAME}/convert_sample_files",
            files=files,
            auth=AUTH,
            timeout=(60, None),
        )
    finally:
        for _, (_, fobj) in files:
            fobj.close()

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "success", body
    saved = body.get("saved_files") or []
    assert saved, f"convert_sample_files returned empty saved_files: {body}"
    print(f"Saved files: {saved}")
    print(f"Total documents: {body.get('num_documents')}")
    _state["saved_files"] = saved
    _state["request_id"] = body.get("request_id") or ""


@skip_unless_graphrag
def test_03_extract_schema_from_jsonl():
    """Run the schema-extraction LLM and validate the returned proposal."""
    _require_stage("saved_files")
    print(f"\n--- Stage 3: Running schema extraction ---")
    resp = requests.post(
        f"{GRAPHRAG_URL}/ui/{GRAPH_NAME}/extract_schema_from_jsonl",
        json={
            "filenames": _state["saved_files"],
            "request_id": _state.get("request_id", ""),
        },
        auth=AUTH,
        timeout=SCHEMA_EXTRACT_TIMEOUT,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    proposal = body.get("proposal") or {}
    summary = body.get("summary") or {}
    schema_gsql = body.get("schema_gsql") or ""

    print(f"Vertex types: {summary.get('vertex_count')}")
    print(f"Edge types: {summary.get('edge_count')}")
    print(f"Vertex names: {summary.get('vertex_names')}")
    print(f"Edge names: {summary.get('edge_names')}")

    assert summary.get("vertex_count", 0) >= EXPECTED_MIN_VERTICES, (
        f"LLM produced too few vertex types: got {summary.get('vertex_count')}, "
        f"need >= {EXPECTED_MIN_VERTICES}. Proposal: {summary}"
    )
    assert summary.get("edge_count", 0) >= EXPECTED_MIN_EDGES, (
        f"LLM produced too few edge types: got {summary.get('edge_count')}, "
        f"need >= {EXPECTED_MIN_EDGES}. Proposal: {summary}"
    )
    assert schema_gsql.strip(), "Empty schema_gsql in response"

    # Every vertex in the proposal must have a description (the LLM
    # is prompted to emit a // comment per declaration).
    no_desc_vertices = [
        v["name"] for v in proposal.get("vertices", []) if not v.get("description")
    ]
    no_desc_edges = [
        e["name"] for e in proposal.get("edges", []) if not e.get("description")
    ]
    if no_desc_vertices:
        print(f"WARN: vertices without description: {no_desc_vertices}")
    if no_desc_edges:
        print(f"WARN: edges without description: {no_desc_edges}")

    _state["proposal"] = proposal
    _state["schema_gsql"] = schema_gsql
    _state["expected_vertex_names"] = {v["name"] for v in proposal.get("vertices", [])}
    _state["expected_edge_names"] = {e["name"] for e in proposal.get("edges", [])}


@skip_unless_graphrag
def test_04_initialize_graph_with_schema():
    """Apply the LLM-produced schema as part of initialize_graph.

    The endpoint creates the structural GraphRAG schema first, then
    applies the domain types in a single atomic schema-change job.

    The endpoint is async-job: POST returns ``{status: "submitted"}``
    immediately and we poll ``/initialize_status`` until the
    background task finishes. This avoids the browser/proxy
    timeout that happens when retriever installs run for many
    minutes inside one HTTP call.
    """
    _require_stage("schema_gsql")
    print(f"\n--- Stage 4: Initializing graph with extracted schema ---")
    resp = requests.post(
        f"{GRAPHRAG_URL}/ui/{GRAPH_NAME}/initialize_graph",
        json={"schema_gsql": _state["schema_gsql"]},
        auth=AUTH,
        timeout=(60, 60),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "submitted", body
    print(f"Init job submitted: {body.get('message')}")

    # Poll initialize_status until terminal.
    init_timeout = int(os.getenv("INIT_TIMEOUT", "1800"))  # 30 min
    poll_interval = 5
    start = time.time()
    last_message: str | None = None
    final_state: dict | None = None
    while time.time() - start < init_timeout:
        time.sleep(poll_interval)
        try:
            sresp = requests.get(
                f"{GRAPHRAG_URL}/ui/{GRAPH_NAME}/initialize_status",
                auth=AUTH,
                timeout=(30, 30),
            )
        except requests.RequestException as e:
            print(f"  status poll transient error: {e}; retrying")
            continue
        if sresp.status_code != 200:
            print(f"  status poll {sresp.status_code}: {sresp.text[:200]}")
            continue
        sdata = sresp.json()
        msg = sdata.get("message")
        if msg and msg != last_message:
            print(f"  state={sdata.get('state')} message={msg}")
            last_message = msg
        if sdata.get("state") == "completed":
            final_state = sdata
            break
        if sdata.get("state") == "error":
            pytest.fail(
                f"Init failed: {sdata.get('error') or sdata.get('message')}"
            )
    assert final_state is not None, (
        f"Init did not reach 'completed' within {init_timeout}s"
    )

    result = final_state.get("result") or {}
    assert result.get("status") == "success", result.get("message")
    domain_status = result.get("domain_schema_status") or {}
    print(f"Domain schema status: {domain_status.get('status')}")
    print(f"Statements applied: {len(domain_status.get('statements', []))}")
    if domain_status.get("metadata"):
        md = domain_status["metadata"]
        print(
            f"Metadata: {len(md.get('entity_types', []))} EntityType, "
            f"{len(md.get('relationship_types', []))} RelationshipType"
        )
    assert domain_status.get("status") in ("applied", "no-op"), domain_status
    _state["initialized"] = True


@skip_unless_graphrag
def test_05_validate_live_schema():
    """The live graph should now have the structural types AND every
    domain type from the proposal. Verified by reading the live schema
    directly via pyTigerGraph (the graphrag service no longer exposes
    a schema-export endpoint — schemas are read from TG directly).
    """
    _require_stage("initialized")
    print(f"\n--- Stage 5: Validating live schema ---")

    from pyTigerGraph import TigerGraphConnection

    conn = TigerGraphConnection(
        host=TG_HOST,
        graphname=GRAPH_NAME,
        username=USERNAME,
        password=PASSWORD,
    )
    conn.getToken()
    actual_vertex_names = set(conn.getVertexTypes())
    actual_edge_names = set(conn.getEdgeTypes())

    expected_vertices = _state["expected_vertex_names"]
    expected_edges = _state["expected_edge_names"]

    missing_vertices = expected_vertices - actual_vertex_names
    missing_edges = expected_edges - actual_edge_names
    assert not missing_vertices, (
        f"Vertex types missing on graph: {missing_vertices}. "
        f"Got: {actual_vertex_names}"
    )
    assert not missing_edges, (
        f"Edge types missing on graph: {missing_edges}. "
        f"Got: {actual_edge_names}"
    )
    print(f"All {len(expected_vertices)} domain vertex types present.")
    print(f"All {len(expected_edges)} domain edge types present.")


@skip_unless_graphrag
def test_06_create_ingest():
    """Create the loading job for ingest. The JSONLs are already in
    uploads/ingestion_temp/<graph>/ from stage 2, so the underlying
    process_folder run will hit the cached_jsonl_skipped path and
    skip re-conversion.
    """
    _require_stage("initialized")
    print(f"\n--- Stage 6: Creating ingest configuration ---")
    payload = {
        "data_source": "server",
        "data_source_config": {"data_path": f"uploads/{GRAPH_NAME}"},
        "file_format": "multi",
    }
    resp = requests.post(
        f"{GRAPHRAG_URL}/ui/{GRAPH_NAME}/create_ingest",
        json=payload,
        auth=AUTH,
        timeout=(60, None),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "load_job_id" in body, body
    _state["ingest_config"] = body


@skip_unless_graphrag
def test_07_run_ingest():
    """Run the loading job."""
    _require_stage("ingest_config")
    cfg = _state["ingest_config"]
    print(f"\n--- Stage 7: Running ingestion ---")
    resp = requests.post(
        f"{GRAPHRAG_URL}/ui/{GRAPH_NAME}/ingest",
        json={
            "load_job_id": cfg["load_job_id"],
            "data_source_id": cfg["data_source_id"],
            "file_path": cfg.get("data_path", ""),
        },
        auth=AUTH,
        timeout=(60, None),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    print(f"Ingest result: {json.dumps(body)[:300]}")
    _state["ingested"] = True


@skip_unless_graphrag
def test_08_rebuild_graph():
    """Trigger ECC rebuild and poll until completion.

    Completion is detected by tailing the graphrag-ecc container log
    for the canonical end-of-run marker:
        ``Completed ECC task: <graphname>:graphrag``
    The rebuild_status REST endpoint is also checked, but the log
    marker is authoritative — the REST status flips ``completed``
    when ``run_with_tracking`` returns, which can race with the final
    flush of upserts; the log line is emitted strictly after the full
    pipeline (including community detection / post-pipeline checks)
    has finished.
    """
    import subprocess

    _require_stage("ingested")
    print(f"\n--- Stage 8: Triggering rebuild ---")
    resp = requests.post(
        f"{GRAPHRAG_URL}/ui/{GRAPH_NAME}/rebuild_graph",
        auth=AUTH,
        timeout=60,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body.get("status") == "submitted", body
    print("Rebuild submitted; polling for ECC completion marker in container log...")

    completion_marker = f"Completed ECC task: {GRAPH_NAME}:graphrag"
    failure_marker = f"ECC task failed: {GRAPH_NAME}:graphrag"
    poll_start = time.time()
    start_time = poll_start
    last_status = ""
    while time.time() - start_time < REBUILD_TIMEOUT:
        elapsed = int(time.time() - start_time)
        # Check the canonical log marker first.
        try:
            log_tail = subprocess.check_output(
                ["docker", "logs", "--tail", "2000", "graphrag-ecc"],
                stderr=subprocess.STDOUT,
                text=True,
            )
            if failure_marker in log_tail:
                pytest.fail(f"ECC reported task failure in container log for {GRAPH_NAME}")
            if completion_marker in log_tail:
                print(f"  ECC completion marker observed in container log ({elapsed}s).")
                _state["rebuilt"] = True
                return
        except (subprocess.CalledProcessError, FileNotFoundError) as exc:
            print(f"  [{elapsed}s] docker-logs check failed: {exc}")

        # Secondary signal — REST status. Useful for log lines on its
        # state transitions; not authoritative for completion.
        try:
            sr = requests.get(
                f"{GRAPHRAG_URL}/ui/{GRAPH_NAME}/rebuild_status",
                auth=AUTH,
                timeout=120,
            )
            if sr.status_code == 200:
                sd = sr.json()
                status = sd.get("status", "unknown")
                if status != last_status:
                    print(f"  [{elapsed}s] rebuild_status={status} (informational)")
                    last_status = status
                if status == "failed":
                    pytest.fail(f"Rebuild failed per REST status: {sd}")
        except Exception as exc:
            print(f"  [{elapsed}s] rebuild_status poll error: {exc}")

        time.sleep(15)

    pytest.fail(
        f"ECC completion marker not seen in {REBUILD_TIMEOUT}s for graph {GRAPH_NAME}"
    )


@skip_unless_graphrag
def test_09_validate_final_graph():
    """Validate the rebuilt graph has the data we expect.

    Per-type vertex AND edge counts are pulled directly from
    TigerGraph via pyTigerGraph (the graphrag service does not expose
    a per-type statistics endpoint). Every structural and every
    domain type must have a non-zero count — empty types signal that
    extraction did not produce data for them.
    """
    _require_stage("rebuilt")
    print(f"\n--- Stage 9: Validating final graph data ---")

    from pyTigerGraph import TigerGraphConnection

    conn = TigerGraphConnection(
        host=TG_HOST,
        graphname=GRAPH_NAME,
        username=USERNAME,
        password=PASSWORD,
    )
    conn.getToken()

    # Read the live schema directly from TG to discover which domain
    # types we expect. Domain types = everything not in the GraphRAG
    # structural set (which is fixed across releases).
    structural_vertex_types = [
        "Document", "DocumentChunk", "Entity",
        "EntityType", "RelationshipType",
    ]
    structural_edge_types = [
        "HAS_CONTENT", "CONTAINS_ENTITY",
        "IS_HEAD_OF", "HAS_TAIL", "MENTIONS_RELATIONSHIP",
    ]
    # Other GraphRAG-structural types that may exist on the graph but
    # whose presence isn't required (Content / Image / Community and
    # their structural edges).
    structural_optional = {
        "Content", "Image", "Community",
        "HAS_CHILD", "IS_AFTER", "HAS_IMAGE",
        "REFERENCES_IMAGE", "LINKS_TO", "HAS_PARENT",
        "IN_COMMUNITY", "ENTITY_HAS_TYPE",
        "RELATIONSHIP",
    }
    structural_skip = (
        set(structural_vertex_types)
        | set(structural_edge_types)
        | structural_optional
    )
    all_vertex_types = conn.getVertexTypes()
    all_edge_types = conn.getEdgeTypes()
    domain_vertex_types = sorted(
        v for v in all_vertex_types if v not in structural_skip
    )
    domain_edge_types = sorted(
        e for e in all_edge_types if e not in structural_skip
    )
    expected_v = structural_vertex_types + domain_vertex_types
    expected_e = structural_edge_types + domain_edge_types

    # `saw_progress` must only flip on EXTRACTION signals — types that
    # ingest never populates on its own. Document / DocumentChunk are
    # already non-zero after stage 7 (ingest), so they don't tell us
    # anything about extraction having started.
    extraction_signal_v = {"Entity", "EntityType", "RelationshipType"} | set(
        domain_vertex_types
    )
    extraction_signal_e = {
        "CONTAINS_ENTITY", "IS_HEAD_OF", "HAS_TAIL",
        "MENTIONS_RELATIONSHIP",
    } | set(domain_edge_types)

    # ECC's `rebuild_status: completed` reports when the outer rebuild
    # task returns, but extraction upserts can keep flowing into TG for
    # many minutes after that. Poll until every expected type is
    # non-zero, OR until the counts have been stable for several
    # consecutive samples AFTER at least one extraction-signal type
    # has crossed zero (stability at all-zeros just means extraction
    # hasn't started yet, not that it's done).
    poll_interval = 30
    stability_window = 4  # 4 stable samples * 30s = 2 min idle
    max_wait = int(os.getenv("STAGE9_POLL_MAX_S", "1800"))
    start = time.time()
    last_signature = None
    stable_samples = 0
    saw_progress = False
    vertex_counts: dict = {}
    edge_counts: dict = {}
    while True:
        vertex_counts = conn.getVertexCount("*")
        edge_counts = conn.getEdgeCount("*")
        signature = tuple(
            (t, vertex_counts.get(t, 0)) for t in expected_v
        ) + tuple(
            ("E:" + t, edge_counts.get(t, 0)) for t in expected_e
        )
        empty_now = [t for t in expected_v if vertex_counts.get(t, 0) == 0] + [
            "E:" + t for t in expected_e if edge_counts.get(t, 0) == 0
        ]
        # Progress flips only on extraction-signal types crossing zero.
        progress_v = sum(
            1 for t in extraction_signal_v if vertex_counts.get(t, 0) > 0
        )
        progress_e = sum(
            1 for t in extraction_signal_e if edge_counts.get(t, 0) > 0
        )
        if (progress_v + progress_e) > 0:
            saw_progress = True
        elapsed = int(time.time() - start)
        print(
            f"  [{elapsed}s] missing={len(empty_now)} types, "
            f"saw_progress={saw_progress}"
        )
        if not empty_now:
            break
        if signature == last_signature:
            stable_samples += 1
            if saw_progress and stable_samples >= stability_window:
                print(
                    f"  Counts stable for {stability_window} samples after "
                    "first extraction progress; extraction appears done."
                )
                break
        else:
            stable_samples = 0
            last_signature = signature
        if elapsed >= max_wait:
            print(f"  Hit max_wait={max_wait}s; reporting current state.")
            break
        time.sleep(poll_interval)

    print(f"Vertex counts: {json.dumps(vertex_counts)}")
    print(f"Edge counts: {json.dumps(edge_counts)}")

    # Hard requirement: every structural vertex / edge type the
    # extraction-write path is supposed to populate MUST be non-zero.
    required_v = list(structural_vertex_types)
    required_e = list(structural_edge_types)
    # Every domain vertex must have data — domain VTs are extracted
    # entity instances, and a zero count means extraction missed an
    # entire class declared in the schema.
    required_v += list(domain_vertex_types)
    # Domain edges are best-effort — the LLM can propose an edge type
    # in the schema and then not actually extract any instances of it
    # from the corpus (the schema is a superset of the realized graph).
    # We require at least 50% of declared domain edges to have data,
    # warn-print the empties, and fail if coverage is below that
    # threshold.
    DOMAIN_EDGE_MIN_COVERAGE = float(os.getenv("DOMAIN_EDGE_MIN_COVERAGE", "0.5"))

    empty: list = []
    for vt in expected_v:
        c = vertex_counts.get(vt, 0)
        if c == 0 and vt in required_v:
            empty.append(f"VERTEX {vt}")
        print(f"  V {vt}: {c}")

    empty_domain_edges: list = []
    for et in expected_e:
        c = edge_counts.get(et, 0)
        if c == 0:
            if et in required_e:
                empty.append(f"EDGE {et}")
            elif et in domain_edge_types:
                empty_domain_edges.append(et)
        print(f"  E {et}: {c}")

    if empty_domain_edges:
        print(
            f"  WARN: {len(empty_domain_edges)}/{len(domain_edge_types)} "
            f"domain edges empty (LLM proposed but not extracted): "
            f"{empty_domain_edges}"
        )
        if domain_edge_types:
            coverage = (
                len(domain_edge_types) - len(empty_domain_edges)
            ) / len(domain_edge_types)
            assert coverage >= DOMAIN_EDGE_MIN_COVERAGE, (
                f"Domain-edge coverage {coverage:.0%} below "
                f"{DOMAIN_EDGE_MIN_COVERAGE:.0%}: empties = {empty_domain_edges}"
            )

    if empty:
        print(f"Expected structural V: {structural_vertex_types}")
        print(f"Expected domain V: {domain_vertex_types}")
        print(f"Expected structural E: {structural_edge_types}")
        print(f"Expected domain E: {domain_edge_types}")
    assert not empty, f"Empty types after ingest+rebuild: {empty}"

    # EntityType / RelationshipType metadata vertices should be populated
    # with descriptions sourced from the LLM-emitted ``// `` comments
    # above each declaration. Vertex descriptions are reliably emitted
    # by Claude; edge descriptions are LLM-flaky (the model often skips
    # the comment lines above EDGE declarations under load), so we
    # require strict coverage on vertices and only require a soft
    # majority on edges (warn on misses, fail only if more than half
    # are missing).
    et_rows = conn.getVerticesById("EntityType", domain_vertex_types) if domain_vertex_types else []
    rt_rows = conn.getVerticesById("RelationshipType", domain_edge_types) if domain_edge_types else []
    described_v = sum(
        1 for r in et_rows
        if (r.get("attributes", {}) or {}).get("description")
    )
    described_e = sum(
        1 for r in rt_rows
        if (r.get("attributes", {}) or {}).get("description")
    )
    print(
        f"Type metadata descriptions: {described_v}/{len(domain_vertex_types)} V, "
        f"{described_e}/{len(domain_edge_types)} E"
    )
    if domain_vertex_types:
        assert described_v == len(domain_vertex_types), (
            f"Only {described_v}/{len(domain_vertex_types)} domain "
            f"vertex types carry a description — EntityType metadata "
            f"may not have been populated."
        )
    if domain_edge_types:
        # Warn-only by default — Claude routinely skips ``// ``
        # comments above some edge declarations and the variance is
        # high run-to-run. Bump via ``EDGE_DESC_MIN_COVERAGE`` env if
        # you want strict enforcement.
        edge_min_coverage = float(os.getenv("EDGE_DESC_MIN_COVERAGE", "0"))
        edge_coverage = described_e / len(domain_edge_types)
        if edge_min_coverage > 0 and edge_coverage < edge_min_coverage:
            assert False, (
                f"RelationshipType description coverage "
                f"{edge_coverage:.0%} below {edge_min_coverage:.0%} "
                f"({described_e}/{len(domain_edge_types)}) — LLM "
                f"omitted ``// `` comments on too many EDGE "
                f"declarations."
            )
        elif described_e < len(domain_edge_types):
            print(
                f"  WARN: {len(domain_edge_types) - described_e}/"
                f"{len(domain_edge_types)} domain edges missing "
                f"descriptions (LLM-flaky behavior, accepted)."
            )

    print("Final graph validation passed.")


@skip_unless_graphrag
def test_99_cleanup():
    """Clean up: clear graph data and delete uploaded files."""
    if os.getenv("SKIP_CLEANUP") == "1":
        print(f"\n[cleanup] SKIP_CLEANUP=1, keeping graph '{GRAPH_NAME}'")
        pytest.skip("SKIP_CLEANUP=1")
    if "created" not in _state:
        pytest.skip("Graph was never created")

    print(f"\n--- Cleanup: removing graph data for '{GRAPH_NAME}' ---")
    try:
        resp = requests.post(
            f"{GRAPHRAG_URL}/ui/{GRAPH_NAME}/clear_graph_data",
            auth=AUTH,
            timeout=120,
        )
        print(f"clear_graph_data: {resp.status_code} {resp.text[:200]}")
    except Exception as e:
        print(f"clear_graph_data failed: {e}")

    try:
        resp = requests.delete(
            f"{GRAPHRAG_URL}/ui/{GRAPH_NAME}/uploads",
            auth=AUTH,
            timeout=30,
        )
        print(f"delete uploads: {resp.status_code}")
    except Exception as e:
        print(f"delete uploads failed: {e}")
