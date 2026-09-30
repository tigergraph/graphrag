"""Deterministic Jira payload to GraphRAG mapping."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from dateutil import parser as date_parser

from common.chunkers.structured import StructuredChunker
from common.config import get_graphrag_config

from .adf import adf_to_markdown
from .config import JiraDataSource
from .schema import (
    JIRA_ASSIGNEE_EDGE,
    JIRA_COMMENT_AFTER_EDGE,
    JIRA_COMMENT_AUTHOR_EDGE,
    JIRA_COMMENT_ISSUE_EDGE,
    JIRA_COMMENT_REPLY_EDGE,
    JIRA_LINK_EDGE,
    JIRA_PARENT_EDGE,
    JIRA_PROJECT_EDGE,
    JIRA_REPORTER_EDGE,
)


@dataclass(frozen=True)
class VertexRecord:
    vertex_type: str
    vertex_id: str
    attributes: dict[str, Any]


@dataclass(frozen=True)
class EdgeRecord:
    source_type: str
    source_id: str
    edge_type: str
    target_type: str
    target_id: str
    attributes: dict[str, Any] = field(default_factory=dict)


@dataclass
class MappedChunk:
    chunk_id: str
    index: int
    text: str


@dataclass
class MappedComment:
    comment_id: str
    comment_vertex_id: str
    issue_vertex_id: str
    content_hash: str
    chunks: list[MappedChunk]


@dataclass
class MappedIssue:
    issue_id: str
    issue_vertex_id: str
    updated: datetime | None
    content_hash: str
    vertices: list[VertexRecord]
    edges: list[EdgeRecord]
    document: dict[str, Any]
    comments: list[MappedComment]


def _datetime(value: Any) -> str | None:
    if not value:
        return None
    try:
        return date_parser.parse(str(value)).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OverflowError):
        return None


def _parsed_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return date_parser.parse(str(value))
    except (TypeError, ValueError, OverflowError):
        return None


def _timestamp(value: Any) -> float:
    parsed = _parsed_datetime(value)
    return parsed.timestamp() if parsed is not None else 0.0


def _display_name(user: dict[str, Any] | None) -> str:
    if not user:
        return ""
    return str(user.get("displayName") or user.get("accountId") or "").strip()


def _comma_names(values: list[Any] | None) -> str:
    names: list[str] = []
    for value in values or []:
        if isinstance(value, dict):
            name = value.get("name")
        else:
            name = value
        if name is not None and str(name).strip():
            names.append(str(name).strip())
    return ", ".join(names)


_FENCED_BLOCK_RE = re.compile(r"```[^\n]*\n(.*?)```", re.DOTALL)
_LOG_LINE_RE = re.compile(
    r"""(?ix)
    ^\s*(?:
        \[?\d{4}[-/]\d{2}[-/]\d{2}[T\s]\d{2}:\d{2}:\d{2}
        |\[?(?:TRACE|DEBUG|INFO|WARN(?:ING)?|ERROR|FATAL|CRITICAL)\]?\b
        |(?:Traceback\s+\(most\s+recent\s+call\s+last\)|Caused\s+by:)
        |(?:at\s+[\w.$]+\([^)]*(?::\d+)?\))
        |(?:File\s+"[^"]+",\s+line\s+\d+)
        |\{.*"(?:timestamp|time|level|severity|logger)"\s*:
    )
    """
)
_LOG_OMISSION = "[Log output omitted from search content.]"


def _is_log_heavy(lines: list[str]) -> bool:
    non_empty = [line for line in lines if line.strip()]
    if len(non_empty) < 5:
        return False
    matched = sum(bool(_LOG_LINE_RE.search(line)) for line in non_empty)
    return matched >= 3 and matched / len(non_empty) >= 0.6


def _filter_long_log_output(text: str, chunk_size: int) -> str:
    """Remove log-dominated blocks only when a comment needs chunking."""
    if len(text) <= chunk_size:
        return text

    def replace_fence(match: re.Match[str]) -> str:
        body = match.group(1)
        return _LOG_OMISSION if _is_log_heavy(body.splitlines()) else match.group(0)

    filtered = _FENCED_BLOCK_RE.sub(replace_fence, text)
    lines = filtered.splitlines()
    output: list[str] = []
    index = 0
    while index < len(lines):
        end = index
        while end < len(lines) and (
            not lines[end].strip() or _LOG_LINE_RE.search(lines[end])
        ):
            end += 1
        block = lines[index:end]
        if _is_log_heavy(block):
            if not output or output[-1] != _LOG_OMISSION:
                output.append(_LOG_OMISSION)
            index = end
            continue
        output.append(lines[index])
        index += 1
    return "\n".join(output).strip()


class JiraIssueMapper:
    def __init__(
        self,
        source: JiraDataSource,
        graphname: str | None = None,
    ):
        self.source = source
        chunker_config = get_graphrag_config(graphname).get(
            "chunker_config",
            {},
        )
        self.comment_chunker = StructuredChunker(
            chunk_size=chunker_config.get("chunk_size", 0),
            overlap_size=chunker_config.get("overlap_size", -1),
        )

    def _id(self, object_type: str, object_id: Any) -> str:
        # cloud_id intentionally excluded — vertex IDs use only the portable
        # object_type + object_id so they are stable across graph recreations
        # and don't leak internal tenant identifiers.
        return f"jira:{object_type}:{object_id}".lower()

    def _issue_id(self, issue_key: str) -> str:
        # Use the human-readable ticket key so GenerateFunction can construct
        # the vertex ID directly from what the user says (e.g. "GML-2191"
        # → "jira:gml-2191:issue").  cloud_id is NOT included — ticket keys
        # are unique within a graph's connected Jira project scope.
        return f"jira:{issue_key}:issue".lower()

    def _user_vertex(
        self, user: dict[str, Any] | None
    ) -> VertexRecord | None:
        if not user or not user.get("accountId"):
            return None
        account_id = str(user["accountId"])
        return VertexRecord(
            "JiraUser",
            self._id("user", account_id),
            {
                "account_id": account_id,
                "display_name": _display_name(user),
            },
        )

    def map(self, issue: dict[str, Any]) -> MappedIssue:
        issue_id = str(issue["id"])
        issue_key = str(issue.get("key") or issue_id)
        fields = issue.get("fields") or {}
        project = fields.get("project") or {}
        project_id = str(project.get("id") or project.get("key") or "unknown")
        issue_vertex_id = self._issue_id(issue_key)
        project_vertex_id = self._id("project", project_id)
        site_url = self.source.connection.site_url
        issue_url = f"{site_url}/browse/{issue_key}"

        description = adf_to_markdown(fields.get("description"))
        document_text = self._document_text(
            issue_key=issue_key,
            issue_url=issue_url,
            fields=fields,
            project=project,
            description=description,
        )
        content_hash = hashlib.sha256(document_text.encode("utf-8")).hexdigest()

        status = fields.get("status") or {}
        status_category = status.get("statusCategory") or {}
        priority = fields.get("priority") or {}
        resolution = fields.get("resolution") or {}
        issue_type = fields.get("issuetype") or {}
        story_points_field = self.source.scope.story_points_field
        story_points = fields.get(story_points_field) if story_points_field else None

        issue_attrs = {
            "issue_key": issue_key,
            "summary": str(fields.get("summary") or ""),
            "issue_type": str(issue_type.get("name") or ""),
            "status": str(status.get("name") or ""),
            "status_category": str(status_category.get("key") or ""),
            "priority": str(priority.get("name") or ""),
            "resolution": str(resolution.get("name") or ""),
            "labels": _comma_names(fields.get("labels")),
            "components": _comma_names(fields.get("components")),
            "fix_versions": _comma_names(fields.get("fixVersions")),
            "created": _datetime(fields.get("created")),
            "updated": _datetime(fields.get("updated")),
            "due": _datetime(fields.get("duedate")),
            "url": issue_url,
            "content_hash": content_hash,
        }
        if story_points is not None:
            try:
                issue_attrs["story_points"] = float(story_points)
            except (TypeError, ValueError):
                pass
        issue_attrs = {
            key: value for key, value in issue_attrs.items() if value not in (None, "")
        }

        vertices: dict[tuple[str, str], VertexRecord] = {}

        def add_vertex(vertex: VertexRecord | None) -> None:
            if not vertex:
                return
            key = (vertex.vertex_type, vertex.vertex_id)
            existing = vertices.get(key)
            if existing and existing.attributes.keys() - vertex.attributes.keys():
                return
            vertices[key] = vertex

        add_vertex(
            VertexRecord(
                "JiraProject",
                project_vertex_id,
                {
                    "project_key": str(project.get("key") or ""),
                    "name": str(project.get("name") or ""),
                    "url": (
                        f"{site_url}/jira/software/projects/{project.get('key')}"
                        if project.get("key")
                        else site_url
                    ),
                },
            )
        )
        add_vertex(VertexRecord("JiraIssue", issue_vertex_id, issue_attrs))

        edges = [
            EdgeRecord(
                "JiraIssue",
                issue_vertex_id,
                JIRA_PROJECT_EDGE,
                "JiraProject",
                project_vertex_id,
            )
        ]

        for field_name, edge_type in (
            ("assignee", JIRA_ASSIGNEE_EDGE),
            ("reporter", JIRA_REPORTER_EDGE),
        ):
            user_vertex = self._user_vertex(fields.get(field_name))
            add_vertex(user_vertex)
            if user_vertex:
                edges.append(
                    EdgeRecord(
                        "JiraIssue",
                        issue_vertex_id,
                        edge_type,
                        "JiraUser",
                        user_vertex.vertex_id,
                    )
                )

        mapped_comments: list[MappedComment] = []
        previous_comment_vertex_id: str | None = None
        comments = (
            (fields.get("comment") or {}).get("comments") or []
            if self.source.scope.include_comments
            else []
        )
        comments = sorted(
            comments,
            key=lambda comment: (
                _timestamp(comment.get("created")),
                str(comment.get("id") or ""),
            ),
        )
        for comment in comments:
            raw_comment_id = comment.get("id")
            if raw_comment_id is None:
                continue

            # Skip bot/automation comments — accountType "app" means a Jira
            # automation rule, CI integration, or service-account bot. These
            # produce high-volume noise (build status, deploy notifications,
            # auto-transitions) with no useful search content.
            author = comment.get("author") or {}
            if author.get("accountType") == "app":
                continue

            # Skip empty comments — nothing meaningful to store or search.
            comment_body = adf_to_markdown(comment.get("body"))
            if not comment_body or not comment_body.strip():
                continue

            comment_id = str(raw_comment_id)
            comment_vertex_id = self._id("comment", comment_id)
            author_vertex = self._user_vertex(author)
            add_vertex(author_vertex)
            visibility = comment.get("visibility")
            if not isinstance(visibility, dict):
                visibility = {}
            visibility_text = ":".join(
                str(value)
                for value in (
                    visibility.get("type"),
                    visibility.get("value"),
                )
                if value
            )
            comment_body = _filter_long_log_output(
                comment_body,
                self.comment_chunker.chunk_size,
            )
            comment_text = self._comment_document_text(
                issue_key=issue_key,
                issue_url=issue_url,
                issue_summary=str(fields.get("summary") or ""),
                comment=comment,
                visibility=visibility_text,
                body=comment_body,
            )
            chunks = [str(chunk).strip() for chunk in self.comment_chunker.chunk(comment_text)]
            chunks = [chunk for chunk in chunks if chunk]
            comment_hash = hashlib.sha256(
                (
                    "direct-comment-chunks\0"
                    + "\0".join(chunks)
                ).encode("utf-8")
            ).hexdigest()
            comment_attrs = {
                "comment_id": comment_id,
                "created": _datetime(comment.get("created")),
                "updated": _datetime(comment.get("updated")),
                "visibility": visibility_text,
                "is_public": comment.get("jsdPublic"),
                "ontology_class": "Event",
                "content_hash": comment_hash,
            }
            add_vertex(
                VertexRecord(
                    "JiraComment",
                    comment_vertex_id,
                    {
                        key: value
                        for key, value in comment_attrs.items()
                        if value not in (None, "")
                    },
                )
            )
            edges.append(
                EdgeRecord(
                    "JiraComment",
                    comment_vertex_id,
                    JIRA_COMMENT_ISSUE_EDGE,
                    "JiraIssue",
                    issue_vertex_id,
                )
            )
            if author_vertex:
                edges.append(
                    EdgeRecord(
                        "JiraComment",
                        comment_vertex_id,
                        JIRA_COMMENT_AUTHOR_EDGE,
                        "JiraUser",
                        author_vertex.vertex_id,
                    )
                )
            parent_comment = comment.get("parent")
            if not isinstance(parent_comment, dict):
                parent_comment = {}
            parent_comment_id = comment.get("parentId") or parent_comment.get("id")
            if parent_comment_id:
                parent_comment_vertex_id = self._id(
                    "comment",
                    parent_comment_id,
                )
                add_vertex(
                    VertexRecord(
                        "JiraComment",
                        parent_comment_vertex_id,
                        {
                            "comment_id": str(parent_comment_id),
                            "ontology_class": "Event",
                        },
                    )
                )
                edges.append(
                    EdgeRecord(
                        "JiraComment",
                        comment_vertex_id,
                        JIRA_COMMENT_REPLY_EDGE,
                        "JiraComment",
                        parent_comment_vertex_id,
                    )
                )
            if previous_comment_vertex_id:
                edges.append(
                    EdgeRecord(
                        "JiraComment",
                        comment_vertex_id,
                        JIRA_COMMENT_AFTER_EDGE,
                        "JiraComment",
                        previous_comment_vertex_id,
                    )
                )
            mapped_comments.append(
                MappedComment(
                    comment_id=comment_id,
                    comment_vertex_id=comment_vertex_id,
                    issue_vertex_id=issue_vertex_id,
                    content_hash=comment_hash,
                    chunks=[
                        MappedChunk(
                            chunk_id=(
                                f"{comment_vertex_id}:chunk:{index}:"
                                f"{hashlib.sha256(text.encode('utf-8')).hexdigest()[:12]}"
                            ),
                            index=index,
                            text=text,
                        )
                        for index, text in enumerate(chunks)
                    ],
                )
            )
            previous_comment_vertex_id = comment_vertex_id

        parent = fields.get("parent") or {}
        if parent.get("id"):
            parent_key = str(parent.get("key") or parent["id"])
            parent_id = self._issue_id(parent_key)
            parent_fields = parent.get("fields") or {}
            add_vertex(
                VertexRecord(
                    "JiraIssue",
                    parent_id,
                    {
                        "issue_key": str(parent.get("key") or ""),
                        "summary": str(parent_fields.get("summary") or ""),
                        "url": (
                            f"{site_url}/browse/{parent.get('key')}"
                            if parent.get("key")
                            else site_url
                        ),
                    },
                )
            )
            edges.append(
                EdgeRecord(
                    "JiraIssue",
                    issue_vertex_id,
                    JIRA_PARENT_EDGE,
                    "JiraIssue",
                    parent_id,
                )
            )

        for link in fields.get("issuelinks") or []:
            link_type = link.get("type") or {}
            target = link.get("outwardIssue")
            relation = link_type.get("outward")
            if not target:
                target = link.get("inwardIssue")
                relation = link_type.get("inward")
            if not target or not target.get("id"):
                continue
            target_key = str(target.get("key") or target["id"])
            target_id = self._issue_id(target_key)
            target_fields = target.get("fields") or {}
            add_vertex(
                VertexRecord(
                    "JiraIssue",
                    target_id,
                    {
                        "issue_key": target_key,
                        "summary": str(target_fields.get("summary") or ""),
                        "url": f"{site_url}/browse/{target_key}" if target_key else site_url,
                    },
                )
            )
            edges.append(
                EdgeRecord(
                    "JiraIssue",
                    issue_vertex_id,
                    JIRA_LINK_EDGE,
                    "JiraIssue",
                    target_id,
                    {"link_type": str(relation or link_type.get("name") or "relates to")},
                )
            )

        return MappedIssue(
            issue_id=issue_id,
            issue_vertex_id=issue_vertex_id,
            updated=_parsed_datetime(fields.get("updated")),
            content_hash=content_hash,
            vertices=list(vertices.values()),
            edges=edges,
            document={
                "doc_id": self._id("issue-doc", issue_id),
                "doc_type": "jira",
                "content": document_text,
                "position": 0,
            },
            comments=mapped_comments,
        )

    def _comment_document_text(
        self,
        *,
        issue_key: str,
        issue_url: str,
        issue_summary: str,
        comment: dict[str, Any],
        visibility: str,
        body: str,
    ) -> str:
        author = _display_name(comment.get("author")) or "Unknown user"
        return "\n".join(
            [
                f"# Comment by {author} on {issue_key}: {issue_summary}",
                f"Issue: {issue_key}",
                f"URL: {issue_url}",
                f"Comment ID: {comment.get('id') or ''}",
                f"Author: {author}",
                f"Created: {comment.get('created') or ''}",
                f"Updated: {comment.get('updated') or ''}",
                f"Visibility: {visibility or 'default'}",
                "",
                "## Comment",
                body,
            ]
        ).strip() + "\n"

    def _document_text(
        self,
        *,
        issue_key: str,
        issue_url: str,
        fields: dict[str, Any],
        project: dict[str, Any],
        description: str,
    ) -> str:
        status = fields.get("status") or {}
        status_category = status.get("statusCategory") or {}
        issue_type = fields.get("issuetype") or {}
        priority = fields.get("priority") or {}
        lines = [
            f"# {issue_key}: {fields.get('summary') or ''}",
            f"URL: {issue_url}",
            f"Project: {project.get('key') or ''} {project.get('name') or ''}".rstrip(),
            f"Type: {issue_type.get('name') or ''}",
            (
                f"Status: {status.get('name') or ''} "
                f"({status_category.get('key') or ''})"
            ).rstrip(),
            f"Priority: {priority.get('name') or ''}",
            f"Assignee: {_display_name(fields.get('assignee')) or 'Unassigned'}",
            f"Reporter: {_display_name(fields.get('reporter'))}",
            f"Labels: {_comma_names(fields.get('labels'))}",
            f"Components: {_comma_names(fields.get('components'))}",
            f"Fix versions: {_comma_names(fields.get('fixVersions'))}",
            f"Updated: {fields.get('updated') or ''}",
            "",
            "## Description",
            description or "(No description)",
        ]
        attachments = fields.get("attachment") or []
        if attachments:
            lines.extend(
                [
                    "",
                    "## Attachments",
                    ", ".join(
                        str(item.get("filename"))
                        for item in attachments
                        if item.get("filename")
                    ),
                ]
            )
        return "\n".join(lines).strip() + "\n"
