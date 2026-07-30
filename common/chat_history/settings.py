"""Environment-only settings for the operational history graph."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from common.config_env import resolve_environment_placeholders

_GRAPH_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _positive_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if value <= 0:
        raise ValueError(f"{name} must be greater than zero")
    return value


def load_database_config() -> dict[str, Any]:
    """Load only ``db_config`` without importing application services.

    ``common.config`` initializes the configured LLM and embedding services at
    import time. Standalone history jobs must not trigger those side effects,
    so they read the same ``SERVER_CONFIG`` source directly.
    """

    source = os.getenv("SERVER_CONFIG", "configs/server_config.json").strip()
    if not source:
        raise ValueError("SERVER_CONFIG cannot be empty")

    try:
        if source.startswith("{"):
            server_config = json.loads(source)
        else:
            with Path(source).open(encoding="utf-8") as config_file:
                server_config = json.load(config_file)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Unable to load SERVER_CONFIG: {exc}") from exc

    if not isinstance(server_config, dict):
        raise ValueError("SERVER_CONFIG must contain a JSON object")
    db_config = resolve_environment_placeholders(server_config.get("db_config"))
    if not isinstance(db_config, dict):
        raise ValueError("db_config is not found in SERVER_CONFIG")
    if not str(db_config.get("hostname", "")).strip():
        raise ValueError("db_config.hostname is required")
    return dict(db_config)


@dataclass(frozen=True)
class HistorySettings:
    graph_name: str = "GraphRAGChatHistory"
    runtime_token: str = ""
    runtime_username: str = ""
    runtime_password: str = ""
    retention_days: int = 30
    default_page_size: int = 50
    max_page_size: int = 200
    agent_page_size: int = 20
    max_message_bytes: int = 256 * 1024
    max_trace_bytes: int = 512 * 1024
    max_step_summary_bytes: int = 16 * 1024
    max_trace_steps: int = 100
    worker_count: int = 8
    timeout_seconds: int = 10
    transient_attempts: int = 2

    @classmethod
    def from_env(cls) -> "HistorySettings":
        graph_name = os.getenv("CHAT_HISTORY_GRAPH", "GraphRAGChatHistory")
        if not _GRAPH_NAME.fullmatch(graph_name):
            raise ValueError("CHAT_HISTORY_GRAPH is not a valid TigerGraph name")

        default_page = _positive_int("CHAT_HISTORY_DEFAULT_PAGE_SIZE", 50)
        max_page = _positive_int("CHAT_HISTORY_MAX_PAGE_SIZE", 200)
        if default_page > max_page:
            raise ValueError(
                "CHAT_HISTORY_DEFAULT_PAGE_SIZE cannot exceed "
                "CHAT_HISTORY_MAX_PAGE_SIZE"
            )

        return cls(
            graph_name=graph_name,
            runtime_token=os.getenv("CHAT_HISTORY_TG_TOKEN", ""),
            runtime_username=os.getenv("CHAT_HISTORY_TG_USERNAME", ""),
            runtime_password=os.getenv("CHAT_HISTORY_TG_PASSWORD", ""),
            retention_days=_positive_int("CHAT_HISTORY_RETENTION_DAYS", 30),
            default_page_size=default_page,
            max_page_size=max_page,
            agent_page_size=min(
                _positive_int("CHAT_HISTORY_AGENT_PAGE_SIZE", 20), max_page
            ),
            max_message_bytes=_positive_int(
                "CHAT_HISTORY_MAX_MESSAGE_BYTES", 256 * 1024
            ),
            max_trace_bytes=_positive_int(
                "CHAT_HISTORY_MAX_TRACE_BYTES", 512 * 1024
            ),
            max_step_summary_bytes=_positive_int(
                "CHAT_HISTORY_MAX_STEP_SUMMARY_BYTES", 16 * 1024
            ),
            max_trace_steps=_positive_int("CHAT_HISTORY_MAX_TRACE_STEPS", 100),
            worker_count=_positive_int("CHAT_HISTORY_WORKERS", 8),
            timeout_seconds=_positive_int("CHAT_HISTORY_TIMEOUT_SECONDS", 10),
            transient_attempts=_positive_int(
                "CHAT_HISTORY_TRANSIENT_ATTEMPTS", 2
            ),
        )

    def clamp_page_size(self, value: int | None, *, agent: bool = False) -> int:
        default = self.agent_page_size if agent else self.default_page_size
        maximum = self.agent_page_size if agent else self.max_page_size
        if value is None:
            return default
        return max(1, min(int(value), maximum))
