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

"""Synthesizer node for the agentic engine.

Merges the contexts gathered by all executed steps into a single context
block and produces the final grounded answer by reusing the existing
``TigerGraphAgentGenerator`` — so answer quality, citation handling, and
the out-of-corpus honesty match classic mode.
"""

import logging

from agent.agent_generation import TigerGraphAgentGenerator
from agent.agentic_executor import retrieved_chunk_ids
from common.py_schemas import GraphRAGResponse
from common.utils.retrieval_stats import is_grouped, retrieved_entries

logger = logging.getLogger(__name__)


def _without_seen(ctx, seen: set):
    """``ctx`` minus the retrieved entries already in ``seen`` (which it then
    extends), or ``None`` when nothing new is left. ``ctx`` is not modified."""
    result = ctx.get("result") if isinstance(ctx, dict) else None
    fr = result.get("final_retrieval") if isinstance(result, dict) else None
    if not isinstance(fr, dict):
        return ctx
    kept = {}
    for key, value in fr.items():
        if is_grouped(value):
            fresh = {k: v for k, v in value.items() if k not in seen}
            seen.update(fresh)
            if fresh:
                kept[key] = fresh
        elif key == "Similarity_Context" or key not in seen:
            seen.add(key)
            kept[key] = value
    if not kept:
        return None
    return {**ctx, "result": {**result, "final_retrieval": kept}}


def _gather(results: dict, log: bool = False) -> dict:
    """Collect non-empty step contexts into a combined context block.

    Document passages already returned by an earlier step are left out of
    later ones, so the answer model reads (and is billed for) each passage
    once; a step left with nothing new is dropped.
    """
    structural, unstructured = [], []
    seen: set = set()
    skipped = 0
    for sr in results.values():
        if not sr.ok or sr.context is None:
            continue
        ctx = sr.context
        fc = ctx.get("function_call") if isinstance(ctx, dict) else None
        if fc and "Vector_Search" in str(fc):
            before = len(retrieved_entries(_final_retrieval(ctx)))
            ctx = _without_seen(ctx, seen)
            after = len(retrieved_entries(_final_retrieval(ctx))) if ctx else 0
            skipped += before - after
            if ctx is not None:
                unstructured.append(ctx)
        else:
            structural.append(ctx)
    if skipped and log:
        logger.info(f"synthesize: left out {skipped} passage(s) already retrieved by an earlier step")
    return {"structural": structural, "unstructured": unstructured}


def _final_retrieval(ctx) -> dict:
    result = ctx.get("result") if isinstance(ctx, dict) else None
    fr = result.get("final_retrieval") if isinstance(result, dict) else None
    return fr if isinstance(fr, dict) else {}


def has_context(results: dict) -> bool:
    g = _gather(results)
    return bool(g["structural"] or g["unstructured"])


def synthesize(llm, question, results: dict, plan=None, conversation=None) -> GraphRAGResponse:
    """Produce the final answer from gathered step contexts."""
    combined = _gather(results, log=True)
    generator = TigerGraphAgentGenerator(llm)
    answer = generator.generate_answer(question, combined)

    nl = getattr(answer, "generated_answer", None) or str(answer)
    citations = getattr(answer, "citation", []) or []
    answered = bool(combined["structural"] or combined["unstructured"])

    # Chunk ids the plan FETCHED (across all unstructured steps), de-duped in
    # order — the agent's record of what was retrieved, distinct from the
    # SELECTED citations the answer cites.
    retrieved_citations, _seen = [], set()
    for ctx in combined["unstructured"]:
        for cid in retrieved_chunk_ids(ctx):
            if cid not in _seen:
                _seen.add(cid)
                retrieved_citations.append(cid)

    query_sources = {
        "plan": plan.model_dump() if plan is not None else None,
        "steps": [
            {"step_id": sr.step_id, "ok": sr.ok, "summary": sr.summary}
            for sr in results.values()
        ],
        "result": combined,
        "citations": citations,
        "retrieved_citations": retrieved_citations,
        "reasoning": plan.strategy if plan is not None else "",
    }
    return GraphRAGResponse(
        natural_language_response=nl,
        answered_question=answered,
        response_type="agentic",
        query_sources=query_sources,
    )
