# Copyright (c) 2024-2026 TigerGraph, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""
Rule-based memory tier selection (no extra LLM call).

Pipeline: structural guards → phrase rules → fallback (hybrid by default).
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Any


class MemoryRoute(str, Enum):
    """Which conversation layers to inject into the agent context."""

    STM = "stm"
    LTM = "ltm"
    HYBRID = "hybrid"
    NONE = "none"


# Follow-up / recent-turn cues → short-term memory.
_STM_PHRASES: tuple[str, ...] = (
    " that ",
    " this ",
    " it ",
    " your last",
    " you just",
    " above",
    " shorter",
    " longer",
    " try again",
    " continue",
    " rewrite",
    " explain that",
    " make it shorter",
    " make it longer",
)

# Early-session / summarized-fact cues → long-term memory (requires non-empty summary).
_LTM_PHRASES: tuple[str, ...] = (
    "at the start",
    " earlier",
    " beginning",
    " originally",
    " my preference",
    " my preferences",
    " my budget",
    " what did i tell you",
    " what did i say",
    " constraints i",
    " constraint i",
    " we decided",
    " i mentioned before",
    " from the beginning",
)

# Graph / schema / DB statistics — chat history usually not needed.
_NONE_PHRASES: tuple[str, ...] = (
    "how many vertices",
    "how many edges",
    "how many nodes",
    "vertex types",
    "edge types",
    "list vertex",
    "list edge",
    "graph schema",
    "database schema",
    "schema of the graph",
    "count vertices",
    "count edges",
    "number of vertices",
    "number of edges",
)


def _normalize_question(question: str) -> str:
    q = (question or "").strip().lower()
    if not q:
        return ""
    return f" {q} " if not q.startswith(" ") else q


def _contains_any(haystack: str, phrases: tuple[str, ...]) -> bool:
    return any(p in haystack for p in phrases)


def _looks_like_follow_up(q: str) -> bool:
    if _contains_any(q, _STM_PHRASES):
        return True
    stripped = q.strip()
    if len(stripped) < 80 and re.search(
        r"\b(that|it|this|again|continue|shorter|longer|rewrite)\b", stripped
    ):
        return True
    return False


def decide_memory_route(
    question: str,
    *,
    has_messages: bool,
    has_summary: bool,
    memory_cfg: dict[str, Any] | None = None,
) -> MemoryRoute:
    """
    Choose stm | ltm | hybrid | none for the current user turn.

    Parameters
    ----------
    question:
        Current user message text.
    has_messages:
        Whether the conversation has at least one stored exchange.
    has_summary:
        Whether ``rolling_summary`` on the conversation vertex is non-empty.
    memory_cfg:
        Merged memory config; uses ``routing_fallback`` when rules are ambiguous.
    """
    cfg = memory_cfg or {}
    fallback_raw = str(cfg.get("routing_fallback") or "hybrid").lower()
    try:
        fallback = MemoryRoute(fallback_raw)
    except ValueError:
        fallback = MemoryRoute.HYBRID

    q = _normalize_question(question)

    if not has_messages:
        return MemoryRoute.NONE

    if not q:
        return MemoryRoute.HYBRID if has_summary else MemoryRoute.STM

    if _contains_any(q, _NONE_PHRASES):
        return MemoryRoute.NONE

    stm_hit = _looks_like_follow_up(q)
    ltm_hit = has_summary and _contains_any(q, _LTM_PHRASES)

    if stm_hit and ltm_hit:
        return MemoryRoute.HYBRID
    if stm_hit:
        return MemoryRoute.STM
    if ltm_hit:
        return MemoryRoute.LTM

    if not has_summary:
        return MemoryRoute.STM

    return fallback


def routing_enabled(memory_cfg: dict[str, Any] | None) -> bool:
    """True when per-question memory tier selection is active."""
    if not memory_cfg:
        return False
    return bool(memory_cfg.get("routing_enabled", False))
