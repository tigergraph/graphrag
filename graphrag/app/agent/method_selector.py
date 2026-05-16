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

"""Auto-selection of GraphRAG retrieval method.

Two stages:
- Stage A: deterministic rules over the question.
- Stage B: LLM fallback when rules are inconclusive.

Phase 1 returns a single method. Top-K cascade, subset-constraint validation,
and the diagnostician for retry routing land in later phases.
"""

import re
import logging
from typing import Literal, Optional

from langchain.prompts import PromptTemplate
from langchain_core.output_parsers import PydanticOutputParser
from pydantic import BaseModel, Field
from pyTigerGraph.pyTigerGraph import TigerGraphConnection

from common.logs.log import req_id_cv
from common.logs.logwriter import LogWriter

logger = logging.getLogger(__name__)


# Canonical method strings — match the dispatcher in agent_graph.supportai_search.
METHOD_SIMILARITY = "similaritysearch"
METHOD_CONTEXTUAL = "contextualsearch"
METHOD_HYBRID = "hybridsearch"
METHOD_COMMUNITY = "communitysearch"
ALL_METHODS = (METHOD_SIMILARITY, METHOD_CONTEXTUAL, METHOD_HYBRID, METHOD_COMMUNITY)

# Methods that retrieve raw chunks and respect a `top_k` cap on the chunk count.
# Used by `has_insufficient_context` and the in-lane fallback trigger; community
# is excluded because its top_k counts community summaries, not chunks.
CHUNK_BASED_METHODS = frozenset({METHOD_SIMILARITY, METHOD_CONTEXTUAL, METHOD_HYBRID})


# Default fallback when the LLM stage can't produce a usable answer. Hybrid is the
# pre-existing system default and the safest superset retriever.
FALLBACK_METHOD = METHOD_HYBRID


# In-lane fallback table: when a chunk-based method returns insufficient context,
# try this method instead. Subset-aware — never falls back to a method whose
# results are a strict subset of the failing method's seeds (e.g., similarity is
# a subset of contextual/hybrid, so we don't fall back to it from those).
#
# The table fires once per question. Community is the terminal step from hybrid
# because its retrieval surface (community summaries) is fundamentally different
# from chunk retrieval — when chunk-based search finds little, thematic
# summaries may still cover the question.
INLANE_FALLBACK_TABLE = {
    METHOD_SIMILARITY: METHOD_HYBRID,    # point lookup → graph-hop expansion
    METHOD_CONTEXTUAL: METHOD_HYBRID,    # sibling expansion thin → try graph hops
    METHOD_HYBRID: METHOD_COMMUNITY,     # entity-driven thin → try thematic summaries
    # No fallback FROM community — its top-k semantics differ; the in-lane
    # trigger doesn't apply, and falling back to a chunk method when community
    # missed is a different problem (handled by router_fallback / out-of-corpus).
}


def has_insufficient_context(retrieval_dict, method: str, top_k: int) -> bool:
    """Decide whether a chunk-based retriever returned fewer items than asked.

    Args:
        retrieval_dict: the `final_retrieval` dict from the retriever output, or None.
        method: canonical method string (one of ALL_METHODS).
        top_k: the requested number of chunks for this retrieval.

    Returns:
        True if the result is "insufficient" — i.e., the method is chunk-based and
        the retrieved count is strictly below `top_k`. Empty results count as
        insufficient. Returns False for community search (different semantics) and
        for any non-dict input.

    Note: this is the trigger for the in-lane fallback in supportai_search.
    Community search is excluded because its top_k caps community summaries, not
    chunks, and a small number of returned summaries doesn't mean "no context."
    """
    if method not in CHUNK_BASED_METHODS:
        return False
    if not isinstance(retrieval_dict, dict):
        return True  # empty / malformed → insufficient
    return len(retrieval_dict) < top_k


class RetrieverChoice(BaseModel):
    """Public selector result. `source` records how the choice was made — useful
    for telemetry and for the upcoming top-K / diagnostician phases."""

    method: str  # one of ALL_METHODS
    reason: str  # short human-readable justification
    source: str  # "rules" | "llm" | "fallback"


class _LLMRetrieverChoice(BaseModel):
    """Schema returned by the LLM. Uses friendly labels (no `search` suffix); we
    normalise them to canonical method strings before returning."""

    method: Literal["similarity", "contextual", "hybrid", "community"]
    reason: str = Field(default="", description="<= 20 words explaining the pick")


_LLM_LABEL_TO_METHOD = {
    "similarity": METHOD_SIMILARITY,
    "contextual": METHOD_CONTEXTUAL,
    "hybrid": METHOD_HYBRID,
    "community": METHOD_COMMUNITY,
}


# ---------- Stage A: deterministic rules ----------
#
# Order matters: the first pattern family that fires wins. We check community
# first (clearest semantic signal — global/thematic language), then contextual
# (process/narrative), then hybrid (relational), then similarity (short factoid).
# That ordering reflects increasing ambiguity — community language is hardest to
# confuse with the others, similarity is easiest.

_COMMUNITY_PATTERNS = (
    re.compile(r"\b(summari[sz]e|summary)\b", re.IGNORECASE),
    re.compile(r"\b(main|key|central|important)\s+(themes?|topics?|ideas?|points?)\b", re.IGNORECASE),
    re.compile(r"\bwhat\s+(is|are)\s+(this|the|these)\s+(corpus|dataset|documents?)\s+about\b", re.IGNORECASE),
    re.compile(r"\bacross\s+(the|all)\s+documents?\b", re.IGNORECASE),
    re.compile(r"\boverview\s+of\b", re.IGNORECASE),
    re.compile(r"\b(what|which)\s+(topics?|themes?)\b", re.IGNORECASE),
)

_CONTEXTUAL_PATTERNS = (
    re.compile(r"\bwalk\s+me\s+through\b", re.IGNORECASE),
    re.compile(r"\bstep[- ]by[- ]step\b", re.IGNORECASE),
    re.compile(r"\bwhat\s+happens\s+(after|before|next|when)\b", re.IGNORECASE),
    re.compile(r"\bexplain\s+the\s+process\b", re.IGNORECASE),
    re.compile(r"\bhow\s+does\s+(it|this|that)\s+work\b", re.IGNORECASE),
)

_HYBRID_PATTERNS = (
    re.compile(r"\bhow\s+(is|are|does)\s+.+?\s+(related|connect|relate)\b", re.IGNORECASE),
    re.compile(r"\b(relationship|connection)\s+between\b", re.IGNORECASE),
    re.compile(r"\b(work\s+with|report\s+to|depend\s+on|interact\s+with)\b", re.IGNORECASE),
)

_SIMILARITY_PATTERNS = (
    re.compile(r"^\s*(what|who)\s+(is|are|was|were)\b", re.IGNORECASE),
    re.compile(r"^\s*define\b", re.IGNORECASE),
    re.compile(r"^\s*when\s+(did|was|were)\b", re.IGNORECASE),
    re.compile(r"^\s*where\s+(is|are|was|were)\b", re.IGNORECASE),
)

_SIMILARITY_MAX_TOKENS = 12


def rules_choose(question: str) -> Optional[RetrieverChoice]:
    """Stage A: deterministic rules. Returns None if no rule fires with confidence."""
    if not question or not question.strip():
        return None
    q = question.strip()

    for p in _COMMUNITY_PATTERNS:
        if p.search(q):
            return RetrieverChoice(
                method=METHOD_COMMUNITY,
                reason=f"global/thematic phrasing matched /{p.pattern}/",
                source="rules",
            )

    for p in _CONTEXTUAL_PATTERNS:
        if p.search(q):
            return RetrieverChoice(
                method=METHOD_CONTEXTUAL,
                reason=f"process/narrative phrasing matched /{p.pattern}/",
                source="rules",
            )

    for p in _HYBRID_PATTERNS:
        if p.search(q):
            return RetrieverChoice(
                method=METHOD_HYBRID,
                reason=f"relational phrasing matched /{p.pattern}/",
                source="rules",
            )

    token_count = len(q.split())
    if token_count <= _SIMILARITY_MAX_TOKENS:
        for p in _SIMILARITY_PATTERNS:
            if p.match(q):
                return RetrieverChoice(
                    method=METHOD_SIMILARITY,
                    reason=f"short factoid (<= {_SIMILARITY_MAX_TOKENS} tokens) matched /{p.pattern}/",
                    source="rules",
                )

    return None


# ---------- Stage B: LLM fallback ----------


class RetrieverSelector:
    """Picks the best retrieval method for a question.

    Construction mirrors `TigerGraphAgentRouter` so it slots into the existing
    LLM-call plumbing (PydanticOutputParser + invoke_with_parser).
    """

    def __init__(self, llm_model, db_conn: TigerGraphConnection):
        self.llm = llm_model
        self.db_conn = db_conn

    def choose(
        self,
        question: str,
        conversation: Optional[list[dict[str, str]]] = None,
    ) -> RetrieverChoice:
        """Return the best retrieval method for `question`.

        Tries Stage A rules first; on miss, calls the LLM (Stage B). Always
        returns a `RetrieverChoice` — on any unrecoverable error, falls back to
        `FALLBACK_METHOD` rather than raising.
        """
        LogWriter.info(
            f"request_id={req_id_cv.get()} ENTRY RetrieverSelector.choose: {question!r}"
        )

        # Stage A — pure-Python, no external calls
        rule_choice = rules_choose(question)
        if rule_choice is not None:
            LogWriter.info(
                f"request_id={req_id_cv.get()} EXIT RetrieverSelector.choose "
                f"(rules) method={rule_choice.method} reason={rule_choice.reason!r}"
            )
            return rule_choice

        # Stage B — LLM. Schema types are passed in to anchor the prompt.
        try:
            v_types = self.db_conn.getVertexTypes()
            e_types = self.db_conn.getEdgeTypes()
        except Exception as e:  # noqa: BLE001 - schema lookup is best-effort
            logger.warning(
                f"request_id={req_id_cv.get()} schema lookup failed in selector: {e}"
            )
            v_types, e_types = [], []

        try:
            parser = PydanticOutputParser[_LLMRetrieverChoice](
                pydantic_object=_LLMRetrieverChoice
            )
            prompt = PromptTemplate(
                template=self.llm.select_retriever_prompt,
                input_variables=["question", "v_types", "e_types", "conversation"],
                partial_variables={
                    "format_instructions": parser.get_format_instructions()
                },
            )
            res: _LLMRetrieverChoice = self.llm.invoke_with_parser(
                prompt,
                parser,
                {
                    "question": question,
                    "v_types": v_types,
                    "e_types": e_types,
                    "conversation": conversation or [],
                },
                caller_name="select_retriever",
            )
            method = _LLM_LABEL_TO_METHOD.get(res.method.lower())
            if method is None:
                raise ValueError(f"LLM returned unknown method label: {res.method!r}")
            choice = RetrieverChoice(method=method, reason=res.reason or "", source="llm")
        except Exception as e:  # noqa: BLE001 - selector must always return something
            logger.warning(
                f"request_id={req_id_cv.get()} RetrieverSelector LLM stage failed: {e}; "
                f"falling back to {FALLBACK_METHOD}"
            )
            choice = RetrieverChoice(
                method=FALLBACK_METHOD,
                reason=f"selector fallback ({type(e).__name__})",
                source="fallback",
            )

        LogWriter.info(
            f"request_id={req_id_cv.get()} EXIT RetrieverSelector.choose "
            f"({choice.source}) method={choice.method} reason={choice.reason!r}"
        )
        return choice
