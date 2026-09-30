"""Small, testable Jira Cloud REST API client."""

from __future__ import annotations

import concurrent.futures
import time
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable
from urllib.parse import quote

import httpx

from .config import JiraDataSource


ISSUE_FIELDS = [
    "summary",
    "status",
    "issuetype",
    "priority",
    "resolution",
    "project",
    "assignee",
    "reporter",
    "created",
    "updated",
    "duedate",
    "labels",
    "components",
    "fixVersions",
    "parent",
    "issuelinks",
    "comment",
    "description",
    "attachment",
]
STATUS_CATEGORY_JQL = {
    "new": "To Do",
    "indeterminate": "In Progress",
    "done": "Done",
}

# Number of threads used to complete comment pagination within a single page.
# Each thread issues its own HTTP request, so raising this reduces wall-clock
# time proportionally up to the Jira rate-limit ceiling (~100 req/min on free
# plans, higher on paid).  10 is a safe default that won't trigger throttling
# on typical Atlassian Cloud accounts.
COMMENT_FETCH_WORKERS = 10


class JiraAPIError(RuntimeError):
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code


class JiraCloudClient:
    def __init__(
        self,
        source: JiraDataSource,
        *,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        max_attempts: int = 4,
    ):
        self.source = source
        self._sleep = sleep
        self._max_attempts = max_attempts
        self._owns_client = client is None
        self._client = client or httpx.Client(
            base_url=source.connection.site_url,
            auth=(source.connection.email, source.connection.api_token),
            headers={"Accept": "application/json"},
            timeout=httpx.Timeout(30.0, connect=10.0),
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "JiraCloudClient":
        return self

    def __exit__(self, *_args) -> None:
        self.close()

    @staticmethod
    def _retry_after(response: httpx.Response, attempt: int) -> float:
        value = response.headers.get("Retry-After")
        if value:
            try:
                return min(max(float(value), 0.0), 60.0)
            except ValueError:
                try:
                    retry_at = parsedate_to_datetime(value)
                    if retry_at.tzinfo is None:
                        retry_at = retry_at.replace(tzinfo=timezone.utc)
                    return min(
                        max((retry_at - datetime.now(timezone.utc)).total_seconds(), 0),
                        60.0,
                    )
                except (TypeError, ValueError):
                    pass
        return min(2 ** attempt, 30)

    @staticmethod
    def _error_message(response: httpx.Response) -> str:
        try:
            payload = response.json()
        except ValueError:
            return f"Jira returned HTTP {response.status_code}"
        if isinstance(payload, dict):
            messages = payload.get("errorMessages")
            if isinstance(messages, list) and messages:
                return "; ".join(str(message) for message in messages)
            errors = payload.get("errors")
            if isinstance(errors, dict) and errors:
                return "; ".join(f"{key}: {value}" for key, value in errors.items())
            if payload.get("message"):
                return str(payload["message"])
        return f"Jira returned HTTP {response.status_code}"

    def _request(self, method: str, path: str, **kwargs) -> Any:
        last_error: Exception | None = None
        for attempt in range(self._max_attempts):
            try:
                response = self._client.request(method, path, **kwargs)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = exc
                if attempt + 1 == self._max_attempts:
                    break
                self._sleep(min(2 ** attempt, 30))
                continue

            if response.status_code == 429 or response.status_code >= 500:
                if attempt + 1 < self._max_attempts:
                    self._sleep(self._retry_after(response, attempt))
                    continue
            if response.is_error:
                raise JiraAPIError(
                    response.status_code,
                    self._error_message(response),
                )
            if response.status_code == 204:
                return None
            try:
                return response.json()
            except ValueError as exc:
                raise JiraAPIError(
                    response.status_code,
                    "Jira returned an invalid JSON response",
                ) from exc
        raise JiraAPIError(503, f"Unable to reach Jira: {last_error}") from last_error

    def myself(self) -> dict[str, Any]:
        return self._request("GET", "/rest/api/3/myself")

    def cloud_id(self) -> str:
        payload = self._request("GET", "/_edge/tenant_info")
        cloud_id = payload.get("cloudId") if isinstance(payload, dict) else None
        if not cloud_id:
            raise JiraAPIError(502, "Jira did not return a cloudId")
        return str(cloud_id)

    def projects(self) -> list[dict[str, Any]]:
        start_at = 0
        projects: list[dict[str, Any]] = []
        while True:
            payload = self._request(
                "GET",
                "/rest/api/3/project/search",
                params={"startAt": start_at, "maxResults": 100, "orderBy": "key"},
            )
            values = payload.get("values") or []
            projects.extend(values)
            if payload.get("isLast", True) or not values:
                break
            start_at += len(values)
        return projects

    def project(self, project_key: str) -> dict[str, Any]:
        """Return one project by key without enumerating every visible project."""
        return self._request(
            "GET",
            f"/rest/api/3/project/{quote(project_key, safe='')}",
        )

    def _jql(
        self,
        *,
        incremental: bool = True,
        include_order: bool = True,
    ) -> str:
        scope = self.source.scope
        projects = ", ".join(scope.project_keys)
        clauses = [f"project in ({projects})"]
        if scope.created_after:
            clauses.append(f'created >= "{scope.created_after.isoformat()}"')
        if scope.updated_after:
            clauses.append(f'updated >= "{scope.updated_after.isoformat()}"')
        if scope.status_categories:
            categories = ", ".join(
                f'"{STATUS_CATEGORY_JQL[category]}"'
                for category in scope.status_categories
            )
            clauses.append(f"statusCategory in ({categories})")
        if scope.jql_extra:
            clauses.append(f"({scope.jql_extra})")
        if incremental and self.source.sync.checkpoint:
            checkpoint = self.source.sync.checkpoint
            if checkpoint.tzinfo is None:
                checkpoint = checkpoint.replace(tzinfo=timezone.utc)
            checkpoint = checkpoint.astimezone(timezone.utc) - timedelta(
                seconds=self.source.sync.overlap_seconds
            )
            clauses.append(f'updated >= "{checkpoint:%Y-%m-%d %H:%M}"')
        jql = " AND ".join(clauses)
        return f"{jql} ORDER BY updated ASC, key ASC" if include_order else jql

    def approximate_issue_count(self) -> int:
        payload = self._request(
            "POST",
            "/rest/api/3/search/approximate-count",
            json={
                "jql": self._jql(
                    incremental=False,
                    include_order=False,
                )
            },
            headers={"Content-Type": "application/json"},
        )
        return int(payload.get("count") or 0)

    def _iter_search_pages(
        self,
        *,
        fields: list[str],
        incremental: bool,
    ) -> Iterator[list[dict[str, Any]]]:
        token: str | None = None
        jql = self._jql(incremental=incremental)
        while True:
            body: dict[str, Any] = {
                "jql": jql,
                "fields": fields,
                "fieldsByKeys": False,
                "maxResults": 100,
            }
            if token:
                body["nextPageToken"] = token
            payload = self._request(
                "POST",
                "/rest/api/3/search/jql",
                json=body,
                headers={"Content-Type": "application/json"},
            )
            issues = payload.get("issues") or []
            if issues:
                yield issues
            token = payload.get("nextPageToken")
            if not token:
                break

    def _iter_search(self, *, fields: list[str], incremental: bool):
        for issues in self._iter_search_pages(
            fields=fields,
            incremental=incremental,
        ):
            yield from issues

    def iter_issue_pages(self) -> Iterator[list[dict[str, Any]]]:
        """Yield complete issue pages for durable page-level synchronization.

        Comment completion is parallelised across issues within each page so
        that the extra Jira API calls needed for issues with >10 comments are
        issued concurrently rather than one at a time.  ``_complete_comments``
        returns immediately when an issue already carries all its comments, so
        it is safe to submit every issue to the pool.
        """
        fields = list(ISSUE_FIELDS)
        story_points = self.source.scope.story_points_field
        if story_points:
            fields.append(story_points)
        for issues in self._iter_search_pages(fields=fields, incremental=True):
            if self.source.scope.include_comments:
                # Use up to COMMENT_FETCH_WORKERS threads so that the extra
                # comment-pagination API calls for issues with >10 comments run
                # in parallel.  httpx.Client uses httpcore's connection pool
                # which is thread-safe for concurrent requests.
                workers = min(len(issues), COMMENT_FETCH_WORKERS)
                with concurrent.futures.ThreadPoolExecutor(
                    max_workers=workers, thread_name_prefix="jira-comment"
                ) as pool:
                    futures = [
                        pool.submit(self._complete_comments, issue)
                        for issue in issues
                    ]
                    # Wait for all and propagate any exception immediately.
                    for fut in concurrent.futures.as_completed(futures):
                        fut.result()
            yield issues

    def iter_issues(self) -> Iterator[dict[str, Any]]:
        for issues in self.iter_issue_pages():
            yield from issues

    def _complete_comments(self, issue: dict[str, Any]) -> None:
        fields = issue.setdefault("fields", {})
        page = fields.get("comment") or {}
        comments = list(page.get("comments") or [])
        total = int(page.get("total") or len(comments))
        if len(comments) >= total:
            page["comments"] = comments
            fields["comment"] = page
            return

        issue_id = issue.get("id") or issue.get("key")
        start_at = len(comments)
        while start_at < total:
            payload = self._request(
                "GET",
                f"/rest/api/3/issue/{issue_id}/comment",
                params={"startAt": start_at, "maxResults": 100, "orderBy": "created"},
            )
            batch = payload.get("comments") or []
            if not batch:
                break
            comments.extend(batch)
            start_at += len(batch)
            total = int(payload.get("total") or total)
        page["comments"] = comments
        page["total"] = total
        fields["comment"] = page
