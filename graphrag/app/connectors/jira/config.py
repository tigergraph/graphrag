"""Validated configuration for the Jira Cloud connector."""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, Field, field_validator, model_validator


SOURCE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
PROJECT_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,31}$")


class JiraConnection(BaseModel):
    site_url: str
    email: str = Field(min_length=3)
    api_token: str = ""
    cloud_id: str | None = None

    @field_validator("site_url")
    @classmethod
    def validate_site_url(cls, value: str) -> str:
        normalized = value.strip().rstrip("/")
        parsed = urlparse(normalized)
        if parsed.scheme != "https" or not parsed.hostname:
            raise ValueError("site_url must be a valid HTTPS URL")
        if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
            raise ValueError("site_url must not include a path, query, or fragment")
        return normalized

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        value = value.strip()
        if "@" not in value:
            raise ValueError("email must be a valid Atlassian account email")
        return value


class JiraScope(BaseModel):
    project_keys: list[str] = Field(default_factory=list)
    created_after: date | None = None
    updated_after: date | None = None
    status_categories: list[
        Literal["new", "indeterminate", "done"]
    ] = Field(default_factory=list)
    jql_extra: str = ""
    include_comments: bool = True
    story_points_field: str | None = None

    @field_validator("project_keys")
    @classmethod
    def normalize_project_keys(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        seen: set[str] = set()
        for raw in values:
            key = raw.strip().upper()
            if not PROJECT_KEY_RE.fullmatch(key):
                raise ValueError(f"invalid Jira project key: {raw!r}")
            if key not in seen:
                normalized.append(key)
                seen.add(key)
        return normalized

    @field_validator("status_categories")
    @classmethod
    def deduplicate_status_categories(
        cls,
        values: list[Literal["new", "indeterminate", "done"]],
    ) -> list[Literal["new", "indeterminate", "done"]]:
        return list(dict.fromkeys(values))

    @field_validator("jql_extra")
    @classmethod
    def validate_jql_extra(cls, value: str) -> str:
        value = value.strip()
        if re.search(r"\border\s+by\b", value, flags=re.IGNORECASE):
            raise ValueError("jql_extra must not contain ORDER BY")
        return value

    @field_validator("story_points_field")
    @classmethod
    def validate_story_points_field(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        value = value.strip()
        if not re.fullmatch(r"customfield_\d+", value):
            raise ValueError("story_points_field must look like customfield_10016")
        return value


class JiraSyncState(BaseModel):
    overlap_seconds: int = Field(default=120, ge=0, le=3600)
    checkpoint: datetime | None = None
    migrating_legacy_comments: bool = False
    last_tested_at: datetime | None = None
    last_started_at: datetime | None = None
    last_completed_at: datetime | None = None
    last_error: str | None = None
    last_issue_count: int = Field(default=0, ge=0)


class JiraDataSource(BaseModel):
    id: str
    type: Literal["jira_cloud"] = "jira_cloud"
    enabled: bool = True
    display_name: str = Field(min_length=1, max_length=100)
    connection: JiraConnection
    scope: JiraScope
    sync: JiraSyncState = Field(default_factory=JiraSyncState)

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

class JiraSourceFile(BaseModel):
    sources: list[JiraDataSource] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_source_ids(self) -> "JiraSourceFile":
        ids = [source.id for source in self.sources]
        if len(ids) != len(set(ids)):
            raise ValueError("data-source ids must be unique")
        return self
