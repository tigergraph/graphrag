#!/usr/bin/env python3
# Copyright (c) 2024-2026 TigerGraph, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""
Delete GraphRAG chat memory vertices (message, conversation) on TigerGraph.

Typical use (from host, with graphrag container running):
  docker exec graphrag python /code/scripts/clear_chat_memory.py --all --yes

See also: scripts/clear_chat_stack.ps1 / clear_chat_stack.sh (TigerGraph memory only).
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from pyTigerGraph import TigerGraphConnection


def _load_db_config(config_path: str) -> dict:
    with open(config_path, encoding="utf-8") as f:
        cfg = json.load(f)
    return cfg["db_config"]


def _connect_global(db: dict) -> TigerGraphConnection:
    kw = dict(
        host=db["hostname"],
        username=db["username"],
        password=db["password"],
        graphname="",
        restppPort=db.get("restppPort", "9000"),
        gsPort=db.get("gsPort", "14240"),
    )
    conn = TigerGraphConnection(**kw)
    if db.get("getToken"):
        token = conn.getToken()[0]
        kw["apiToken"] = token
        conn = TigerGraphConnection(**kw)
    return conn


def _connect_graph(db: dict, graphname: str) -> TigerGraphConnection:
    kw = dict(
        host=db["hostname"],
        username=db["username"],
        password=db["password"],
        graphname=graphname,
        restppPort=db.get("restppPort", "9000"),
        gsPort=db.get("gsPort", "14240"),
    )
    conn = TigerGraphConnection(**kw)
    if db.get("getToken"):
        token = conn.getToken()[0]
        kw["apiToken"] = token
        conn = TigerGraphConnection(**kw)
    return conn


def _graphs_with_memory(global_conn: TigerGraphConnection) -> list[str]:
    out: list[str] = []
    for g in global_conn.listGraphs():
        verts = g.get("vertices") or []
        if "message" in verts and "conversation" in verts:
            out.append(g["graphName"])
    return out


def _del_vertices_if_present(conn: TigerGraphConnection, vtype: str, *, dry_run: bool) -> int:
    if vtype not in (conn.getVertexTypes() or []):
        return 0
    try:
        n = conn.getVertexCount(vtype)
    except Exception:
        return 0
    if dry_run or n == 0:
        return int(n)
    conn.delVerticesByType(vtype, permanent=True)
    return int(n)


def clear_graph_memory(conn: TigerGraphConnection, graphname: str, *, dry_run: bool) -> None:
    types = set(conn.getVertexTypes() or [])
    if "message" not in types or "conversation" not in types:
        print(f"  [{graphname}] skip: message/conversation types not on graph", file=sys.stderr)
        return
    try:
        n_msg = conn.getVertexCount("message")
        n_conv = conn.getVertexCount("conversation")
    except Exception as e:
        print(f"  [{graphname}] could not read counts: {e}", file=sys.stderr)
        return
    try:
        n_sum = conn.getVertexCount("summary") if "summary" in types else 0
    except Exception:
        n_sum = 0
    print(f"  [{graphname}] message={n_msg} conversation={n_conv} summary={n_sum}")
    if dry_run:
        return
    # Order: summary (edges to message/conversation), then message, then conversation.
    _del_vertices_if_present(conn, "summary", dry_run=False)
    if n_msg:
        conn.delVerticesByType("message", permanent=True)
    if n_conv:
        conn.delVerticesByType("conversation", permanent=True)
    print(
        f"  [{graphname}] deleted summary (if any) + message + conversation vertices (permanent=True)"
    )


def main() -> int:
    p = argparse.ArgumentParser(description="Clear TG chat memory (conversation/message vertices)")
    p.add_argument(
        "-g",
        "--graph",
        action="append",
        dest="graphs",
        help="Graph name (repeat for multiple). Default: none; use --all",
    )
    p.add_argument("--all", action="store_true", help="All graphs that have chat memory vertex types")
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Only print vertex counts, do not delete",
    )
    p.add_argument(
        "--yes",
        action="store_true",
        help="Required to actually delete (safety)",
    )
    p.add_argument(
        "--config",
        default=os.environ.get("SERVER_CONFIG", "/code/configs/server_config.json"),
        help="Path to server_config.json (default: SERVER_CONFIG env or /code/configs/...)",
    )
    args = p.parse_args()

    if not args.dry_run and not args.yes:
        print("Refusing to delete without --yes (or use --dry-run).", file=sys.stderr)
        return 2

    if not os.path.isfile(args.config):
        print(f"Config not found: {args.config}", file=sys.stderr)
        return 1

    db = _load_db_config(args.config)
    gconn = _connect_global(db)

    if args.all:
        targets = _graphs_with_memory(gconn)
    elif args.graphs:
        targets = args.graphs
    else:
        print("Specify --all or one or more --graph NAME", file=sys.stderr)
        return 2

    if not targets:
        print("No graphs to process.")
        return 0

    print(f"Processing {len(targets)} graph(s): {', '.join(targets)}")
    for gn in targets:
        c = _connect_graph(db, gn)
        clear_graph_memory(c, gn, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
