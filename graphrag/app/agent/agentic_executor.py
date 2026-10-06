# Copyright (c) 2024-2026 TigerGraph, Inc.
#
# This program may be redistributed and/or modified under the terms of the GNU
# Affero General Public License as published by the Free Software Foundation,
# either version 3 of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE. See the GNU Affero General Public License for more
# details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.

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
from common.utils.retrieval_stats import retrieved_entries
from tools import tool_registry as registry

logger = logging.getLogger(__name__)

# Per-step trace fields are kept inspectable but bounded so a retrieval
# that returns long chunk text can't bloat the saved trace file.
_TRACE_FIELD_CAP = 12000


def _json_size(obj) -> int:
    # Count characters as written, not as \uXXXX escapes, so non-English
    # text is neither overcounted nor squeezed out of the preview.
    return len(json.dumps(obj, ensure_ascii=False, default=str))


def _cap_strings(obj, max_len: int):
    if isinstance(obj, str):
        if len(obj) <= max_len:
            return obj
        return f"{obj[:max_len]}…(+{len(obj) - max_len:,} chars)"
    if isinstance(obj, dict):
        return {k: _cap_strings(v, max_len) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_cap_strings(v, max_len) for v in obj]
    return obj


def _cap_items(obj, max_items: int):
    if isinstance(obj, dict):
        return {k: _cap_items(v, max_items) for k, v in list(obj.items())[:max_items]}
    if isinstance(obj, list):
        return [_cap_items(v, max_items) for v in obj[:max_items]]
    return obj


def fit_for_trace(obj, limit: int = _TRACE_FIELD_CAP):
    """Return ``(value, shortened)`` for storing ``obj`` in the trace.

    ``value`` is the tool's own data with its shape intact — long strings cut
    first, then long lists and maps — so the Trace Logs view still shows
    real tool output, never a wrapper. ``shortened`` is ``None`` when nothing
    was cut, else ``{"full_chars": N, "shown_chars": M}`` for the trace to
    report alongside the value.
    """
    try:
        plain = json.loads(json.dumps(obj, default=str))
    except Exception:
        plain = str(obj)
    full = _json_size(plain)
    if full <= limit:
        return obj, None
    value = plain
    for max_len in (4000, 2000, 1000, 500, 250, 120, 60):
        value = _cap_strings(plain, max_len)
        if _json_size(value) <= limit:
            break
    else:
        for max_items in (50, 20, 10, 5, 2, 1):
            value = _cap_items(_cap_strings(plain, 60), max_items)
            if _json_size(value) <= limit:
                break
    return value, {"full_chars": full, "shown_chars": _json_size(value)}


def trace_note(cut: dict) -> str:
    """The note recorded next to a value ``fit_for_trace`` shortened."""
    return (
        f"Truncated from {cut['full_chars']:,} to {cut['shown_chars']:,} "
        "characters due to the trace log size limit."
    )


def with_trace_note(value, cut):
    """``value`` with a ``note`` key when it was shortened and is a map."""
    if cut and isinstance(value, dict):
        return {**value, "note": trace_note(cut)}
    return value


def cap_for_trace(obj, limit: int = _TRACE_FIELD_CAP):
    """The trace-sized value of ``obj``, for callers with nowhere to record
    that it was shortened."""
    return fit_for_trace(obj, limit)[0]


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
    return [k for k in retrieved_entries(fr) if k != "Similarity_Context"]


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
    # Input and output keep the tool's own data; a value shortened to fit the
    # trace gets a ``note`` saying so next to it.
    trace_output = {"summary": out.get("summary", "")}
    if out.get("context") is not None:
        trace_output["result"], cut = fit_for_trace(out.get("context"))
        trace_output = with_trace_note(trace_output, cut)
    trace_input = with_trace_note(*fit_for_trace(args))
    traces.append({
        "node": f"{step.id}: {step.tool}",
        "kind": step.kind,
        "tool": step.tool,
        "duration_s": duration,
        "input": trace_input,
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
