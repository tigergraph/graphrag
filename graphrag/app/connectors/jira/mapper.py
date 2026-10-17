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
    JIRA_CHANGE_AUTHOR_EDGE,
    JIRA_CHANGE_EDGE,
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
class MappedFact:
    """One short searchable fact for an issue.

    entities are extra CONTAINS_ENTITY targets besides the issue itself.
    """

    chunk: MappedChunk
    entities: tuple[tuple[str, str], ...] = ()


@dataclass
class MappedIssue:
    issue_id: str
    issue_vertex_id: str
    updated: datetime | None
    content_hash: str
    vertices: list[VertexRecord]
    edges: list[EdgeRecord]
    facts: list[MappedFact]
    legacy_document_id: str
    change_vertex_ids: list[str]
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
                comment=comment,
                body=comment_body,
            )
            chunks = [
                self._lead_with_issue(issue_key, str(chunk))
                for chunk in self.comment_chunker.chunk(comment_text)
            ]
            chunks = [chunk for chunk in chunks if chunk]
            comment_hash = hashlib.sha256(
                (
                    "direct-comment-chunks\0"
                    + "\0".join(chunks)
                ).encode("utf-8")
            ).hexdigest()
            comment_attrs = {
                "comment_id": comment_id,
                "body": comment_body,
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

        change_vertices, change_edges, change_parts = self._map_changes(
            issue_id=issue_id,
            issue_key=issue_key,
            changelog=issue.get("changelog") or {},
        )
        for vertex in change_vertices:
            add_vertex(vertex)
        edges.extend(change_edges)
        facts = self._facts(
            issue_vertex_id,
            [
                (
                    self._record_text(
                        issue_key=issue_key,
                        issue_url=issue_url,
                        fields=fields,
                        project=project,
                    ),
                    (),
                ),
                *self._description_parts(issue_key, description),
                *change_parts,
            ],
        )
        content_hash = hashlib.sha256(
            "\0".join(fact.chunk.text for fact in facts).encode("utf-8")
        ).hexdigest()
        issue_vertex = vertices[("JiraIssue", issue_vertex_id)]
        issue_attributes = dict(issue_vertex.attributes)
        issue_attributes["content_hash"] = content_hash
        vertices[("JiraIssue", issue_vertex_id)] = VertexRecord(
            "JiraIssue",
            issue_vertex_id,
            issue_attributes,
        )

        return MappedIssue(
            issue_id=issue_id,
            issue_vertex_id=issue_vertex_id,
            updated=_parsed_datetime(fields.get("updated")),
            content_hash=content_hash,
            vertices=list(vertices.values()),
            edges=edges,
            facts=facts,
            legacy_document_id=self._id("issue-doc", issue_id),
            change_vertex_ids=[vertex.vertex_id for vertex in change_vertices],
            comments=mapped_comments,
        )

    def _comment_document_text(
        self,
        *,
        issue_key: str,
        comment: dict[str, Any],
        body: str,
    ) -> str:
        author = _display_name(comment.get("author")) or "Unknown user"
        created = _datetime(comment.get("created")) or ""
        when = f" at {created}" if created else ""
        return (
            f"Issue: {issue_key}\n"
            f"Comment by {author}{when}:\n"
            f"{body.strip()}"
        )

    def _lead_with_issue(self, issue_key: str, text: str) -> str:
        cleaned = text.strip()
        prefix = f"Issue: {issue_key}"
        if not cleaned or cleaned.startswith(prefix):
            return cleaned
        return f"{prefix}\n{cleaned}"

    def _record_text(
        self,
        *,
        issue_key: str,
        issue_url: str,
        fields: dict[str, Any],
        project: dict[str, Any],
    ) -> str:
        status = fields.get("status") or {}
        status_category = status.get("statusCategory") or {}
        issue_type = fields.get("issuetype") or {}
        priority = fields.get("priority") or {}
        resolution = fields.get("resolution") or {}
        project_line = (
            f"{project.get('key') or ''} {project.get('name') or ''}".strip()
        )
        status_name = str(status.get("name") or "")
        category = str(status_category.get("key") or "")
        status_line = (
            f"{status_name} ({category})".strip()
            if category
            else status_name
        )
        lines = [f"Issue: {issue_key}"]
        for label, value in (
            ("Summary", fields.get("summary")),
            ("URL", issue_url),
            ("Project", project_line),
            ("Type", issue_type.get("name")),
            ("Status", status_line),
            ("Priority", priority.get("name")),
            ("Resolution", resolution.get("name")),
            ("Assignee", _display_name(fields.get("assignee")) or "Unassigned"),
            ("Reporter", _display_name(fields.get("reporter"))),
            ("Labels", _comma_names(fields.get("labels"))),
            ("Components", _comma_names(fields.get("components"))),
            ("Fix versions", _comma_names(fields.get("fixVersions"))),
            ("Due", _datetime(fields.get("duedate"))),
            ("Updated", _datetime(fields.get("updated"))),
        ):
            text = str(value or "").strip()
            if text:
                lines.append(f"{label}: {text}")
        return "\n".join(lines)

    def _description_parts(
        self,
        issue_key: str,
        description: str,
    ) -> list[tuple[str, tuple[tuple[str, str], ...]]]:
        if not description or not description.strip():
            return []
        source_text = f"Issue: {issue_key}\nDescription:\n{description.strip()}"
        return [
            (self._lead_with_issue(issue_key, str(piece)), ())
            for piece in self.comment_chunker.chunk(source_text)
            if str(piece).strip()
        ]

    _CHANGELOG_FIELDS = {"status", "assignee", "priority", "resolution"}

    def _map_changes(
        self,
        *,
        issue_id: str,
        issue_key: str,
        changelog: dict[str, Any],
    ) -> tuple[
        list[VertexRecord],
        list[EdgeRecord],
        list[tuple[str, tuple[tuple[str, str], ...]]],
    ]:
        """One JiraChange event and one fact per tracked changelog item."""
        histories = sorted(
            changelog.get("histories") or [],
            key=lambda history: history.get("created") or "",
        )
        vertices: list[VertexRecord] = []
        edges: list[EdgeRecord] = []
        parts: list[tuple[str, tuple[tuple[str, str], ...]]] = []
        change_index = 0
        issue_vertex_id = self._issue_id(issue_key)
        for history in histories:
            author = history.get("author") or {}
            author_name = _display_name(author) or "Unknown"
            created = _datetime(history.get("created"))
            history_id = str(history.get("id") or "")
            for item in history.get("items") or []:
                field = str(item.get("field") or "").lower()
                if field not in self._CHANGELOG_FIELDS:
                    continue
                from_val = str(item.get("fromString") or "").strip()
                to_val = str(item.get("toString") or "").strip()
                if not to_val:
                    continue
                change_index += 1
                change_key = (
                    f"{issue_id}:{history_id or change_index}:"
                    f"{field}:{change_index}"
                )
                change_vertex_id = self._id("change", change_key)
                attributes = {
                    "change_id": change_key,
                    "field": field,
                    "from_value": from_val,
                    "to_value": to_val,
                    "created": created,
                    "ontology_class": "Event",
                }
                vertices.append(
                    VertexRecord(
                        "JiraChange",
                        change_vertex_id,
                        {
                            key: value
                            for key, value in attributes.items()
                            if value not in (None, "")
                        },
                    )
                )
                edges.append(
                    EdgeRecord(
                        "JiraIssue",
                        issue_vertex_id,
                        JIRA_CHANGE_EDGE,
                        "JiraChange",
                        change_vertex_id,
                    )
                )
                author_vertex = self._user_vertex(author)
                if author_vertex:
                    vertices.append(author_vertex)
                    edges.append(
                        EdgeRecord(
                            "JiraChange",
                            change_vertex_id,
                            JIRA_CHANGE_AUTHOR_EDGE,
                            "JiraUser",
                            author_vertex.vertex_id,
                        )
                    )
                when = f"{created}: " if created else ""
                if from_val:
                    sentence = (
                        f"{when}{author_name} changed {field} from "
                        f"\"{from_val}\" to \"{to_val}\""
                    )
                else:
                    sentence = (
                        f"{when}{author_name} set {field} to \"{to_val}\""
                    )
                parts.append(
                    (
                        f"Issue: {issue_key}\n{sentence}",
                        (("JiraChange", change_vertex_id),),
                    )
                )
        return vertices, edges, parts

    def _facts(
        self,
        issue_vertex_id: str,
        parts: list[tuple[str, tuple[tuple[str, str], ...]]],
    ) -> list[MappedFact]:
        facts: list[MappedFact] = []
        for text, entities in parts:
            cleaned = text.strip()
            if not cleaned:
                continue
            digest = hashlib.sha256(cleaned.encode("utf-8")).hexdigest()[:12]
            index = len(facts)
            facts.append(
                MappedFact(
                    chunk=MappedChunk(
                        chunk_id=f"{issue_vertex_id}:fact:{index}:{digest}",
                        index=index,
                        text=cleaned + "\n",
                    ),
                    entities=entities,
                )
            )
        return facts
