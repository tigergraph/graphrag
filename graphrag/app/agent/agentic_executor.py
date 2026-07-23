# Copyright (c) 2024-2026 TigerGraph, Inc.
#
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0).
# See the LICENSE file or <https://www.gnu.org/licenses/agpl-3.0.html>
# for the full license text.

"""Executor node for the agentic engine.

Runs a plan's steps in dependency order, resolving ``arg_bindings`` from
earlier results into each step's tool args, and dispatching through the
tool registry (which validates args and never raises). Retrieval steps run
sequentially in this first cut — the retrievers do blocking TG I/O, so
true parallelism would need a thread pool; that's a later optimization
noted in the plan. Returns a ``{step_id: StepResult}`` map.
"""

import json
import logging
import time

from common.llm_services.base_llm import get_collected_usage
from common.py_schemas import StepResult
from tools import tool_registry as registry

logger = logging.getLogger(__name__)

# Per-step trace fields are kept inspectable but bounded so a retrieval
# that returns long chunk text can't bloat the saved trace file.
_TRACE_FIELD_CAP = 12000


def cap_for_trace(obj, limit: int = _TRACE_FIELD_CAP):
    """Return ``obj`` unchanged if its JSON is under ``limit`` chars; else a
    truncated-preview marker (kept JSON-valid for the Trace Logs UI)."""
    try:
        s = json.dumps(obj, default=str)
    except Exception:
        s = str(obj)
    if len(s) <= limit:
        return obj
    return {"_truncated": True, "chars": len(s), "preview": s[:limit]}


def retrieved_chunk_ids(context) -> list:
    """Chunk ids the agent FETCHED from a retrieval tool/step context.

    The retrieval tools return their chunks (keyed by id, with text) under
    ``context['result']['final_retrieval']``. Recording *what was fetched* is
    the agent's job, not the tool's — so the agent harvests those keys here for
    the trace. Synthetic non-chunk keys (e.g. community ``Similarity_Context``)
    are dropped.
    """
    if not isinstance(context, dict):
        return []
    inner = context.get("result")
    fr = inner.get("final_retrieval") if isinstance(inner, dict) else None
    if not isinstance(fr, dict):
        return []
    return [k for k in fr.keys() if k != "Similarity_Context"]


def _usage_since(start_idx: int) -> dict:
    """Aggregate LLM usage recorded since ``start_idx`` in the collector."""
    bucket = get_collected_usage() or []
    delta = bucket[start_idx:]
    return {
        "input_tokens": sum(int(u.get("input_tokens", 0) or 0) for u in delta),
        "output_tokens": sum(int(u.get("output_tokens", 0) or 0) for u in delta),
        "total_tokens": sum(int(u.get("total_tokens", 0) or 0) for u in delta),
        "cost": sum(float(u.get("cost", 0) or 0) for u in delta),
        "calls": [
            {
                "caller_name": u.get("caller_name"),
                "input_tokens": u.get("input_tokens", 0),
                "output_tokens": u.get("output_tokens", 0),
                "total_tokens": u.get("total_tokens", 0),
                "cost": u.get("cost", 0),
            }
            for u in delta
        ],
    }


def _resolve_path(results: dict, ref: str):
    """Resolve ``"<step_id>.<dotted.path>"`` against prior StepResults.

    ``S1.context.result`` -> results["S1"].context["result"]. Returns None
    if any hop is missing.
    """
    parts = ref.split(".")
    step_id, path = parts[0], parts[1:]
    sr = results.get(step_id)
    if sr is None:
        return None
    cur = sr.context
    for p in path:
        if p == "context":
            continue
        if isinstance(cur, dict):
            cur = cur.get(p)
        else:
            cur = getattr(cur, p, None)
        if cur is None:
            return None
    return cur


def _ready(step, done: set) -> bool:
    return all(dep in done for dep in (step.depends_on or []))


def _run_step(step, args, ctx, results, traces):
    """Run one step, recording its result + a per-step trace (duration, usage)."""
    ctx.emit(f"{step.rationale or step.tool}")
    usage_start = len(get_collected_usage() or [])
    t0 = time.time()
    out = registry.run(step.tool, args, ctx)
    duration = round(time.time() - t0, 3)
    results[step.id] = StepResult(
        step_id=step.id,
        ok=bool(out.get("ok")),
        summary=out.get("summary", ""),
        context=out.get("context"),
        citations=out.get("citations") or [],
    )
    # Trace output carries the one-line summary AND the actual result, so
    # the Trace Logs detail view shows what each step returned (not just a
    # status line). Input is the resolved tool args.
    trace_output = {"summary": out.get("summary", "")}
    if out.get("context") is not None:
        trace_output["result"] = cap_for_trace(out.get("context"))
    traces.append({
        "node": f"{step.id}: {step.tool}",
        "kind": step.kind,
        "tool": step.tool,
        "duration_s": duration,
        "input": cap_for_trace(args),
        "output": trace_output,
        "rationale": step.rationale or "",
        "usage": _usage_since(usage_start),
    })


def execute_plan(plan, ctx):
    """Execute ``plan`` against the tool context.

    Returns ``(results, traces)`` where ``results`` is ``{step_id:
    StepResult}`` and ``traces`` is a per-step list (node, duration_s,
    output, usage) for the Trace Logs UI.
    """
    results: dict = {}
    traces: list = []
    done: set = set()
    remaining = [s for s in plan.steps if s.kind != "answer" and s.tool]

    # Dependency-ordered passes. Independent steps simply run in listed
    # order within a pass; dependents wait for their inputs.
    guard = 0
    while remaining and guard < 100:
        guard += 1
        progressed = False
        for step in list(remaining):
            if not _ready(step, done):
                continue
            args = dict(step.args or {})
            for arg_name, ref in (step.arg_bindings or {}).items():
                val = _resolve_path(results, ref)
                if val is not None:
                    args[arg_name] = val
            _run_step(step, args, ctx, results, traces)
            done.add(step.id)
            remaining.remove(step)
            progressed = True
        if not progressed:
            # Unsatisfiable dependencies (cycle / missing dep) — run the
            # rest unbound so nothing is silently skipped.
            for step in remaining:
                _run_step(step, dict(step.args or {}), ctx, results, traces)
            break
    return results, traces
