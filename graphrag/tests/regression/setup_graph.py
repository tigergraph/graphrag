"""GraphRAG Regression — Graph Setup

Creates and populates a GraphRAG knowledge graph from a dataset folder.

Two modes:
  1. Fresh setup (default) — creates graph, initialises schema, uploads &
     ingests documents, triggers ECC rebuild.
  2. Load exported graph (--load-exported) — restore from ExportedGraph/
     folder (stub — implement load_exported() when confirmed).

The generated graphname is printed last so run_setup.sh can capture it.

Run via:
    ./graphrag/tests/regression/run_setup.sh --dataset <name>
    ./graphrag/tests/regression/run_setup.sh --dataset <name> --graphname <override>
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
import uuid
from typing import List, Optional

import httpx

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

_REBUILD_POLL_SECS = 30


class SetupError(Exception):
    pass


# ─── HTTP helpers ─────────────────────────────────────────────────────────────

class _Client:
    def __init__(self, base_url: str, username: str, password: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.auth     = (username, password)

    def post(self, path: str, json_body=None) -> dict:
        resp = httpx.post(
            f"{self.base_url}{path}",
            json=json_body,
            auth=self.auth,
            timeout=None,
        )
        resp.raise_for_status()
        return resp.json()

    def get(self, path: str) -> dict:
        resp = httpx.get(
            f"{self.base_url}{path}",
            auth=self.auth,
            timeout=None,
        )
        resp.raise_for_status()
        return resp.json()

    def upload_file(self, path: str, filepath: str) -> dict:
        fname = os.path.basename(filepath)
        with open(filepath, "rb") as fh:
            resp = httpx.post(
                f"{self.base_url}{path}",
                auth=self.auth,
                params={"overwrite": "true"},
                files={"files": (fname, fh)},
                timeout=None,
            )
        resp.raise_for_status()
        return resp.json()


# ─── Setup steps ──────────────────────────────────────────────────────────────

def _create_graph(client: _Client, graphname: str) -> None:
    result = client.post(f"/ui/{graphname}/create_graph")
    status = result.get("status", "")
    if status == "error":
        msg = result.get("message", "")
        if "already exists" in msg.lower():
            print(f"        Graph already exists — continuing.", flush=True)
        else:
            raise SetupError(f"create_graph: {msg}")


def _initialize(client: _Client, graphname: str) -> None:
    try:
        result = client.post(f"/ui/{graphname}/initialize_graph")
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 409:
            try:
                body   = e.response.json()
                detail = body.get("detail", {})
                if isinstance(detail, dict) and detail.get("reason") == "structural_present":
                    print(f"        Schema already installed — skipping init.", flush=True)
                    return
            except Exception:
                pass
        raise SetupError(f"initialize ({e.response.status_code}): {e.response.text[:300]}") from e

    state = str(result.get("status", result.get("state", ""))).lower()
    if state in ("submitted", "running", "in_progress"):
        _poll_initialize(client, graphname)


def _poll_initialize(client: _Client, graphname: str, poll_secs: int = 5) -> None:
    print(f"        Polling initialization status ...", flush=True)
    last_msg = None
    while True:
        status  = client.get(f"/ui/{graphname}/initialize_status")
        state   = str(status.get("state", "")).lower()
        message = str(status.get("message") or "")
        error   = status.get("error")

        if message and message != last_msg:
            print(f"        [{graphname}] {message}", flush=True)
            last_msg = message

        if state == "completed":
            return
        if state == "failed" or error:
            raise SetupError(f"Initialization failed: {error or message}")
        time.sleep(poll_secs)


def _ingest_documents(client: _Client, graphname: str, doc_files: List[str]) -> None:
    # Upload
    print(f"        Uploading {len(doc_files)} file(s) ...", flush=True)
    for fp in doc_files:
        result = client.upload_file(f"/ui/{graphname}/uploads", fp)
        if result.get("status") not in ("success", "conflict"):
            raise SetupError(f"Upload returned unexpected status: {result}")
        print(f"        + {os.path.basename(fp)}", flush=True)

    # Derive and validate file format from uploaded files
    extensions = {
        os.path.splitext(fp)[1].lstrip(".").lower()
        for fp in doc_files
        if os.path.splitext(fp)[1]
    }
    if len(extensions) != 1:
        raise SetupError(
            f"data/ must contain one file type for ingest; found: {sorted(extensions)}"
        )
    file_format = extensions.pop()

    # Create ingest job
    folder_path = f"uploads/{graphname}"
    ingest_info = client.post(f"/ui/{graphname}/create_ingest", json_body={
        "data_source":        "server",
        "data_source_config": {"data_path": folder_path},
        "loader_config":      {},
        "file_format":        file_format,
    })
    load_job_id    = ingest_info.get("load_job_id") or ingest_info.get("jobId")
    data_source_id = ingest_info.get("data_source_id") or ingest_info.get("dataSourceId")
    file_path      = ingest_info.get("data_path") or folder_path

    if not load_job_id or data_source_id is None:
        raise SetupError(f"create_ingest did not return expected fields: {ingest_info}")

    # Run ingest
    client.post(f"/ui/{graphname}/ingest", json_body={
        "load_job_id":    load_job_id,
        "data_source_id": data_source_id,
        "file_path":      file_path,
    })


def _rebuild(client: _Client, graphname: str) -> None:
    client.post(f"/ui/{graphname}/rebuild_graph")
    print(f"        Polling rebuild status every {_REBUILD_POLL_SECS}s ...", flush=True)
    while True:
        status = client.get(f"/ui/{graphname}/rebuild_status")
        stage  = str(status.get("stage") or status.get("status", "")).lower()
        if stage in ("complete", "completed", "done", "success"):
            return
        if stage in ("failed", "error"):
            raise SetupError(f"Rebuild failed: {status}")
        time.sleep(_REBUILD_POLL_SECS)


# ─── Main setup pipeline ──────────────────────────────────────────────────────

def fresh_setup(
    client: _Client,
    graphname: str,
    doc_files: List[str],
    skip_rebuild: bool = False,
) -> None:
    errors: List[str] = []
    init_ok = True

    print(f"\n  [1/4] Create Graph", flush=True)
    t = time.monotonic()
    try:
        _create_graph(client, graphname)
        print(f"  Done  ({time.monotonic() - t:.1f}s)", flush=True)
    except SetupError as e:
        print(f"  Failed: {e}", flush=True)
        errors.append(f"Create Graph: {e}")
        init_ok = False

    if init_ok:
        print(f"\n  [2/4] Initialize Schema  (1-3 min ...)", flush=True)
        print(f"        Waiting 15s for graph to be ready ...", flush=True)
        time.sleep(15)
        t = time.monotonic()
        try:
            _initialize(client, graphname)
            print(f"  Done  ({time.monotonic() - t:.1f}s)", flush=True)
        except SetupError as e:
            print(f"  Failed: {e}", flush=True)
            errors.append(f"Initialize: {e}")
            init_ok = False

    if init_ok and doc_files:
        print(f"\n  [3/4] Upload & Ingest Documents", flush=True)
        print(f"        Waiting 30s for schema to settle ...", flush=True)
        time.sleep(30)
        t = time.monotonic()
        try:
            _ingest_documents(client, graphname, doc_files)
            print(f"  Done  ({time.monotonic() - t:.1f}s)", flush=True)
        except SetupError as e:
            print(f"  Failed: {e}", flush=True)
            errors.append(f"Ingest: {e}")
    elif init_ok:
        print(f"\n  [3/4] Upload & Ingest - skipped (no data/ files found)", flush=True)

    if not skip_rebuild:
        print(f"\n  [4/4] Rebuild Knowledge Graph (ECC)", flush=True)
        t = time.monotonic()
        try:
            _rebuild(client, graphname)
            print(f"  Done  ({time.monotonic() - t:.1f}s)", flush=True)
        except SetupError as e:
            print(f"  Failed: {e}", flush=True)
            errors.append(f"Rebuild: {e}")
    else:
        print(f"\n  [4/4] Rebuild - skipped", flush=True)

    if errors:
        print(f"\n  Setup completed with errors:")
        for err in errors:
            print(f"    - {err}")
        sys.exit(1)




# ─── CLI entry point ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="GraphRAG Regression — Graph Setup")
    parser.add_argument("--dataset",       required=True,
                        help="Dataset name (folder under graphrag/tests/test_questions/)")
    parser.add_argument("--url",           default=None,
                        help="GraphRAG base URL (overrides server_config.json)")
    parser.add_argument("--graphname",     default=None,
                        help="Override the auto-generated graphname")
    parser.add_argument("--skip-rebuild",  action="store_true",
                        help="Skip ECC rebuild step (useful when testing setup only)")
    args = parser.parse_args()

    config_path = os.environ.get("SERVER_CONFIG", "/code/configs/server_config.json")
    if not os.path.isfile(config_path):
        sys.exit(f"ERROR: SERVER_CONFIG not found: {config_path}")

    with open(config_path) as f:
        server_cfg = json.load(f)

    db       = server_cfg.get("db_config", {})
    username = db.get("username", "")
    password = db.get("password", "")
    if not username or not password:
        sys.exit(f"ERROR: db_config.username / password not set in {config_path}")

    graphrag_url = (
        args.url
        or server_cfg.get("graphrag_config", {}).get("query_url", "http://localhost:8000")
    ).rstrip("/")

    dataset_dir = os.path.join("/code/tests/test_questions", args.dataset)
    if not os.path.isdir(dataset_dir):
        sys.exit(f"ERROR: dataset directory not found: {dataset_dir}")

    # Auto-generate graphname: <dataset_lower>_<8-char uuid>
    uid       = uuid.uuid4().hex[:8]
    graphname = args.graphname or f"{args.dataset.lower()}_{uid}"
    graphname = re.sub(r"[^A-Za-z0-9_]", "_", graphname)
    if graphname[0].isdigit():
        graphname = f"g_{graphname}"

    print(f"\nGraphRAG Regression - Graph Setup")
    print("─" * 60)
    print(f"  Dataset   : {args.dataset}")
    print(f"  Graph     : {graphname}")
    print(f"  URL       : {graphrag_url}")
    print(f"  Mode      : fresh-setup")
    print("─" * 60)

    data_dir = os.path.join(dataset_dir, "data")
    doc_files = []
    if os.path.isdir(data_dir):
        # Prefer pre-built corpus files (*_corpus.txt) when they exist — these
        # are the combined per-dataset documents produced by build_corpus.py.
        # Fall back to all files in data/ if no corpus files are present.
        corpus_files = sorted(
            os.path.join(data_dir, f)
            for f in os.listdir(data_dir)
            if f.endswith("_corpus.txt") and os.path.isfile(os.path.join(data_dir, f))
        )
        if corpus_files:
            doc_files = corpus_files
            print(f"  Documents : {len(doc_files)} corpus file(s) (pre-built)")
        else:
            doc_files = [
                os.path.join(data_dir, f)
                for f in sorted(os.listdir(data_dir))
                if os.path.isfile(os.path.join(data_dir, f))
            ]
            print(f"  Documents : {len(doc_files)} file(s)")
        for fp in doc_files:
            print(f"              - {os.path.basename(fp)}")
    else:
        print(f"  Warning: no data/ directory found - skipping ingest")
    print("─" * 60)

    client = _Client(graphrag_url, username, password)
    fresh_setup(client, graphname, doc_files, skip_rebuild=args.skip_rebuild)

    print(f"\n  Setup complete!  Graph '{graphname}' is ready.")
    print(f"\n  Next - run evaluation:")
    print(f"    ./graphrag/tests/regression/run_eval.sh --dataset {args.dataset} --graphname {graphname}")
    print("─" * 60)

    # Print graphname last so run_setup.sh can capture it cleanly
    print(graphname)
