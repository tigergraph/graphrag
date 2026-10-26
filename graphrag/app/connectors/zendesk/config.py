"""Validated configuration for the Zendesk connector."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


SOURCE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
SUBDOMAIN_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
EXTERNAL_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,128}")
TicketStatus = Literal["new", "open", "pending", "hold", "solved", "closed"]


class ZendeskConnection(BaseModel):
    subdomain: str
    email: str = Field(min_length=3)
    api_token: str = ""

    @field_validator("subdomain")
    @classmethod
    def validate_subdomain(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized.endswith(".zendesk.com"):
            normalized = normalized[: -len(".zendesk.com")]
        if not SUBDOMAIN_RE.fullmatch(normalized):
            raise ValueError(
                "subdomain must be the Zendesk account name, for example acme"
            )
        return normalized

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        value = value.strip()
        if "@" not in value:
            raise ValueError("email must be the Zendesk agent email")
        return value


class ZendeskScope(BaseModel):
    updated_after: datetime | None = None
    statuses: list[TicketStatus] = Field(default_factory=list)
    jira_integration_external_id: str | None = None

    @field_validator("statuses")
    @classmethod
    def deduplicate_statuses(cls, values: list[TicketStatus]) -> list[TicketStatus]:
        return list(dict.fromkeys(values))

    @field_validator("jira_integration_external_id")
    @classmethod
    def validate_external_id(cls, value: str | None) -> str | None:
        if value is None or not str(value).strip():
            return None
        value = str(value).strip()
        if not EXTERNAL_ID_RE.fullmatch(value):
            raise ValueError(
                "jira_integration_external_id must use letters, numbers, '_' or '-'"
            )
        return value


class ZendeskSyncState(BaseModel):
    overlap_seconds: int = Field(default=120, ge=0, le=3600)
    checkpoint: datetime | None = None
    export_cursor: str | None = None
    pending_comment_ticket_ids: list[str] = Field(default_factory=list)
    linked_ticket_ids: list[str] = Field(default_factory=list)
    last_tested_at: datetime | None = None
    last_started_at: datetime | None = None
    last_completed_at: datetime | None = None
    last_error: str | None = None
    last_link_error: str | None = None
    last_ticket_count: int = Field(default=0, ge=0)


class ZendeskDataSource(BaseModel):
    id: str
    type: Literal["zendesk"] = "zendesk"
    enabled: bool = True
    display_name: str = Field(min_length=1, max_length=100)
    connection: ZendeskConnection
    scope: ZendeskScope = Field(default_factory=ZendeskScope)
    sync: ZendeskSyncState = Field(default_factory=ZendeskSyncState)

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        value = value.strip()
        if not SOURCE_ID_RE.fullmatch(value):
            raise ValueError(
                "id must be 1-64 characters using letters, numbers, '_' or '-'"
            )
        return value

    @field_validator("display_name")
    @classmethod
    def normalize_display_name(cls, value: str) -> str:
        return value.strip()


class ZendeskSourceFile(BaseModel):
    zendesk_sources: list[ZendeskDataSource] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_source_ids(self) -> "ZendeskSourceFile":
        ids = [source.id for source in self.zendesk_sources]
        if len(ids) != len(set(ids)):
            raise ValueError("data-source ids must be unique")
        return self
