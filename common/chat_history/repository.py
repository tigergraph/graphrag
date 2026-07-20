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
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)

_USER_TYPE = "ChatUser"
_CONVERSATION_TYPE = "ChatConversation"
_MESSAGE_TYPE = "ChatMessage"
_TRACE_TYPE = "ChatTrace"
_TRACE_STEP_TYPE = "ChatTraceStep"

_RETRIEVABLE_TYPES = frozenset({"DocumentChunk", "Entity", "Community"})


class ChatHistoryError(Exception):
    """Base for conversation-store failures."""


class NotConversationOwner(ChatHistoryError):
    """Raised when a write targets a conversation the principal doesn't own.

    Only ever raised on writes. Reads resolve to an empty result instead, so a
    caller cannot use the presence of an error to learn that a conversation id
    exists.
    """


def _now() -> int:
    return int(time.time())


def _attrs(vertex: dict) -> dict:
    """Flatten a pyTigerGraph vertex payload to its attributes."""
    return vertex.get("attributes", vertex)


def _is_missing_vertex_error(exc: Exception) -> bool:
    """True when TigerGraph rejected a VERTEX parameter that names no vertex.

    Matched on the message because pyTigerGraph raises a bare exception here
    rather than a typed one. Kept narrow: anything else must still propagate,
    since swallowing a real query failure would look identical to "this user
    has no conversations".
    """
    text = " ".join(str(a) for a in getattr(exc, "args", ()) or [str(exc)]).lower()
    if "failed to convert" in text and "vertex id" in text:
        return True
    return "not a valid vertex id" in text


def _next_seq(conversation: Optional[dict], message_id: str) -> int:
    """Position for *message_id* within *conversation*.

    Re-writing an existing message keeps its place — a retry or a feedback
    update must not reorder the thread. A new message goes after the current
    last. Sequential, so concurrent appends to one conversation could collide;
    a conversation is a single user talking to one assistant in order, so the
    contention isn't real, and ties fall back to create_epoch on read.
    """
    messages = (conversation or {}).get("messages") or []
    for message in messages:
        if message.get("message_id") == message_id:
            return int(message.get("seq") or 0)
    if not messages:
        return 1
    return max(int(m.get("seq") or 0) for m in messages) + 1


class _Repository:
    def __init__(self, conn, principal: str):
        if not principal or not isinstance(principal, str):
            raise ValueError("principal is required")
        self._conn = conn
        self._principal = principal

    @property
    def principal(self) -> str:
        return self._principal

    def _run(self, query: str, params: dict) -> list:
        """Run a principal-scoped query, tolerating a principal with no vertex.

        A ChatUser vertex is only created on first write, so every read for a
        user who has not chatted yet passes a VERTEX<ChatUser> parameter naming
        a vertex that does not exist. TigerGraph rejects that outright rather
        than matching nothing, which turns "no history" into an error — and on
        the write path that error surfaces inside the ownership probe, so a new
        user's first message is refused before their vertex can be created.

        A principal with no vertex owns nothing, which is exactly the empty
        result the query would return, so answer it here.
        """
        try:
            return self._conn.runInstalledQuery(query, params)
        except Exception as e:
            if _is_missing_vertex_error(e):
                logger.debug(
                    "%s: no ChatUser vertex for principal yet; empty result", query
                )
                return []
            raise

    def _principal_param(self) -> tuple:
        """The principal as a VERTEX<ChatUser> query argument.

        pyTigerGraph wants a 1-tuple here. A bare string still works but only
        via a failed POST and a GET retry, doubling the round-trips on every
        read.
        """
        return (self._principal,)

    def _ensure_user(self) -> None:
        self._conn.upsertVertex(_USER_TYPE, self._principal, {})


class ConversationRepository(_Repository):
    """Conversations and messages owned by a single principal."""

    def list_conversations(self) -> list[dict]:
        res = self._run("Chat_List_Conversations", {"u": self._principal_param()})
        if not res:
            return []
        convs = [_attrs(v) for v in res[0].get("Convs", [])]
        first_message = res[1].get("first_message", {}) if len(res) > 1 else {}
        for c in convs:
            c["first_message"] = first_message.get(c.get("conversation_id"), "")
        return convs

    def get_conversation(self, conversation_id: str) -> Optional[dict]:
        """Return the conversation and its ordered messages, or None.

        None covers both "does not exist" and "belongs to someone else"; the
        query cannot distinguish them and neither can this method, which is
        what stops the endpoint from confirming that an id exists.
        """
        res = self._run(
            "Chat_Get_Conversation",
            {"u": self._principal_param(), "conversation_id": conversation_id},
        )
        if not res:
            return None
        convs = res[0].get("Convs", [])
        if not convs:
            return None
        messages = [_attrs(m) for m in res[1].get("Msgs", [])] if len(res) > 1 else []
        parents = res[2].get("parents", {}) if len(res) > 2 else {}
        for message in messages:
            message["parent_id"] = parents.get(message.get("message_id")) or None
        return {**_attrs(convs[0]), "messages": messages}

    def _owned_or_absent(self, conversation_id: str) -> bool:
        """True if the principal owns *conversation_id* or nobody does.

        Reads can treat "not yours" and "not there" alike; writes cannot, or
        appending to another user's conversation id would quietly attach a
        second OWNS_CONVERSATION edge and hand the caller a copy of it. The
        existence probe is a primary-id lookup rather than a traversal, so it
        reveals nothing beyond whether the id is taken.
        """
        if self.get_conversation(conversation_id) is not None:
            return True
        try:
            existing = self._conn.getVerticesById(_CONVERSATION_TYPE, conversation_id)
        except Exception as e:
            # Only an absent-vertex error means "nobody owns it"; anything
            # else must propagate rather than let the write proceed.
            if _is_missing_vertex_error(e):
                return True
            raise
        return not existing

    def upsert_conversation(self, conversation_id: str, name: str = "") -> None:
        if not self._owned_or_absent(conversation_id):
            raise NotConversationOwner(conversation_id)
        self._ensure_user()
        now = _now()
        attributes = {
            "update_epoch": now,
            "deleted_epoch": 0,
            "create_epoch": (now, "ignore_if_exists"),
        }
        if name:
            attributes["name"] = name
        self._conn.upsertVertex(_CONVERSATION_TYPE, conversation_id, attributes)
        self._conn.upsertEdge(
            _USER_TYPE,
            self._principal,
            "OWNS_CONVERSATION",
            _CONVERSATION_TYPE,
            conversation_id,
        )

    def append_message(
        self,
        conversation_id: str,
        message_id: str,
        content: Optional[str] = None,
        role: Optional[str] = None,
        seq: Optional[int] = None,
        model_name: Optional[str] = None,
        response_time: Optional[float] = None,
        parent_id: Optional[str] = None,
        feedback: Optional[int] = None,
        comment: Optional[str] = None,
    ) -> None:
        """Create or update one message.

        Keyed on message_id, so a client retry re-upserts the same vertex
        rather than appending a duplicate — PRIMARY_ID makes the write
        idempotent without a separate dedupe table.

        ``None`` fields are omitted from the write rather than sent as empty.
        The same endpoint carries both a new message and a later feedback
        update, and the latter arrives with only message_id/feedback set —
        writing its empty content through would erase the message body.
        """
        if not self._owned_or_absent(conversation_id):
            raise NotConversationOwner(conversation_id)
        existing = self.get_conversation(conversation_id)
        self.upsert_conversation(conversation_id)

        if seq is None:
            # Callers on the chat path don't track position, and leaving seq
            # unset would order every message identically and hand the ordering
            # back to whatever the engine returns. Derive it: reuse the
            # message's own seq on a re-write so a retry or a feedback update
            # doesn't move it, otherwise append after the current last.
            seq = _next_seq(existing, message_id)

        attributes = {
            "content": content,
            "role": role,
            "model_name": model_name,
            "response_time": response_time,
            "seq": seq,
            "feedback": feedback,
            "comment": comment,
        }
        attributes = {k: v for k, v in attributes.items() if v is not None}
        attributes["create_epoch"] = (_now(), "ignore_if_exists")

        self._conn.upsertVertex(_MESSAGE_TYPE, message_id, attributes)
        self._conn.upsertEdge(
            _CONVERSATION_TYPE,
            conversation_id,
            "HAS_MESSAGE",
            _MESSAGE_TYPE,
            message_id,
        )
        if parent_id:
            self._conn.upsertEdge(
                _MESSAGE_TYPE, message_id, "REPLIES_TO", _MESSAGE_TYPE, parent_id
            )
        self._conn.upsertVertex(
            _CONVERSATION_TYPE, conversation_id, {"update_epoch": _now()}
        )

    def set_feedback(
        self, conversation_id: str, message_id: str, feedback: int, comment: str = ""
    ) -> None:
        conversation = self.get_conversation(conversation_id)
        if conversation is None:
            raise NotConversationOwner(conversation_id)
        owned = {m.get("message_id") for m in conversation.get("messages", [])}
        if message_id not in owned:
            raise NotConversationOwner(conversation_id)
        self._conn.upsertVertex(
            _MESSAGE_TYPE, message_id, {"feedback": feedback, "comment": comment}
        )

    def list_feedback(self) -> list[dict]:
        res = self._run("Chat_Get_Feedback", {"u": self._principal_param()})
        return [_attrs(m) for m in res[0].get("Msgs", [])] if res else []

    def search_messages(self, query: str, limit: int = 10) -> list[dict]:
        """Substring search over the principal's own messages.

        Backs the assistant's history tool. Takes no user argument for the
        same reason the rest of this class doesn't: the tool schema is written
        by the model, and an argument that exists is an argument that can be
        argued into a different value.
        """
        limit = max(1, min(int(limit or 10), 50))
        res = self._run(
            "Chat_Search_My_Messages",
            {"u": self._principal_param(), "q": query or "", "result_limit": limit},
        )
        return res[0].get("hits", []) if res else []

    def delete_conversation(self, conversation_id: str) -> bool:
        """Hard-delete the conversation and its messages, traces and steps.

        Returns False when the principal doesn't own it, without revealing
        whether it exists.
        """
        res = self._run(
            "Chat_Delete_Conversation",
            {"u": self._principal_param(), "conversation_id": conversation_id},
        )
        return bool(res and res[0].get("deleted", 0))


class TraceRetention:
    """Expiry of traces past their retention window.

    Traces carry the user's prompts and the text of retrieved documents, so
    the window is a privacy control rather than housekeeping. Replaces the
    file writer's 30-day sweep, which ran a full directory scan on every
    single trace write; this runs on a schedule instead.

    Has no principal: expiry spans all users by definition. Kept apart from
    ConversationRepository for the same reason as AdminFeedbackRepository —
    so the scoped class has no unscoped method.
    """

    #: Matches the file writer's previous behaviour.
    DEFAULT_MAX_AGE_DAYS = 30

    def __init__(self, conn):
        self._conn = conn

    def expire(self, max_age_days: int = DEFAULT_MAX_AGE_DAYS) -> int:
        cutoff = _now() - int(max_age_days) * 86400
        res = self._conn.runInstalledQuery(
            "Chat_Expire_Traces", {"cutoff_epoch": cutoff}
        )
        return int(res[0].get("deleted", 0)) if res else 0


class AdminFeedbackRepository:
    """Every user's feedback, for the roles chat_config permits.

    Deliberately not a ``_Repository``: it has no principal and returns other
    users' data by design, which is exactly the shape the rest of this module
    exists to prevent. Keeping it as its own class with its own query means
    ``ConversationRepository`` never grows an "all users" mode, and a reader
    can see at the import site which one is in play.

    Callers must gate on ``chat_config.conversationAccessRoles`` before
    constructing this — the class does not check roles itself, because it has
    no credentials to check them with.
    """

    def __init__(self, conn):
        self._conn = conn

    def list_all_feedback(self) -> list[dict]:
        res = self._conn.runInstalledQuery("Chat_Get_All_Feedback", {})
        return res[0].get("feedback", []) if res else []


class TraceRepository(_Repository):
    """Execution traces for a single principal's messages."""

    def get_trace(self, message_id: str) -> Optional[dict]:
        res = self._run(
            "Chat_Get_Trace", {"u": self._principal_param(), "message_id": message_id}
        )
        if not res:
            return None
        traces = res[0].get("Traces", [])
        if not traces:
            return None
        steps = [_attrs(s) for s in res[1].get("Steps", [])] if len(res) > 1 else []
        retrieved = (
            [
                {"id": r.get("v_id"), "type": r.get("v_type")}
                for r in res[2].get("Retrieved", [])
            ]
            if len(res) > 2
            else []
        )
        return {**_attrs(traces[0]), "steps": steps, "retrieved": retrieved}

    def save_trace(
        self,
        message_id: str,
        user_query: str,
        response_type: str = "",
        response_time: float = 0.0,
        answered_question: bool = False,
        natural_language_response: str = "",
        tokens_in: int = 0,
        tokens_out: int = 0,
        plan: str = "",
        steps: Optional[list[dict]] = None,
        retrieved: Optional[list[dict]] = None,
    ) -> None:
        """Persist a trace, its steps, and what it retrieved.

        trace_id is the message_id: the relationship is 1:1, so reusing the id
        makes the lookup a primary-id hit and keeps a retried write idempotent.

        ``retrieved`` is a list of ``{"id", "type"}`` naming corpus vertices
        fetched while answering; steps may carry their own ``retrieved`` when
        the caller has per-node attribution. Only the edge is stored, never the
        retrieved text — the file-based writer had to strip that text to keep
        trace files small and lost the provenance with it.

        An id that names no live vertex is dropped by TigerGraph rather than
        fabricating one, so a stale citation costs provenance for that chunk
        and cannot pollute the corpus.
        """
        self._conn.upsertVertex(
            _TRACE_TYPE,
            message_id,
            {
                "user_query": user_query,
                "response_type": response_type,
                "response_time": response_time,
                "answered_question": answered_question,
                "natural_language_response": natural_language_response,
                "tokens_in": tokens_in,
                "tokens_out": tokens_out,
                "plan": plan,
                "create_epoch": _now(),
            },
        )
        self._conn.upsertEdge(
            _MESSAGE_TYPE, message_id, "HAS_TRACE", _TRACE_TYPE, message_id
        )

        for target in self._live_targets(retrieved or []):
            self._link_retrieved(_TRACE_TYPE, message_id, target)

        previous_step_id = None
        for idx, step in enumerate(steps or []):
            step_id = f"{message_id}:{idx}"
            self._conn.upsertVertex(
                _TRACE_STEP_TYPE,
                step_id,
                {
                    "name": step.get("name", ""),
                    "idx": idx,
                    "duration_ms": step.get("duration_ms", 0),
                    "input": step.get("input", ""),
                    "output": step.get("output", ""),
                    "tokens_in": step.get("tokens_in", 0),
                    "tokens_out": step.get("tokens_out", 0),
                    "status": step.get("status", ""),
                },
            )
            self._conn.upsertEdge(
                _TRACE_TYPE, message_id, "HAS_STEP", _TRACE_STEP_TYPE, step_id
            )
            if previous_step_id is not None:
                self._conn.upsertEdge(
                    _TRACE_STEP_TYPE,
                    previous_step_id,
                    "NEXT_STEP",
                    _TRACE_STEP_TYPE,
                    step_id,
                )
            for target in self._live_targets(step.get("retrieved", [])):
                self._link_retrieved(_TRACE_STEP_TYPE, step_id, target)
            previous_step_id = step_id

    def _live_targets(self, targets: list[dict]) -> list[dict]:
        """Drop RETRIEVED targets that don't name a live corpus vertex.

        This filter is mandatory, not defensive. TigerGraph's upsert creates
        an endpoint that doesn't exist, so writing an edge for an unverified
        id fabricates an empty DocumentChunk *in the shared corpus* — a trace
        write silently corrupting the knowledge graph it is describing. The
        ids come from agent output and can name chunks that were re-ingested
        or deleted since.

        Batched per type (one lookup per type, normally just DocumentChunk),
        and it runs on the trace writer's thread, off the response path.
        """
        wanted: dict[str, set] = {}
        for target in targets:
            target_type, target_id = target.get("type"), target.get("id")
            if not target_type or not target_id:
                continue
            if target_type not in _RETRIEVABLE_TYPES:
                # RETRIEVED declares pairs only to the corpus types.
                logger.debug("Skipping RETRIEVED to unsupported type %r", target_type)
                continue
            wanted.setdefault(target_type, set()).add(str(target_id))

        live = []
        for target_type, ids in wanted.items():
            try:
                found = self._conn.getVerticesById(target_type, list(ids)) or []
            except Exception:
                logger.debug(
                    "RETRIEVED target lookup failed for %r; dropping %d id(s)",
                    target_type, len(ids), exc_info=True,
                )
                continue
            found_ids = {v.get("v_id") for v in found}
            missing = ids - found_ids
            if missing:
                logger.debug(
                    "Dropping %d RETRIEVED target(s) naming no live %s",
                    len(missing), target_type,
                )
            live.extend({"id": i, "type": target_type} for i in ids & found_ids)
        return live

    def _link_retrieved(self, source_type: str, source_id: str, target: dict) -> None:
        """Link to an already-verified target. See :meth:`_live_targets`."""
        self._conn.upsertEdge(
            source_type, source_id, "RETRIEVED", target["type"], target["id"]
        )
