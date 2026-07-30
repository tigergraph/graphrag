"""Bound and redact trace data before it reaches TigerGraph."""

from __future__ import annotations

import json
import re
from typing import Any

from .models import TraceStep
from .settings import HistorySettings

_SECRET_FRAGMENTS = (
    "authorization",
    "api_key",
    "apikey",
    "password",
    "secret",
    "token",
    "credential",
    "cookie",
)
_BODY_KEYS = {
    "final_retrieval",
    "document_body",
    "full_document",
    "raw_document",
    "chunk_text",
}
_INLINE_SECRET = re.compile(
    r"(?i)\b(password|passwd|api[_-]?key|secret|token|credential)"
    r"(\s*[:=]\s*)([^\s,;]+)"
)
_BEARER_SECRET = re.compile(
    r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+"
)


def _byte_len(value: str) -> int:
    return len(value.encode("utf-8"))


def truncate_utf8(value: Any, max_bytes: int) -> tuple[str, bool]:
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    raw = text.encode("utf-8")
    if len(raw) <= max_bytes:
        return text, False
    marker = "\n...[truncated]"
    marker_raw = marker.encode("utf-8")
    if max_bytes <= len(marker_raw):
        return raw[:max_bytes].decode("utf-8", errors="ignore"), True
    budget = max_bytes - len(marker_raw)
    shortened = raw[:budget].decode("utf-8", errors="ignore") + marker
    return shortened, True


def redact_value(value: Any, *, key: str = "") -> Any:
    lowered = key.lower()
    if any(fragment in lowered for fragment in _SECRET_FRAGMENTS):
        return "[redacted]"
    if lowered in _BODY_KEYS:
        return "[omitted]"
    if isinstance(value, dict):
        return {
            str(child_key): redact_value(child_value, key=str(child_key))
            for child_key, child_value in value.items()
        }
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    if isinstance(value, tuple):
        return [redact_value(item) for item in value]
    if isinstance(value, str):
        value = _BEARER_SECRET.sub("Bearer [redacted]", value)
        return _INLINE_SECRET.sub(
            lambda match: (
                f"{match.group(1)}{match.group(2)}[redacted]"
            ),
            value,
        )
    return value


def prepare_trace_payload(
    trace_data: dict[str, Any],
    settings: HistorySettings,
) -> tuple[dict[str, Any], bool]:
    redacted = redact_value(trace_data)
    encoded = json.dumps(redacted, default=str, separators=(",", ":"))
    if _byte_len(encoded) <= settings.max_trace_bytes:
        return redacted, False

    # Preserve the stable envelope fields and replace the potentially large
    # query-source body with an explicit truncation marker.
    bounded = {
        key: value
        for key, value in redacted.items()
        if key
        in {
            "message_id",
            "conversation_id",
            "username",
            "user_query",
            "response_time",
            "response_type",
            "answered_question",
            "natural_language_response",
            "timestamp",
            "request_id",
            "status",
        }
    }
    bounded["query_sources"] = {"truncated": True}
    bounded["truncated"] = True
    encoded = json.dumps(bounded, default=str, separators=(",", ":"))
    if _byte_len(encoded) > settings.max_trace_bytes:
        response = str(bounded.pop("natural_language_response", ""))
        envelope_bytes = _byte_len(
            json.dumps(bounded, default=str, separators=(",", ":"))
        )
        response_budget = max(
            0, settings.max_trace_bytes - envelope_bytes - 40
        )
        bounded["natural_language_response"], _ = truncate_utf8(
            response, response_budget
        )
    encoded = json.dumps(bounded, default=str, separators=(",", ":"))
    if _byte_len(encoded) > settings.max_trace_bytes:
        bounded = {"truncated": True}
    return bounded, True


def prepare_query_sources(
    query_sources: dict[str, Any] | None,
    settings: HistorySettings,
) -> tuple[dict[str, Any], bool]:
    """Redact and bound sources stored with a reusable chat message."""
    redacted = redact_value(query_sources or {})
    encoded = json.dumps(redacted, default=str, separators=(",", ":"))
    if _byte_len(encoded) <= settings.max_trace_bytes:
        return redacted, False
    return {"truncated": True}, True


def build_trace_steps(
    trace_id: str,
    query_sources: dict[str, Any] | None,
    settings: HistorySettings,
) -> tuple[list[TraceStep], bool]:
    raw_steps = (query_sources or {}).get("agent_steps") or []
    if not isinstance(raw_steps, list):
        return [], False

    selected_steps = raw_steps[: settings.max_trace_steps]
    valid_step_ids: set[str] = set()
    for ordinal, raw_step in enumerate(selected_steps):
        step = raw_step if isinstance(raw_step, dict) else {}
        raw_id = str(step.get("id") or step.get("step_id") or ordinal)
        valid_step_ids.add(f"{trace_id}:{raw_id}")
    truncated = len(raw_steps) > settings.max_trace_steps
    steps: list[TraceStep] = []
    for ordinal, raw_step in enumerate(selected_steps):
        step = raw_step if isinstance(raw_step, dict) else {"output": raw_step}
        raw_id = str(step.get("id") or step.get("step_id") or ordinal)
        step_id = f"{trace_id}:{raw_id}"
        input_summary, input_cut = truncate_utf8(
            redact_value(step.get("input", "")),
            settings.max_step_summary_bytes,
        )
        output_summary, output_cut = truncate_utf8(
            redact_value(step.get("output", "")),
            settings.max_step_summary_bytes,
        )
        error_summary, error_cut = truncate_utf8(
            redact_value(step.get("error", "")),
            settings.max_step_summary_bytes,
        )
        depends_on = [
            f"{trace_id}:{dependency}"
            for dependency in (step.get("depends_on") or [])
            if f"{trace_id}:{dependency}" in valid_step_ids
        ]
        steps.append(
            TraceStep(
                step_id=step_id,
                ordinal=ordinal,
                step_type=str(step.get("kind") or step.get("node") or ""),
                tool_name=str(step.get("tool") or step.get("node") or ""),
                status=str(step.get("status") or "completed"),
                duration=float(
                    step.get("duration")
                    or step.get("duration_s")
                    or 0.0
                ),
                input_summary=input_summary,
                output_summary=output_summary,
                error_summary=error_summary,
                depends_on=depends_on,
            )
        )
        truncated = truncated or input_cut or output_cut or error_cut
    return steps, truncated
