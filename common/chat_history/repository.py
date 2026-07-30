"""Principal-bound repository for TigerGraph chat history.

The repository is the only Python layer allowed to call the operational
graph's installed queries. User-facing instances capture a
``HistoryPrincipal`` at construction time and never accept a caller-supplied
user ID.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any

from pyTigerGraph import TigerGraphConnection

from common.db.connection_utils import normalize_restpp_url

from .models import ConversationPage, HistoryMessage, TraceEnvelope, epoch_now
from .principal import HistoryPrincipal
from .redaction import (
    prepare_query_sources,
    prepare_trace_payload,
    redact_value,
    truncate_utf8,
)
from .settings import HistorySettings, load_database_config

logger = logging.getLogger(__name__)


class HistoryRepositoryError(RuntimeError):
    """Base class for typed repository failures."""


class HistoryConfigurationError(HistoryRepositoryError):
    pass


class HistoryNotFoundError(HistoryRepositoryError):
    pass


class HistoryConflictError(HistoryRepositoryError):
    pass


class HistoryPayloadTooLargeError(HistoryRepositoryError):
    pass


class HistoryUnavailableError(HistoryRepositoryError):
    pass


_EXECUTORS: dict[int, ThreadPoolExecutor] = {}
_EXECUTOR_LOCK = threading.Lock()


def _executor(worker_count: int) -> ThreadPoolExecutor:
    with _EXECUTOR_LOCK:
        executor = _EXECUTORS.get(worker_count)
        if executor is None:
            executor = ThreadPoolExecutor(
                max_workers=worker_count,
                thread_name_prefix="chat-history",
            )
            _EXECUTORS[worker_count] = executor
        return executor


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        default=str,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _payload_hash(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _model_dict(value: Any) -> dict[str, Any]:
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if isinstance(value, dict):
        return dict(value)
    raise TypeError(f"Expected a model or dict, got {type(value).__name__}")


def _epoch_to_iso(value: Any) -> str | None:
    try:
        epoch = int(value or 0)
    except (TypeError, ValueError):
        return None
    if epoch <= 0:
        return None
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )


def _decode_json(value: Any, default: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if not value:
        return default
    try:
        return json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


def _vertex_attributes(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    attributes = value.get("attributes")
    if isinstance(attributes, dict):
        out = dict(attributes)
        vertex_id = value.get("v_id")
        if vertex_id:
            out.setdefault("id", vertex_id)
        return out
    return dict(value)


def _payload(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    merged: dict[str, Any] = {}
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                merged.update(item)
    return merged


def _rows(raw: Any, key: str) -> list[dict[str, Any]]:
    value = _payload(raw).get(key, [])
    if isinstance(value, dict):
        value = value.get("list") or value.get("items") or []
    if not isinstance(value, list):
        return []
    return [_vertex_attributes(item) for item in value]


def _encode_cursor(value: dict[str, Any]) -> str:
    raw = _canonical_json(value).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        padding = "=" * (-len(value) % 4)
        decoded = base64.urlsafe_b64decode(value + padding)
        parsed = json.loads(decoded)
    except Exception as exc:
        raise ValueError("Invalid history cursor") from exc
    if not isinstance(parsed, dict):
        raise ValueError("Invalid history cursor")
    return parsed


def _conversation_api(
    attrs: dict[str, Any], principal: HistoryPrincipal
) -> dict[str, Any]:
    conversation_id = str(
        attrs.get("conversation_id") or attrs.get("id") or ""
    )
    return {
        "conversation_id": conversation_id,
        "user_id": principal.user_id,
        "name": attrs.get("title") or "",
        "graph_name": attrs.get("graph_name") or "",
        "status": attrs.get("status") or "active",
        "create_ts": _epoch_to_iso(attrs.get("created_at")),
        "update_ts": _epoch_to_iso(attrs.get("updated_at")),
        "delete_ts": _epoch_to_iso(attrs.get("deleted_at")),
        "_updated_at": int(attrs.get("updated_at") or 0),
    }


def _message_api(attrs: dict[str, Any]) -> dict[str, Any]:
    message_id = str(attrs.get("message_id") or attrs.get("id") or "")
    return {
        "id": int(attrs.get("sequence_no") or 0),
        "conversation_id": str(attrs.get("conversation_id") or ""),
        "message_id": message_id,
        "turn_id": str(attrs.get("turn_id") or ""),
        "parent_id": attrs.get("parent_message_id") or None,
        "model": attrs.get("model") or None,
        "content": attrs.get("content") or "",
        "answered_question": bool(attrs.get("answered_question") or False),
        "response_type": attrs.get("response_type") or None,
        "query_sources": _decode_json(
            attrs.get("query_sources_json"), None
        ),
        "role": attrs.get("role") or "",
        "response_time": float(attrs.get("response_time") or 0.0),
        "feedback": int(attrs.get("feedback") or 0),
        "comment": attrs.get("comment") or "",
        "create_ts": _epoch_to_iso(attrs.get("created_at")),
        "update_ts": _epoch_to_iso(attrs.get("updated_at")),
        "_created_at": int(attrs.get("created_at") or 0),
        "_updated_at": int(attrs.get("updated_at") or 0),
        "_sequence_no": int(attrs.get("sequence_no") or 0),
    }


@lru_cache(maxsize=4)
def _runtime_connection(
    graph_name: str,
    runtime_token: str,
    runtime_username: str,
    runtime_password: str,
    timeout_seconds: int,
) -> TigerGraphConnection:
    if not runtime_token and not (runtime_username and runtime_password):
        raise HistoryConfigurationError(
            "Set CHAT_HISTORY_TG_TOKEN or both CHAT_HISTORY_TG_USERNAME "
            "and CHAT_HISTORY_TG_PASSWORD"
        )

    db_config = load_database_config()
    kwargs: dict[str, Any] = {
        "host": db_config["hostname"],
        "graphname": graph_name,
        "restppPort": db_config.get("restppPort", "9000"),
        "gsPort": db_config.get("gsPort", "14240"),
    }
    if runtime_token:
        kwargs["apiToken"] = runtime_token
    else:
        kwargs["username"] = runtime_username
        kwargs["password"] = runtime_password

    conn = normalize_restpp_url(TigerGraphConnection(**kwargs))
    conn.customizeHeader(
        timeout=timeout_seconds * 1000,
        responseSize=5_000_000,
    )
    return conn


def _default_connection(settings: HistorySettings) -> TigerGraphConnection:
    return _runtime_connection(
        settings.graph_name,
        settings.runtime_token,
        settings.runtime_username,
        settings.runtime_password,
        settings.timeout_seconds,
    )


class _QueryRepository:
    def __init__(
        self,
        *,
        connection: Any | None = None,
        settings: HistorySettings | None = None,
    ) -> None:
        self.settings = settings or HistorySettings.from_env()
        self._connection = connection
        self._executor = _executor(self.settings.worker_count)

    @property
    def connection(self) -> Any:
        if self._connection is None:
            self._connection = _default_connection(self.settings)
        return self._connection

    def _run_query_sync(
        self, query_name: str, params: dict[str, Any]
    ) -> Any:
        last_error: Exception | None = None
        for attempt in range(self.settings.transient_attempts):
            try:
                return self.connection.runInstalledQuery(
                    query_name, params=params
                )
            except (
                HistoryConfigurationError,
                HistoryConflictError,
                HistoryNotFoundError,
                HistoryPayloadTooLargeError,
            ):
                raise
            except Exception as exc:
                last_error = exc
                if attempt + 1 < self.settings.transient_attempts:
                    time.sleep(0.1 * (attempt + 1))
        assert last_error is not None
        logger.warning(
            "History query %s failed after %d attempt(s): %s",
            query_name,
            self.settings.transient_attempts,
            last_error,
        )
        raise HistoryUnavailableError(
            f"TigerGraph history query {query_name} is unavailable"
        ) from last_error

    async def _run_query(
        self, query_name: str, params: dict[str, Any]
    ) -> Any:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            self._executor, self._run_query_sync, query_name, params
        )


class PrincipalHistoryRepository(_QueryRepository):
    """Repository permanently bound to one authenticated principal."""

    def __init__(
        self,
        principal: HistoryPrincipal,
        *,
        current_graph: str | None = None,
        connection: Any | None = None,
        settings: HistorySettings | None = None,
    ) -> None:
        super().__init__(connection=connection, settings=settings)
        self.principal = principal
        self.current_graph = current_graph
        if current_graph is not None and not principal.can_access_graph(
            current_graph
        ):
            raise HistoryNotFoundError("Conversation not found")

    def for_graph(self, graph_name: str) -> "PrincipalHistoryRepository":
        return PrincipalHistoryRepository(
            self.principal,
            current_graph=graph_name,
            connection=self._connection,
            settings=self.settings,
        )

    def _graph_candidates(self, graph_name: str | None = None) -> list[str]:
        selected = graph_name or self.current_graph
        if selected:
            if not self.principal.can_access_graph(selected):
                raise HistoryNotFoundError("Conversation not found")
            return [selected]
        return sorted(self.principal.accessible_graphs)

    def _check_message_size(self, content: str) -> None:
        if len((content or "").encode("utf-8")) > self.settings.max_message_bytes:
            raise HistoryPayloadTooLargeError(
                f"Message exceeds {self.settings.max_message_bytes} bytes"
            )

    @staticmethod
    def _status(raw: Any) -> str:
        return str(_payload(raw).get("status") or "")

    @staticmethod
    def _require_mutation(raw: Any) -> None:
        status = PrincipalHistoryRepository._status(raw)
        if status == "conflict":
            raise HistoryConflictError("History idempotency key collision")
        if status not in {"created", "replayed", "ok"}:
            raise HistoryNotFoundError("Conversation not found")

    async def begin_turn(
        self,
        message: HistoryMessage | dict[str, Any],
        *,
        graph_name: str | None = None,
        create_if_missing: bool = False,
        event_time: int | None = None,
    ) -> dict[str, Any]:
        graph = self._graph_candidates(graph_name)[0]
        data = _model_dict(message)
        if str(data.get("role")) != "user":
            raise ValueError("begin_turn requires a user message")
        content = str(data.get("content") or "")
        self._check_message_size(content)
        event_time = event_time or epoch_now()
        title = " ".join(content.split())[:256]
        turn_id = str(data.get("turn_id") or data["message_id"])
        idempotent = {
            "conversation_id": data["conversation_id"],
            "message_id": data["message_id"],
            "turn_id": turn_id,
            "graph_name": graph,
            "role": "user",
            "content": content,
            "model": data.get("model"),
        }
        raw = await self._run_query(
            "Chat_Begin_Turn",
            {
                "principal_id": self.principal.user_id,
                "graph_name": graph,
                "conversation_id": data["conversation_id"],
                "conversation_title": title,
                "conversation_hash": _payload_hash(
                    {
                        "conversation_id": data["conversation_id"],
                        "principal_id": self.principal.user_id,
                        "graph_name": graph,
                    }
                ),
                "turn_id": turn_id,
                "message_id": data["message_id"],
                "parent_message_id": data.get("parent_id") or "",
                "model_name": data.get("model") or "",
                "content": content,
                "payload_hash": _payload_hash(idempotent),
                "event_time": event_time,
                "create_if_missing": bool(create_if_missing),
            },
        )
        self._require_mutation(raw)
        return {
            "conversation_id": data["conversation_id"],
            "message_id": data["message_id"],
            "turn_id": turn_id,
            "status": self._status(raw),
        }

    async def complete_turn(
        self,
        *,
        user_message_id: str,
        assistant_message: HistoryMessage | dict[str, Any],
        trace: TraceEnvelope,
        graph_name: str | None = None,
        event_time: int | None = None,
    ) -> dict[str, Any]:
        graph = self._graph_candidates(graph_name)[0]
        data = _model_dict(assistant_message)
        if str(data.get("role")) != "system":
            raise ValueError("complete_turn requires a system message")
        content = str(data.get("content") or "")
        self._check_message_size(content)
        event_time = event_time or trace.created_at or epoch_now()

        trace_data, trace_cut = prepare_trace_payload(
            trace.trace_data, self.settings
        )
        trace.truncated = bool(trace.truncated or trace_cut)
        trace_json = _canonical_json(trace_data)
        provenance, provenance_cut = prepare_trace_payload(
            trace.provenance, self.settings
        )
        provenance_json = _canonical_json(provenance)
        trace.truncated = bool(trace.truncated or provenance_cut)
        safe_query_sources, sources_cut = prepare_query_sources(
            data.get("query_sources"), self.settings
        )
        query_sources_json = _canonical_json(safe_query_sources)
        trace.truncated = bool(trace.truncated or sources_cut)
        if len(trace.steps) > self.settings.max_trace_steps:
            trace.truncated = True
        bounded_steps = trace.steps[: self.settings.max_trace_steps]
        valid_step_ids = {step.step_id for step in bounded_steps}
        steps_payload = []
        for step in bounded_steps:
            step_data = step.model_dump()
            for field in (
                "input_summary",
                "output_summary",
                "error_summary",
            ):
                step_data[field], cut = truncate_utf8(
                    redact_value(step_data.get(field, ""), key=field),
                    self.settings.max_step_summary_bytes,
                )
                trace.truncated = bool(trace.truncated or cut)
            step_data["depends_on"] = [
                dependency
                for dependency in step_data.get("depends_on", [])
                if dependency in valid_step_ids
            ]
            step_data["depends_on_json"] = _canonical_json(
                step_data.pop("depends_on")
            )
            steps_payload.append(step_data)
        steps_json = (
            _canonical_json(steps_payload) if steps_payload else ""
        )

        def trace_storage_bytes() -> int:
            return len(
                (
                    trace_json
                    + provenance_json
                    + steps_json
                ).encode("utf-8")
            )

        if trace_storage_bytes() > self.settings.max_trace_bytes:
            trace.truncated = True
            steps_payload = []
            steps_json = ""
            provenance = {"truncated": True}
            provenance_json = _canonical_json(provenance)
        if trace_storage_bytes() > self.settings.max_trace_bytes:
            trace_data = {
                key: value
                for key, value in trace_data.items()
                if key
                in {
                    "message_id",
                    "conversation_id",
                    "request_id",
                    "status",
                    "response_type",
                }
            }
            trace_data["truncated"] = True
            trace_json = _canonical_json(trace_data)

        message_hash_input = {
            "conversation_id": data["conversation_id"],
            "message_id": data["message_id"],
            "turn_id": data.get("turn_id"),
            "parent_id": user_message_id,
            "role": "system",
            "content": content,
            "model": data.get("model"),
            "answered_question": bool(data.get("answered_question")),
            "response_type": data.get("response_type"),
            "query_sources": safe_query_sources,
        }
        raw = await self._run_query(
            "Chat_Complete_Turn",
            {
                "principal_id": self.principal.user_id,
                "graph_name": graph,
                "conversation_id": data["conversation_id"],
                "turn_id": data.get("turn_id") or trace.trace_id,
                "user_message_id": user_message_id,
                "assistant_message_id": data["message_id"],
                "model_name": data.get("model") or "",
                "content": content,
                "response_time": float(data.get("response_time") or 0.0),
                "answered_question": bool(
                    data.get("answered_question") or False
                ),
                "response_type": data.get("response_type") or "",
                "query_sources_json": query_sources_json,
                "message_payload_hash": _payload_hash(message_hash_input),
                "trace_id": trace.trace_id,
                "request_id": trace.request_id,
                "trace_status": trace.status,
                "trace_json": trace_json,
                "provenance_json": provenance_json,
                "steps_json": steps_json,
                "trace_truncated": trace.truncated,
                "trace_payload_hash": _payload_hash(
                    {
                        "trace": trace_data,
                        "provenance": provenance,
                        "steps": steps_payload,
                        "truncated": trace.truncated,
                    }
                ),
                "event_time": event_time,
                "expires_at": trace.expires_at
                or (
                    event_time
                    + self.settings.retention_days * 24 * 60 * 60
                ),
            },
        )
        self._require_mutation(raw)
        return {
            "conversation_id": data["conversation_id"],
            "message_id": data["message_id"],
            "trace_id": trace.trace_id,
            "status": self._status(raw),
        }

    def _list_conversations_sync(
        self,
        *,
        graph_name: str | None = None,
        cursor: str | None = None,
        limit: int | None = None,
        agent: bool = False,
    ) -> ConversationPage:
        size = self.settings.clamp_page_size(limit, agent=agent)
        decoded = _decode_cursor(cursor)
        all_items: list[dict[str, Any]] = []
        for graph in self._graph_candidates(graph_name):
            raw = self._run_query_sync(
                "Chat_List_My_Conversations",
                {
                    "principal_id": self.principal.user_id,
                    "graph_name": graph,
                    "cursor_updated_at": int(
                        decoded.get("updated_at") or 0
                    ),
                    "cursor_id": str(decoded.get("id") or ""),
                    "page_size": size,
                },
            )
            all_items.extend(
                _conversation_api(row, self.principal)
                for row in _rows(raw, "rows")
            )
        all_items.sort(
            key=lambda item: (
                int(item.get("_updated_at") or 0),
                item.get("conversation_id") or "",
            ),
            reverse=True,
        )
        page_items = all_items[:size]
        next_cursor = None
        if len(all_items) >= size and page_items:
            last = page_items[-1]
            next_cursor = _encode_cursor(
                {
                    "updated_at": last["_updated_at"],
                    "id": last["conversation_id"],
                }
            )
        for item in page_items:
            item.pop("_updated_at", None)
        return ConversationPage(items=page_items, next_cursor=next_cursor)

    async def list_conversations(
        self,
        *,
        graph_name: str | None = None,
        cursor: str | None = None,
        limit: int | None = None,
    ) -> ConversationPage:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            self._executor,
            lambda: self._list_conversations_sync(
                graph_name=graph_name, cursor=cursor, limit=limit
            ),
        )

    def list_conversations_for_agent(
        self, *, cursor: str | None = None, limit: int | None = None
    ) -> ConversationPage:
        return self._list_conversations_sync(
            cursor=cursor, limit=limit, agent=True
        )

    def _get_conversation_sync(
        self,
        conversation_id: str,
        *,
        cursor: str | None = None,
        limit: int | None = None,
        graph_name: str | None = None,
    ) -> tuple[list[dict[str, Any]], str | None, str]:
        size = self.settings.clamp_page_size(limit)
        decoded = _decode_cursor(cursor)
        after_sequence = int(decoded.get("sequence") or 0)
        for graph in self._graph_candidates(graph_name):
            raw = self._run_query_sync(
                "Chat_Get_My_Conversation",
                {
                    "principal_id": self.principal.user_id,
                    "graph_name": graph,
                    "conversation_id": conversation_id,
                    "after_sequence": after_sequence,
                    "page_size": size,
                },
            )
            conversations = _rows(raw, "conversation")
            if not conversations:
                continue
            messages = [_message_api(row) for row in _rows(raw, "rows")]
            next_cursor = None
            if len(messages) >= size and messages:
                next_cursor = _encode_cursor(
                    {"sequence": messages[-1]["_sequence_no"] + 1}
                )
            for item in messages:
                item.pop("_created_at", None)
                item.pop("_updated_at", None)
                item.pop("_sequence_no", None)
            return messages, next_cursor, graph
        raise HistoryNotFoundError("Conversation not found")

    async def get_conversation(
        self,
        conversation_id: str,
        *,
        cursor: str | None = None,
        limit: int | None = None,
        graph_name: str | None = None,
    ) -> tuple[list[dict[str, Any]], str | None, str]:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            self._executor,
            lambda: self._get_conversation_sync(
                conversation_id,
                cursor=cursor,
                limit=limit,
                graph_name=graph_name,
            ),
        )

    def get_conversation_for_agent(
        self,
        conversation_id: str,
        *,
        cursor: str | None = None,
        limit: int | None = None,
    ) -> tuple[list[dict[str, Any]], str | None, str]:
        return self._get_conversation_sync(
            conversation_id,
            cursor=cursor,
            limit=min(
                limit or self.settings.agent_page_size,
                self.settings.agent_page_size,
            ),
        )

    async def load_agent_history(
        self, conversation_id: str
    ) -> list[dict[str, str]]:
        messages, _, _ = await self.get_conversation(
            conversation_id,
            limit=self.settings.max_page_size,
        )
        users = {
            message["message_id"]: message
            for message in messages
            if message.get("role") == "user"
        }
        history: list[dict[str, str]] = []
        for message in messages:
            if message.get("role") != "system":
                continue
            parent = users.get(str(message.get("parent_id") or ""))
            if parent is None:
                continue
            history.append(
                {
                    "query": parent.get("content") or "",
                    "response": message.get("content") or "",
                    "create_ts": message.get("create_ts"),
                    "update_ts": message.get("update_ts"),
                }
            )
        return history

    def _search_messages_sync(
        self,
        query: str,
        *,
        cursor: str | None = None,
        limit: int | None = None,
        agent: bool = False,
    ) -> ConversationPage:
        normalized = " ".join((query or "").split())
        if not normalized:
            raise ValueError("Search text is required")
        if len(normalized.encode("utf-8")) > 1024:
            raise HistoryPayloadTooLargeError(
                "Search text exceeds 1024 bytes"
            )
        size = self.settings.clamp_page_size(limit, agent=agent)
        decoded = _decode_cursor(cursor)
        items: list[dict[str, Any]] = []
        for graph in self._graph_candidates():
            raw = self._run_query_sync(
                "Chat_Search_My_Messages",
                {
                    "principal_id": self.principal.user_id,
                    "graph_name": graph,
                    "search_text": normalized,
                    "before_created_at": int(
                        decoded.get("created_at") or 0
                    ),
                    "before_id": str(decoded.get("id") or ""),
                    "page_size": size,
                },
            )
            items.extend(_message_api(row) for row in _rows(raw, "rows"))
        items.sort(
            key=lambda item: (
                item.get("_created_at") or 0,
                item.get("message_id") or "",
            ),
            reverse=True,
        )
        items = items[:size]
        next_cursor = None
        if len(items) >= size and items:
            next_cursor = _encode_cursor(
                {
                    "created_at": items[-1]["_created_at"],
                    "id": items[-1]["message_id"],
                }
            )
        for item in items:
            item.pop("_created_at", None)
            item.pop("_updated_at", None)
            item.pop("_sequence_no", None)
        return ConversationPage(items=items, next_cursor=next_cursor)

    async def list_feedback(
        self, *, cursor: str | None = None, limit: int | None = None
    ) -> ConversationPage:
        size = self.settings.clamp_page_size(limit)
        decoded = _decode_cursor(cursor)
        items: list[dict[str, Any]] = []
        for graph in self._graph_candidates():
            raw = await self._run_query(
                "Chat_Get_My_Feedback",
                {
                    "principal_id": self.principal.user_id,
                    "graph_name": graph,
                    "cursor_updated_at": int(
                        decoded.get("updated_at") or 0
                    ),
                    "cursor_id": str(decoded.get("id") or ""),
                    "page_size": size,
                },
            )
            items.extend(_message_api(row) for row in _rows(raw, "rows"))
        items.sort(
            key=lambda item: (
                item.get("_updated_at") or 0,
                item.get("message_id") or "",
            ),
            reverse=True,
        )
        items = items[:size]
        next_cursor = None
        if len(items) >= size and items:
            next_cursor = _encode_cursor(
                {
                    "updated_at": items[-1]["_updated_at"],
                    "id": items[-1]["message_id"],
                }
            )
        for item in items:
            item.pop("_created_at", None)
            item.pop("_updated_at", None)
            item.pop("_sequence_no", None)
        return ConversationPage(items=items, next_cursor=next_cursor)

    async def search_messages(
        self,
        query: str,
        *,
        cursor: str | None = None,
        limit: int | None = None,
    ) -> ConversationPage:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            self._executor,
            lambda: self._search_messages_sync(
                query, cursor=cursor, limit=limit
            ),
        )

    def search_messages_for_agent(
        self,
        query: str,
        *,
        cursor: str | None = None,
        limit: int | None = None,
    ) -> ConversationPage:
        return self._search_messages_sync(
            query, cursor=cursor, limit=limit, agent=True
        )

    async def update_feedback(
        self,
        *,
        conversation_id: str,
        message_id: str,
        feedback: int,
        comment: str = "",
    ) -> None:
        if feedback not in {0, 1, 2}:
            raise ValueError("feedback must be 0, 1, or 2")
        for graph in self._graph_candidates():
            raw = await self._run_query(
                "Chat_Update_My_Feedback",
                {
                    "principal_id": self.principal.user_id,
                    "graph_name": graph,
                    "conversation_id": conversation_id,
                    "message_id": message_id,
                    "feedback": feedback,
                    "comment": comment[:4096],
                    "event_time": epoch_now(),
                },
            )
            if int(_payload(raw).get("updated") or 0) > 0:
                return
        raise HistoryNotFoundError("Message not found")

    async def delete_conversation(self, conversation_id: str) -> None:
        for graph in self._graph_candidates():
            raw = await self._run_query(
                "Chat_Delete_My_Conversation",
                {
                    "principal_id": self.principal.user_id,
                    "graph_name": graph,
                    "conversation_id": conversation_id,
                    "event_time": epoch_now(),
                },
            )
            if int(_payload(raw).get("deleted") or 0) > 0:
                return
        raise HistoryNotFoundError("Conversation not found")

    async def get_trace(self, message_id: str) -> dict[str, Any]:
        if not self.principal.is_trace_reader():
            raise PermissionError(
                "Trace access requires the conversation owner to be a superuser"
            )
        for graph in self._graph_candidates():
            raw = await self._run_query(
                "Chat_Get_My_Trace",
                {
                    "principal_id": self.principal.user_id,
                    "graph_name": graph,
                    "message_id": message_id,
                },
            )
            traces = _rows(raw, "trace")
            if not traces:
                continue
            data = _decode_json(traces[0].get("trace_json"), {})
            if not isinstance(data, dict):
                data = {}
            data.setdefault("message_id", message_id)
            data.setdefault(
                "conversation_id", traces[0].get("conversation_id")
            )
            data.setdefault("username", self.principal.user_id)
            data["truncated"] = bool(
                data.get("truncated") or traces[0].get("truncated")
            )
            return data
        raise HistoryNotFoundError("Trace log not found")


class AdminHistoryRepository(_QueryRepository):
    """Explicit cross-user operations guarded by an authenticated admin."""

    def __init__(
        self,
        principal: HistoryPrincipal,
        *,
        connection: Any | None = None,
        settings: HistorySettings | None = None,
    ) -> None:
        if not principal.is_history_admin():
            raise PermissionError("History administrator role required")
        super().__init__(connection=connection, settings=settings)
        self.principal = principal

    async def list_feedback(
        self, *, cursor: str | None = None, limit: int | None = None
    ) -> ConversationPage:
        size = self.settings.clamp_page_size(limit)
        decoded = _decode_cursor(cursor)
        raw = await self._run_query(
            "Chat_Get_All_Feedback_Admin",
            {
                "cursor_updated_at": int(
                    decoded.get("updated_at") or 0
                ),
                "cursor_id": str(decoded.get("id") or ""),
                "page_size": size,
            },
        )
        items = [_message_api(row) for row in _rows(raw, "rows")]
        next_cursor = None
        if len(items) >= size and items:
            last = items[-1]
            next_cursor = _encode_cursor(
                {
                    "updated_at": last["_updated_at"],
                    "id": last["message_id"],
                }
            )
        for item in items:
            item.pop("_created_at", None)
            item.pop("_updated_at", None)
            item.pop("_sequence_no", None)
        return ConversationPage(items=items, next_cursor=next_cursor)

    async def expire_traces(
        self, *, cutoff_epoch: int | None = None, batch_size: int = 500
    ) -> int:
        raw = await self._run_query(
            "Chat_Expire_Traces_Admin",
            {
                "cutoff_epoch": cutoff_epoch or epoch_now(),
                "batch_size": max(1, min(int(batch_size), 500)),
            },
        )
        return int(_payload(raw).get("expired") or 0)


def create_history_repository(
    principal: HistoryPrincipal,
    *,
    current_graph: str | None = None,
    connection: Any | None = None,
    settings: HistorySettings | None = None,
) -> PrincipalHistoryRepository:
    return PrincipalHistoryRepository(
        principal,
        current_graph=current_graph,
        connection=connection,
        settings=settings,
    )


async def check_history_health(
    *,
    connection: Any | None = None,
    settings: HistorySettings | None = None,
) -> dict[str, Any]:
    repository = _QueryRepository(
        connection=connection,
        settings=settings,
    )
    raw = await repository._run_query("Chat_History_Health", {})
    payload = _payload(raw)
    if str(payload.get("status") or "") != "ok":
        raise HistoryUnavailableError("TigerGraph history health check failed")
    return payload


def create_admin_history_repository(
    principal: HistoryPrincipal,
    *,
    connection: Any | None = None,
    settings: HistorySettings | None = None,
) -> AdminHistoryRepository:
    return AdminHistoryRepository(
        principal, connection=connection, settings=settings
    )
