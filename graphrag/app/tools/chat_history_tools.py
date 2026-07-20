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

"""Agent tools over the current user's own conversation history.

Operate on ctx.chat_repo, a ConversationRepository already bound to the
authenticated principal and current graph. Neither tool takes a user or
graph argument, so there's no way for the model to point them elsewhere.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

_MAX_CONVERSATIONS = 50
_MAX_SEARCH_HITS = 20
_MAX_MESSAGES = 200
_MAX_CONTENT_CHARS = 4000


def _epoch_to_iso(epoch: Any) -> str:
    try:
        epoch = int(epoch or 0)
    except (TypeError, ValueError):
        epoch = 0
    if epoch <= 0:
        return ""
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


def _ok(summary: str, context: Any) -> dict:
    return {"ok": True, "summary": summary, "context": context, "citations": []}


def _title(conversation: dict, max_chars: int = 80) -> str:
    """A human-readable label: the stored name, else a preview of the first message."""
    name = (conversation.get("name") or "").strip()
    if name:
        return name
    preview = " ".join(str(conversation.get("first_message") or "").split())
    if not preview:
        return "(untitled)"
    return preview if len(preview) <= max_chars else preview[: max_chars - 1] + "…"


def _unavailable() -> dict:
    return {
        "ok": False,
        "summary": (
            "conversation history is not available in this context; answer "
            "from the visible conversation turns only"
        ),
        "context": None,
        "citations": [],
    }


def list_my_conversations(ctx, limit: int | None = None) -> dict:
    repo = getattr(ctx, "chat_repo", None)
    if repo is None:
        return _unavailable()
    ctx.emit("Listing your conversations")
    limit = max(1, min(int(limit or _MAX_CONVERSATIONS), _MAX_CONVERSATIONS))
    conversations = repo.list_conversations()[:limit]
    rows = [
        {
            "conversation_id": c.get("conversation_id", ""),
            "title": _title(c),
            "created": _epoch_to_iso(c.get("create_epoch")),
            "last_updated": _epoch_to_iso(c.get("update_epoch")),
        }
        for c in conversations
    ]
    return _ok(
        f"{len(rows)} conversation(s) belonging to the current user on this graph",
        {"conversations": rows},
    )


def get_my_conversation(ctx, conversation_id: str) -> dict:
    repo = getattr(ctx, "chat_repo", None)
    if repo is None:
        return _unavailable()
    ctx.emit("Reading that conversation's messages")
    conversation = repo.get_conversation(str(conversation_id or ""))
    if conversation is None:
        return _ok(
            "no conversation with that id exists in the current user's own "
            "history on this graph",
            {"conversation": None, "messages": []},
        )
    messages = (conversation.get("messages") or [])[:_MAX_MESSAGES]
    rows = [
        {
            "message_id": m.get("message_id", ""),
            "role": m.get("role", ""),
            "content": str(m.get("content", ""))[:_MAX_CONTENT_CHARS],
            "created": _epoch_to_iso(m.get("create_epoch")),
        }
        for m in messages
    ]
    return _ok(
        f"{len(rows)} message(s) in the current user's conversation "
        f"{conversation.get('conversation_id', '')!r}",
        {
            "conversation": {
                "conversation_id": conversation.get("conversation_id", ""),
                "name": conversation.get("name", ""),
                "created": _epoch_to_iso(conversation.get("create_epoch")),
                "last_updated": _epoch_to_iso(conversation.get("update_epoch")),
            },
            "messages": rows,
        },
    )


def search_my_messages(ctx, q: str, limit: int | None = None) -> dict:
    repo = getattr(ctx, "chat_repo", None)
    if repo is None:
        return _unavailable()
    ctx.emit("Searching your past messages")
    limit = max(1, min(int(limit or 10), _MAX_SEARCH_HITS))
    hits = repo.search_messages(q or "", limit=limit)
    rows = [
        {
            "conversation_id": h.get("conversation_id", ""),
            "conversation_name": h.get("conversation_name", ""),
            "message_id": h.get("message_id", ""),
            "content": h.get("content", ""),
            "role": h.get("role", ""),
            "created": _epoch_to_iso(h.get("create_epoch")),
        }
        for h in hits
    ]
    return _ok(
        f"{len(rows)} matching message(s) in the current user's own history",
        {"messages": rows},
    )
