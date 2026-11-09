"""Zendesk Support REST client used by connection tests and sync."""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable
from urllib.parse import quote

import httpx

from .config import ZendeskDataSource

logger = logging.getLogger(__name__)

COMMENT_FETCH_WORKERS = 16
_INTEGRATION_ID_KEYS = (
    "external_id",
    "integration_external_id",
    "jira_external_id",
)


class ZendeskAPIError(RuntimeError):
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code


def _response_body(response: httpx.Response) -> str:
    text = getattr(response, "text", None)
    if isinstance(text, str):
        return text
    try:
        payload = response.json()
    except Exception:
        return ""
    return str(payload)


def _error_titles(errors: Any) -> list[str]:
    if isinstance(errors, dict):
        errors = [errors]
    if not isinstance(errors, list):
        return []
    titles: list[str] = []
    for item in errors:
        if isinstance(item, dict):
            title = item.get("title") or item.get("detail") or item.get("message")
            if title:
                titles.append(str(title))
        elif item:
            titles.append(str(item))
    return titles


def _integration_links_path(external_id: str) -> str:
    return f"/api/v2/integrations/jira/{quote(external_id, safe='')}/links"


def _link_rows(payload: Any) -> list[dict[str, Any]] | None:
    if not isinstance(payload, dict):
        return None
    links = payload.get("links")
    if not isinstance(links, list) or not links:
        return None
    rows = [item for item in links if isinstance(item, dict)]
    if rows and any("issue_key" in row or "ticket_id" in row for row in rows):
        return rows
    return None


def _integration_ids(payload: Any, *, jira_only: bool) -> list[str]:
    if not isinstance(payload, dict):
        return []
    items: list[dict[str, Any]] = []
    for value in payload.values():
        if isinstance(value, list):
            items.extend(item for item in value if isinstance(item, dict))
        elif isinstance(value, dict):
            items.append(value)
    found: list[str] = []
    for item in items:
        if jira_only and "jira" not in _item_text(item):
            continue
        found.extend(_id_values(item))
        settings = item.get("settings")
        if isinstance(settings, dict):
            found.extend(_id_values(settings))
    return found


def _item_text(item: dict[str, Any]) -> str:
    parts = [
        str(item.get(key) or "")
        for key in ("name", "app_name", "title", "type", "product")
    ]
    settings = item.get("settings")
    if isinstance(settings, dict):
        parts.extend(str(value or "") for value in settings.values() if not isinstance(value, (dict, list)))
    return " ".join(parts).lower()


def _id_values(item: dict[str, Any]) -> list[str]:
    found: list[str] = []
    for key in _INTEGRATION_ID_KEYS:
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            found.append(value.strip())
    return found


def ticket_search_query(source: ZendeskDataSource) -> str:
    """Build the ticket-count query: type:ticket plus optional filters."""
    parts = ["type:ticket"]
    updated_after = source.scope.updated_after
    if updated_after is not None:
        parts.append(f"updated>={updated_after.date().isoformat()}")
    statuses = list(source.scope.statuses)
    if len(statuses) == 1:
        parts.append(f"status:{statuses[0]}")
    elif statuses:
        parts.append(
            "(" + " OR ".join(f"status:{status}" for status in statuses) + ")"
        )
    return " ".join(parts)


def _latest_ticket_time(tickets: list[dict[str, Any]]) -> int | None:
    latest: int | None = None
    for ticket in tickets:
        raw = ticket.get("generated_timestamp")
        if raw is None:
            continue
        try:
            stamp = int(raw)
        except (TypeError, ValueError):
            continue
        if latest is None or stamp > latest:
            latest = stamp
    return latest


def export_start_time(source: ZendeskDataSource) -> int:
    """Checkpoint minus overlap, else updated_after, else the start of time."""
    if source.sync.checkpoint is not None:
        start = source.sync.checkpoint - timedelta(
            seconds=source.sync.overlap_seconds
        )
        return max(1, int(start.timestamp()))
    updated_after = source.scope.updated_after
    if updated_after is not None:
        if updated_after.tzinfo is None:
            updated_after = updated_after.replace(tzinfo=timezone.utc)
        return max(1, int(updated_after.timestamp()))
    return 1


class ZendeskClient:
    def __init__(
        self,
        source: ZendeskDataSource,
        *,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        max_attempts: int = 4,
    ):
        self.source = source
        self._sleep = sleep
        self._max_attempts = max_attempts
        self._owns_client = client is None
        subdomain = source.connection.subdomain
        self._client = client or httpx.Client(
            base_url=f"https://{subdomain}.zendesk.com",
            auth=(
                f"{source.connection.email}/token",
                source.connection.api_token,
            ),
            headers={"Accept": "application/json"},
            timeout=httpx.Timeout(30.0, connect=10.0),
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "ZendeskClient":
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
                        max(
                            (retry_at - datetime.now(timezone.utc)).total_seconds(),
                            0,
                        ),
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
            return f"Zendesk returned HTTP {response.status_code}"
        if isinstance(payload, dict):
            description = payload.get("description") or payload.get("error")
            if description:
                return str(description)
            titles = _error_titles(payload.get("errors"))
            if titles:
                return "; ".join(titles)
        return f"Zendesk returned HTTP {response.status_code}"

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
                message = self._error_message(response)
                if "jira" in path.lower():
                    logger.warning(
                        "Zendesk Jira request failed %s %s -> %s %s",
                        method,
                        path.split("?", 1)[0],
                        response.status_code,
                        _response_body(response)[:500],
                    )
                raise ZendeskAPIError(response.status_code, f"{path.split('?', 1)[0]}: {message}")
            if response.status_code == 204:
                return None
            try:
                return response.json()
            except ValueError as exc:
                raise ZendeskAPIError(
                    response.status_code,
                    "Zendesk returned an invalid JSON response",
                ) from exc
        raise ZendeskAPIError(503, f"Unable to reach Zendesk: {last_error}") from last_error

    def me(self) -> dict[str, Any]:
        payload = self._request("GET", "/api/v2/users/me.json")
        if isinstance(payload, dict) and isinstance(payload.get("user"), dict):
            return payload["user"]
        return payload if isinstance(payload, dict) else {}

    def count_tickets(self) -> int:
        payload = self._request(
            "GET",
            "/api/v2/search/count.json",
            params={"query": ticket_search_query(self.source)},
        )
        return int((payload or {}).get("count") or 0)

    def iter_ticket_pages(
        self,
    ) -> Iterator[tuple[list[dict[str, Any]], int | None, bool, str | None]]:
        """Yield cursor pages as (tickets, end_time, end_of_stream, after_cursor).

        Cursor exports resume from after_cursor. end_time is only present on
        the older time-based export.
        """
        path = "/api/v2/incremental/tickets/cursor.json"
        params: dict[str, Any] | None = {
            "start_time": export_start_time(self.source)
        }
        while True:
            payload = self._request("GET", path, params=params) or {}
            tickets = list(payload.get("tickets") or [])
            end_of_stream = bool(payload.get("end_of_stream"))
            after_cursor = payload.get("after_cursor") or None
            if after_cursor is not None:
                after_cursor = str(after_cursor)
            end_time = payload.get("end_time")
            if end_time is None:
                end_time = _latest_ticket_time(tickets)
            if end_time is None and not after_cursor and not end_of_stream:
                raise ZendeskAPIError(
                    502,
                    "Zendesk incremental export did not return a cursor",
                )
            yield (
                tickets,
                int(end_time) if end_time is not None else None,
                end_of_stream,
                after_cursor,
            )
            if end_of_stream:
                return
            after_url = payload.get("after_url")
            if after_url:
                path = str(after_url)
                params = None
            elif after_cursor:
                path = "/api/v2/incremental/tickets/cursor.json"
                params = {"cursor": after_cursor}
            else:
                return

    def ticket(self, ticket_id: str) -> dict[str, Any]:
        payload = self._request("GET", f"/api/v2/tickets/{quote(str(ticket_id), safe='')}.json")
        if isinstance(payload, dict) and isinstance(payload.get("ticket"), dict):
            return payload["ticket"]
        return payload if isinstance(payload, dict) else {}

    def audits(self, ticket_id: str) -> list[dict[str, Any]]:
        path: str | None = (
            f"/api/v2/tickets/{quote(str(ticket_id), safe='')}/audits.json"
        )
        audits: list[dict[str, Any]] = []
        while path:
            payload = self._request("GET", path) or {}
            audits.extend(payload.get("audits") or [])
            path = payload.get("next_page") or None
        return audits

    def comments(self, ticket_id: str) -> list[dict[str, Any]]:
        path: str | None = (
            f"/api/v2/tickets/{quote(str(ticket_id), safe='')}/comments.json"
        )
        comments: list[dict[str, Any]] = []
        while path:
            payload = self._request("GET", path) or {}
            comments.extend(payload.get("comments") or [])
            path = payload.get("next_page") or None
        return comments

    def users(self, user_ids: list[str]) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []
        unique = list(dict.fromkeys(str(user_id) for user_id in user_ids if user_id))
        for start in range(0, len(unique), 100):
            batch = unique[start : start + 100]
            payload = self._request(
                "GET",
                "/api/v2/users/show_many.json",
                params={"ids": ",".join(batch)},
            ) or {}
            found.extend(payload.get("users") or [])
        return found

    def _show_many(self, path: str, ids: list[str], key: str) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []
        unique = list(dict.fromkeys(str(item_id) for item_id in ids if item_id))
        for start in range(0, len(unique), 100):
            batch = unique[start : start + 100]
            payload = self._request(
                "GET",
                path,
                params={"ids": ",".join(batch)},
            ) or {}
            found.extend(payload.get(key) or [])
        return found

    def organizations(self, organization_ids: list[str]) -> list[dict[str, Any]]:
        return self._show_many(
            "/api/v2/organizations/show_many.json",
            organization_ids,
            "organizations",
        )

    def groups(self, group_ids: list[str]) -> list[dict[str, Any]]:
        return self._show_many(
            "/api/v2/groups/show_many.json",
            group_ids,
            "groups",
        )

    def iter_jira_link_pages(self) -> Iterator[list[dict[str, Any]]]:
        """Yield link pages from the configured integration, or the account default.

        Accounts on the current Jira integration reject ``GET /api/v2/jira/links``.
        When that happens and no external id is configured, look up the
        integration id and read ``/api/v2/integrations/jira/{id}/links``.
        """
        external_id = (self.source.scope.jira_integration_external_id or "").strip()
        if external_id:
            yield from self._paged_links(_integration_links_path(external_id))
            return
        legacy_error: ZendeskAPIError | None = None
        for path in ("/api/v2/jira/links", "/api/services/jira/links"):
            try:
                yield from self._paged_links(path)
            except ZendeskAPIError as exc:
                legacy_error = exc
                if exc.status_code not in (400, 403, 404, 422):
                    raise
                continue
            return
        assert legacy_error is not None
        link_pages, integration_ids = self._discover_jira_links()
        for page in link_pages:
            yield page
        if link_pages:
            return
        failures: list[str] = []
        for integration_id in integration_ids:
            try:
                yield from self._paged_links(_integration_links_path(integration_id))
                return
            except ZendeskAPIError as exc:
                if exc.status_code not in (400, 403, 404, 422):
                    raise
                failures.append(str(exc))
        raise ZendeskAPIError(
            legacy_error.status_code,
            (
                f"{legacy_error}. "
                + ("; ".join(failures) + ". " if failures else "")
                + "In Zendesk Admin Center open Apps and integrations, "
                "Integrations, Jira, Edit, and paste that integration id."
            ),
        ) from legacy_error

    def _paged_links(self, path: str) -> Iterator[list[dict[str, Any]]]:
        last_error: ZendeskAPIError | None = None
        for params in (None, {"page[size]": 100}):
            try:
                payload = self._request("GET", path, params=params) or {}
            except ZendeskAPIError as exc:
                last_error = exc
                if exc.status_code not in (400, 403, 404, 422):
                    raise
                continue
            yield from self._continue_link_pages(path, payload)
            return
        assert last_error is not None
        raise last_error

    def _continue_link_pages(
        self,
        path: str,
        payload: dict[str, Any],
    ) -> Iterator[list[dict[str, Any]]]:
        while True:
            yield list(payload.get("links") or [])
            meta = payload.get("meta") or {}
            after = meta.get("after_cursor") or meta.get("after")
            next_page = payload.get("next_page")
            if meta.get("has_more") and after:
                after = str(after)
                if after.startswith(("http://", "https://")):
                    path = after
                    payload = self._request("GET", path) or {}
                    continue
                payload = self._request(
                    "GET",
                    path,
                    params={"page[after]": after},
                ) or {}
                continue
            if next_page:
                path = str(next_page)
                payload = self._request("GET", path) or {}
                continue
            return

    def _discover_jira_links(
        self,
    ) -> tuple[list[list[dict[str, Any]]], list[str]]:
        pages: list[list[dict[str, Any]]] = []
        found: list[str] = []
        for path, jira_only in (
            ("/api/v2/integrations/jira", False),
            ("/api/v2/integrations/jira.json", False),
            ("/api/v2/apps/installations.json", True),
        ):
            try:
                payload = self._request("GET", path) or {}
            except ZendeskAPIError as exc:
                logger.warning("Zendesk Jira discovery %s failed: %s", path, exc)
                continue
            rows = _link_rows(payload)
            if rows is not None:
                logger.warning(
                    "Zendesk Jira discovery %s returned %s links", path, len(rows)
                )
                pages.append(rows)
                return pages, []
            found.extend(_integration_ids(payload, jira_only=jira_only))
            if found:
                logger.warning("Zendesk Jira discovery %s ids=%s", path, found)
                break
        return pages, list(dict.fromkeys(found))
