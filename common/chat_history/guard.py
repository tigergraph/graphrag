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


from __future__ import annotations

import logging
import re
from typing import Iterable

from common.db.schema_utils import (
    CHAT_HISTORY_EDGE_TYPES,
    CHAT_HISTORY_VERTEX_TYPES,
)

logger = logging.getLogger(__name__)

CHAT_HISTORY_QUERY_PREFIX = "Chat_"
_CHAT_TYPES = CHAT_HISTORY_VERTEX_TYPES | CHAT_HISTORY_EDGE_TYPES
_CHAT_TYPE_RE = re.compile(
    r"\b(" + "|".join(sorted(re.escape(t) for t in _CHAT_TYPES)) + r")\b",
    re.IGNORECASE,
)


class ChatHistoryAccessDenied(Exception):
    """A database call named a conversation type or query."""


def is_chat_history_type(name: str) -> bool:
    if not isinstance(name, str):
        return False
    lowered = name.strip().lower()
    return any(lowered == t.lower() for t in _CHAT_TYPES)


def is_chat_history_query(name: str) -> bool:
    return isinstance(name, str) and name.strip().startswith(CHAT_HISTORY_QUERY_PREFIX)


def filter_types(names: Iterable[str]) -> list:
    """Drop conversation types from a schema listing."""
    if not names:
        return []
    return [n for n in names if not is_chat_history_type(n)]


def filter_schema(schema: dict) -> dict:
    """Drop conversation types from a ``getSchema()`` payload."""
    if not isinstance(schema, dict):
        return schema
    out = dict(schema)
    for key in ("VertexTypes", "EdgeTypes"):
        items = out.get(key)
        if isinstance(items, list):
            out[key] = [
                i
                for i in items
                if not is_chat_history_type(
                    i.get("Name") if isinstance(i, dict) else i
                )
            ]
    return out


def mentions_chat_history(*values) -> str | None:
    """Return the conversation type named anywhere in *values*, if any.

    Args are scanned rather than only the function name because that is where
    the type appears: ``getVertices('ChatConversation')`` is a well-formed
    call to an allowed function.
    """
    for value in values:
        if value is None:
            continue
        if isinstance(value, str):
            match = _CHAT_TYPE_RE.search(value)
            if match:
                return match.group(1)
        elif isinstance(value, dict):
            found = mentions_chat_history(*value.keys(), *value.values())
            if found:
                return found
        elif isinstance(value, (list, tuple, set)):
            found = mentions_chat_history(*value)
            if found:
                return found
    return None


def assert_agent_may_call(method: str, args: tuple, kwargs: dict) -> None:
    """Raise if an agent-issued call reaches conversation data.

    Raises rather than returning empty so the attempt is auditable and the
    model gets an error it can't mistake for "no results".
    """
    named = mentions_chat_history(args, kwargs)
    if named:
        logger.warning(
            "Refused agent call %s naming conversation type %r", method, named
        )
        raise ChatHistoryAccessDenied(
            f"{named} is not available. Conversation history is not part of the "
            f"knowledge graph and cannot be queried."
        )
