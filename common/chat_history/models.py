"""Transport-neutral models used by the history repositories and tools."""

from __future__ import annotations

import enum
import time
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


def epoch_now() -> int:
    return int(time.time())


class MessageRole(str, enum.Enum):
    USER = "user"
    SYSTEM = "system"


class HistoryMessage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    conversation_id: str
    message_id: str
    turn_id: str = ""
    parent_id: str | None = None
    model: str | None = None
    content: str = ""
    answered_question: bool = False
    response_type: str | None = None
    query_sources: dict[str, Any] | None = None
    role: str
    response_time: float = 0.0
    feedback: int = 0
    comment: str = ""
    create_ts: str | None = None
    update_ts: str | None = None

    @field_validator("response_time", mode="before")
    @classmethod
    def normalize_nullable_response_time(cls, value: Any) -> Any:
        """Apply the transport default when the UI explicitly sends null."""
        return 0.0 if value is None else value

    @field_validator("feedback", mode="before")
    @classmethod
    def normalize_nullable_feedback(cls, value: Any) -> Any:
        """Apply the transport default when the UI explicitly sends null."""
        return 0 if value is None else value

    @field_validator("comment", mode="before")
    @classmethod
    def normalize_nullable_comment(cls, value: Any) -> Any:
        """Apply the transport default when the UI explicitly sends null."""
        return "" if value is None else value


class TraceStep(BaseModel):
    model_config = ConfigDict(extra="ignore")

    step_id: str
    ordinal: int = Field(ge=0)
    step_type: str = ""
    tool_name: str = ""
    status: str = "completed"
    duration: float = 0.0
    input_summary: str = ""
    output_summary: str = ""
    error_summary: str = ""
    depends_on: list[str] = Field(default_factory=list)


class TraceEnvelope(BaseModel):
    model_config = ConfigDict(extra="ignore")

    trace_id: str
    message_id: str
    conversation_id: str
    request_id: str = ""
    status: str = "completed"
    response_type: str = ""
    elapsed: float = 0.0
    trace_data: dict[str, Any] = Field(default_factory=dict)
    provenance: dict[str, Any] = Field(default_factory=dict)
    steps: list[TraceStep] = Field(default_factory=list)
    truncated: bool = False
    created_at: int = Field(default_factory=epoch_now)
    expires_at: int = 0


class ConversationPage(BaseModel):
    items: list[dict[str, Any]] = Field(default_factory=list)
    next_cursor: str | None = None
