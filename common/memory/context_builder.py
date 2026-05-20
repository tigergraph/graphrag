# Copyright (c) 2024-2026 TigerGraph, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""
Hybrid conversation memory: recent verbatim turns + rolling summary (TigerGraph).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Any

from langchain_core.messages import HumanMessage
from pyTigerGraph import TigerGraphConnection

from common.config import get_completion_config, get_llm_service, get_memory_config
from common.memory import tg_memory
from common.memory.memory_router import MemoryRoute, decide_memory_route, routing_enabled
from common.memory.tokens import approx_tokens

logger = logging.getLogger(__name__)

_SEM = asyncio.Semaphore(4)
_refresh_locks: dict[str, asyncio.Lock] = {}


def _lock_for(conversation_id: str) -> asyncio.Lock:
    lock = _refresh_locks.get(conversation_id)
    if lock is None:
        lock = asyncio.Lock()
        _refresh_locks[conversation_id] = lock
    return lock


def _truncate_to_token_budget(text: str, max_tokens: int) -> str:
    if max_tokens <= 0 or not text:
        return ""
    if approx_tokens(text) <= max_tokens:
        return text
    lo, hi = 0, len(text)
    best = ""
    while lo <= hi:
        mid = (lo + hi) // 2
        chunk = text[:mid]
        if approx_tokens(chunk) <= max_tokens:
            best = chunk
            lo = mid + 1
        else:
            hi = mid - 1
    return best.rstrip()


def _summarizer_llm_config(graphname: str, memory_cfg: dict[str, Any]) -> dict[str, Any]:
    base = get_completion_config(graphname).copy()
    ov = memory_cfg.get("summary_model")
    if isinstance(ov, dict) and ov:
        for k, v in ov.items():
            if v is not None:
                base[k] = v
    return base


def _compute_recent_window(
    rows_asc: list[dict[str, Any]], recent_budget_tokens: int
) -> tuple[list[dict[str, Any]], int, bool]:
    """
    Walk newest → oldest, pack turns until ``recent_budget_tokens``.
    Returns (recent_rows chronological among selected, tokens_used, dropped_older).
    """
    if not rows_asc:
        return [], 0, False
    n = len(rows_asc)
    picked_idx: list[int] = []
    used = 0
    for i in range(n - 1, -1, -1):
        row = rows_asc[i]
        u = row.get("user_content") or ""
        a = row.get("system_content") or ""
        add = approx_tokens(u) + approx_tokens(a)
        if used + add > recent_budget_tokens:
            if not picked_idx:
                picked_idx.append(i)
                used += add
            break
        picked_idx.append(i)
        used += add
    picked_idx.sort()
    recent = [rows_asc[i] for i in picked_idx]
    dropped = bool(picked_idx and picked_idx[0] > 0)
    return recent, used, dropped


def resolve_memory_route(
    question: str,
    *,
    memory_cfg: dict[str, Any],
    has_messages: bool,
    has_summary: bool,
) -> MemoryRoute:
    """Pick memory tier for this turn (hybrid when routing is disabled)."""
    if not routing_enabled(memory_cfg):
        return MemoryRoute.HYBRID
    return decide_memory_route(
        question,
        has_messages=has_messages,
        has_summary=has_summary,
        memory_cfg=memory_cfg,
    )


def build_agent_context(
    conn: TigerGraphConnection,
    graphname: str,
    conversation_id: str,
    *,
    question: str | None = None,
    route: MemoryRoute | None = None,
) -> list[dict[str, Any]]:
    """
    Build ``question_for_agent`` conversation list for the chosen memory tier.

    Does not touch ``tracelog``. When ``routing_enabled`` and ``question`` are set,
    ``route`` may be omitted and will be computed via ``memory_router``.
    """
    memory_cfg = get_memory_config(graphname)
    recent_budget = int(memory_cfg.get("recent_budget_tokens") or 1200)
    total_cap = int(memory_cfg.get("total_context_cap_tokens") or 4000)
    reserve = int(memory_cfg.get("reserve_for_answer_tokens") or 800)
    summary_max = int(memory_cfg.get("summary_max_tokens") or 800)
    ratio = float(memory_cfg.get("summary_sub_cap_ratio") or 0.4)

    summary_state = tg_memory.get_conversation_summary(conn, graphname, conversation_id)
    rolling = str(summary_state.get("rolling_summary") or "")
    has_summary = bool(rolling.strip())

    rows = tg_memory.list_messages_for_memory(conn, graphname, conversation_id)
    has_messages = bool(rows)

    tier = route
    if tier is None:
        tier = resolve_memory_route(
            question or "",
            memory_cfg=memory_cfg,
            has_messages=has_messages,
            has_summary=has_summary,
        )

    if tier == MemoryRoute.NONE or not has_messages:
        logger.info("[MEMORY] conv=%s route=%s context_turns=0", conversation_id, tier.value)
        return []

    recent_rows, recent_tokens, _dropped = _compute_recent_window(rows, recent_budget)

    include_summary = tier in (MemoryRoute.LTM, MemoryRoute.HYBRID) and has_summary
    include_recent = tier in (MemoryRoute.STM, MemoryRoute.HYBRID)

    if tier == MemoryRoute.LTM and not has_summary:
        include_recent = True
        tier = MemoryRoute.STM

    summary_text = ""
    summary_tokens = 0
    if include_summary:
        if tier == MemoryRoute.LTM:
            summary_budget = summary_max
        else:
            remaining = max(0, total_cap - reserve - (recent_tokens if include_recent else 0))
            summary_budget = min(summary_max, int(remaining * ratio))
        summary_text = _truncate_to_token_budget(rolling, summary_budget)
        summary_tokens = approx_tokens(summary_text) if summary_text else 0

    out: list[dict[str, Any]] = []
    if include_summary and summary_text.strip():
        out.append(
            {
                "query": "<conversation_summary>",
                "response": summary_text.strip(),
                "create_ts": "",
                "update_ts": "",
            }
        )
    if include_recent:
        out.extend(tg_memory.agent_history_from_messages(recent_rows))

    total_in = summary_tokens + (recent_tokens if include_recent else 0)
    logger.info(
        "[MEMORY] conv=%s route=%s recent_turns=%s recent_tokens=%s summary_tokens=%s total_in=%s",
        conversation_id,
        tier.value,
        len(recent_rows) if include_recent else 0,
        recent_tokens if include_recent else 0,
        summary_tokens,
        total_in,
    )
    return out


def prune_message_window(
    conn: TigerGraphConnection,
    graphname: str,
    conversation_id: str,
    *,
    rows: list[dict[str, Any]] | None = None,
) -> int:
    """
    Delete oldest **summarized** message vertices when count exceeds ``message_retention_count``.

    Only removes messages with chronological index ``< summary_turn_count`` that fall outside
    the newest N retained rows. Never deletes uncovered messages.
    """
    memory_cfg = get_memory_config(graphname)
    retain = int(memory_cfg.get("message_retention_count") or 0)
    if retain <= 0:
        return 0

    if rows is None:
        rows = tg_memory.list_messages_for_memory(conn, graphname, conversation_id)
    total = len(rows)
    if total <= retain:
        return 0

    summary_state = tg_memory.get_conversation_summary(conn, graphname, conversation_id)
    try:
        covered = int(summary_state.get("summary_turn_count") or 0)
    except (TypeError, ValueError):
        covered = 0
    if covered <= 0:
        return 0

    to_delete: list[str] = []
    cutoff = total - retain
    for i in range(cutoff):
        if i >= covered:
            break
        mid = rows[i].get("message_id")
        if mid:
            to_delete.append(str(mid))

    if not to_delete:
        return 0

    deleted = tg_memory.delete_message_vertices(conn, graphname, to_delete)
    logger.info(
        "[MEMORY] pruned conv=%s graph=%s deleted=%s retain=%s total_before=%s covered=%s",
        conversation_id,
        graphname,
        deleted,
        retain,
        total,
        covered,
    )
    return deleted


def needs_summary_refresh(
    conn: TigerGraphConnection,
    graphname: str,
    conversation_id: str,
    *,
    rows: list[dict[str, Any]] | None = None,
) -> bool:
    """
    Decide whether to run the background summarizer (and usually create a ``summary`` vertex).

    - **First** compaction: when ``summary_turn_count`` on the conversation is still zero and
      ``len(messages) >= summary_min_turns``. Without this branch, ``summary_refresh_every_turns``
      alone would force the first run to wait until ``total >= every`` (e.g. min_turns=3 but
      every=4 would never fire at 3 turns).
    - **Later** compactions: ``total - covered >= summary_refresh_every_turns``, or the recent
      token window drops older turns so a refresh is needed.

    Pass ``rows`` when the caller already loaded messages to avoid a duplicate TigerGraph query.
    """
    memory_cfg = get_memory_config(graphname)
    min_turns = int(memory_cfg.get("summary_min_turns") or 10)
    every = int(memory_cfg.get("summary_refresh_every_turns") or 4)
    recent_budget = int(memory_cfg.get("recent_budget_tokens") or 1200)

    if rows is None:
        rows = tg_memory.list_messages_for_memory(conn, graphname, conversation_id)
    total = len(rows)
    if total < min_turns:
        return False

    summary_state = tg_memory.get_conversation_summary(conn, graphname, conversation_id)
    try:
        covered = int(summary_state.get("summary_turn_count") or 0)
    except (TypeError, ValueError):
        covered = 0

    if covered == 0 and total >= min_turns:
        return True

    if total - covered >= every:
        return True

    _, _, dropped = _compute_recent_window(rows, recent_budget)
    return dropped


def _build_summarizer_user_prompt(
    prior_summary: str, new_turns: list[dict[str, Any]], max_tokens: int
) -> str:
    turns_s = json.dumps(
        [
            {
                "user": r.get("user_content") or "",
                "assistant": r.get("system_content") or "",
            }
            for r in new_turns
        ],
        ensure_ascii=False,
    )
    return f"""You are a memory compactor for an AI assistant.

You will receive:
- PRIOR_SUMMARY: the existing structured memory of the conversation (may be empty).
- NEW_TURNS: a list of {{user, assistant}} pairs added since PRIOR_SUMMARY was written.

Update the memory so future answers stay accurate and consistent.

RULES:
- Keep the total output strictly under ~{max_tokens} tokens.
- Keep only facts likely to matter in future turns.
- Replace outdated information with the newest version.
- Do not invent facts. If something is uncertain, mark it as such.
- Do not include greetings, filler, or trace/debug information.

OUTPUT FORMAT (markdown sections, in this order):
## User goals
- ...
## Constraints / preferences
- ...
## Decisions made
- ...
## Important facts / entities
- ...
## Open questions
- ...

PRIOR_SUMMARY:
{prior_summary}

NEW_TURNS:
{turns_s}
"""


def _refresh_summary_sync(conn: TigerGraphConnection, graphname: str, conversation_id: str) -> None:
    if not tg_memory.tg_memory_enabled(graphname):
        logger.debug(
            "[MEMORY] summarize skip conv=%s graph=%s: tg_memory_enabled is false",
            conversation_id,
            graphname,
        )
        return
    memory_cfg = get_memory_config(graphname)
    if not memory_cfg.get("enabled") or (memory_cfg.get("mode") or "").lower() != "hybrid":
        logger.debug(
            "[MEMORY] summarize skip conv=%s graph=%s: hybrid memory off or disabled in config",
            conversation_id,
            graphname,
        )
        return

    rows = tg_memory.list_messages_for_memory(conn, graphname, conversation_id)
    if not rows:
        logger.debug(
            "[MEMORY] summarize skip conv=%s graph=%s: no message rows in TG memory",
            conversation_id,
            graphname,
        )
        return

    if not needs_summary_refresh(conn, graphname, conversation_id, rows=rows):
        logger.debug(
            "[MEMORY] summarize skip conv=%s graph=%s rows=%s: cadence or below summary_min_turns",
            conversation_id,
            graphname,
            len(rows),
        )
        return

    summary_state = tg_memory.get_conversation_summary(conn, graphname, conversation_id)
    try:
        covered = int(summary_state.get("summary_turn_count") or 0)
    except (TypeError, ValueError):
        covered = 0
    prior = str(summary_state.get("rolling_summary") or "")

    new_slice = rows[covered:] if covered > 0 else rows[:]
    if not new_slice:
        logger.warning(
            "[MEMORY] summarize skip conv=%s graph=%s: new_slice empty (covered=%s total_rows=%s)",
            conversation_id,
            graphname,
            covered,
            len(rows),
        )
        return

    max_msgs = int(memory_cfg.get("summarizer_max_messages_per_run") or 0)
    if max_msgs > 0 and len(new_slice) > max_msgs:
        new_slice = new_slice[:max_msgs]

    max_out = int(memory_cfg.get("summary_max_tokens") or 800)
    max_covered = int(memory_cfg.get("summary_max_covered_messages_per_vertex") or 50)
    prompt = _build_summarizer_user_prompt(prior, new_slice, max_out)

    try:
        svc_cfg = _summarizer_llm_config(graphname, memory_cfg)
        llm_model = get_llm_service(svc_cfg)
        llm = llm_model.llm
        try:
            bound = llm.bind(max_tokens=max_out)
            raw = bound.invoke([HumanMessage(content=prompt)])
        except TypeError:
            raw = llm.invoke([HumanMessage(content=prompt)])
        text = raw.content if hasattr(raw, "content") else str(raw)
        text = (text or "").strip()
        text = _truncate_to_token_budget(text, max_out)
        if not text:
            logger.warning(
                "[MEMORY] summarize aborted conv=%s graph=%s: summarizer returned empty text",
                conversation_id,
                graphname,
            )
            return

        epoch = int(time.time())
        new_covered = covered + len(new_slice)
        covered_ids = [str(r["message_id"]) for r in new_slice if r.get("message_id")]
        if not covered_ids:
            logger.warning(
                "[MEMORY] summarize conv=%s graph=%s: %s rows in slice but no message_id; "
                "summary vertex will omit covers_message edges",
                conversation_id,
                graphname,
                len(new_slice),
            )

        summary_id = str(uuid.uuid4())
        intent = ""
        tg_memory.save_summary_vertex(
            conn,
            graphname,
            conversation_id,
            summary_id=summary_id,
            text=text,
            intent=intent,
            weight=1.0,
            epoch_added=epoch,
            covered_message_ids=covered_ids,
            max_covered=max_covered,
        )
        tg_memory.update_conversation_summary(
            conn,
            graphname,
            conversation_id,
            new_summary=text,
            new_turn_count=new_covered,
            epoch=epoch,
        )
        logger.info(
            "[MEMORY] wrote summary vertex conv=%s graph=%s summary_id=%s covered_turns=%s batch_msgs=%s",
            conversation_id,
            graphname,
            summary_id,
            new_covered,
            len(covered_ids),
        )
        try:
            prune_message_window(conn, graphname, conversation_id)
        except Exception:
            logger.warning(
                "prune_message_window failed conv=%s graph=%s",
                conversation_id,
                graphname,
                exc_info=True,
            )
    except Exception:
        logger.warning(
            "refresh_summary_sync failed conv=%s graph=%s",
            conversation_id,
            graphname,
            exc_info=True,
        )


async def refresh_summary_async(
    conn: TigerGraphConnection,
    graphname: str,
    conversation_id: str,
) -> None:
    """Background summarization; never raises."""
    async with _SEM:
        lock = _lock_for(conversation_id)
        async with lock:
            await asyncio.to_thread(_refresh_summary_sync, conn, graphname, conversation_id)
