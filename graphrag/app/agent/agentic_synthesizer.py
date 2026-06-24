# Copyright (c) 2024-2026 TigerGraph, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Synthesizer node for the agentic engine.

Merges the contexts gathered by all executed steps into a single context
block and produces the final grounded answer by reusing the existing
``TigerGraphAgentGenerator`` — so answer quality, citation handling, and
the out-of-corpus honesty match classic mode.
"""

import logging

from agent.agent_generation import TigerGraphAgentGenerator
from common.py_schemas import GraphRAGResponse

logger = logging.getLogger(__name__)


def _gather(results: dict) -> dict:
    """Collect non-empty step contexts into a combined context block."""
    structural, unstructured = [], []
    for sr in results.values():
        if not sr.ok or sr.context is None:
            continue
        ctx = sr.context
        fc = ctx.get("function_call") if isinstance(ctx, dict) else None
        if fc and "Vector_Search" in str(fc):
            unstructured.append(ctx)
        else:
            structural.append(ctx)
    return {"structural": structural, "unstructured": unstructured}


def has_context(results: dict) -> bool:
    g = _gather(results)
    return bool(g["structural"] or g["unstructured"])


def synthesize(llm, question, results: dict, plan=None, conversation=None) -> GraphRAGResponse:
    """Produce the final answer from gathered step contexts."""
    combined = _gather(results)
    generator = TigerGraphAgentGenerator(llm)
    answer = generator.generate_answer(question, combined)

    nl = getattr(answer, "generated_answer", None) or str(answer)
    citations = getattr(answer, "citation", []) or []
    answered = bool(combined["structural"] or combined["unstructured"])

    query_sources = {
        "plan": plan.model_dump() if plan is not None else None,
        "steps": [
            {"step_id": sr.step_id, "ok": sr.ok, "summary": sr.summary}
            for sr in results.values()
        ],
        "result": combined,
        "citations": citations,
        "reasoning": plan.strategy if plan is not None else "",
    }
    return GraphRAGResponse(
        natural_language_response=nl,
        answered_question=answered,
        response_type="agentic",
        query_sources=query_sources,
    )
