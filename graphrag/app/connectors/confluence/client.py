"""Confluence Cloud REST client used by the connection test and sync."""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable
from urllib.parse import quote

import httpx

from .config import ConfluenceDataSource

logger = logging.getLogger(__name__)


class ConfluenceAPIError(RuntimeError):
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code


def wiki_base_url(site: str) -> str:
    return f"https://{site}/wiki"


def content_cql(source: ConfluenceDataSource, since: datetime | None) -> str:
    """CQL for this run: pages and blog posts, optional spaces, lastModified order."""
    types = ", ".join(source.scope.content_types)
    clauses = [f"type in ({types})"]
    if source.scope.space_keys:
        keys = ", ".join(f'"{key}"' for key in source.scope.space_keys)
        clauses.append(f"space in ({keys})")
    if since is not None:
        stamp = since.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M")
        clauses.append(f'lastModified >= "{stamp}"')
    return " AND ".join(clauses) + " ORDER BY lastModified ASC"


def search_since(source: ConfluenceDataSource) -> datetime | None:
    """Checkpoint minus overlap, else updated_after."""
    if source.sync.checkpoint is not None:
        return source.sync.checkpoint - timedelta(seconds=source.sync.overlap_seconds)
    return source.scope.updated_after


class ConfluenceClient:
    def __init__(
        self,
        source: ConfluenceDataSource,
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
            base_url=wiki_base_url(source.connection.site),
            auth=(source.connection.email, source.connection.api_token),
            headers={"Accept": "application/json"},
            timeout=httpx.Timeout(30.0, connect=10.0),
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "ConfluenceClient":
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
            return f"Confluence returned HTTP {response.status_code}"
        if isinstance(payload, dict):
            message = payload.get("message") or payload.get("title")
            if message:
                return str(message)
        return f"Confluence returned HTTP {response.status_code}"

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
                raise ConfluenceAPIError(
                    response.status_code,
                    self._error_message(response),
                )
            if response.status_code == 204:
                return None
            try:
                return response.json()
            except ValueError as exc:
                raise ConfluenceAPIError(
                    response.status_code,
                    "Confluence returned an invalid JSON response",
                ) from exc
        raise ConfluenceAPIError(
            503, f"Unable to reach Confluence: {last_error}"
        ) from last_error

    def _relative(self, link: str) -> str:
        base = str(self._client.base_url).rstrip("/")
        if link.startswith(base):
            link = link[len(base) :]
        if link.startswith("/wiki/"):
            link = link[len("/wiki") :]
        return link or "/"

    def _iter_results(self, path: str, params: dict[str, Any] | None = None) -> Iterator[dict[str, Any]]:
        next_path: str | None = path
        next_params = params
        while next_path:
            payload = self._request("GET", next_path, params=next_params)
            if not isinstance(payload, dict):
                return
            for item in payload.get("results") or []:
                if isinstance(item, dict):
                    yield item
            link = (payload.get("_links") or {}).get("next")
            if not isinstance(link, str) or not link:
                return
            next_path = self._relative(link)
            next_params = None

    def current_user(self) -> dict[str, Any]:
        payload = self._request("GET", "/rest/api/user/current")
        if not isinstance(payload, dict):
            raise ConfluenceAPIError(502, "Confluence did not return the current user")
        return payload

    def spaces(self) -> list[dict[str, Any]]:
        return list(self._iter_results("/api/v2/spaces", {"limit": 250}))

    def search(self, cql: str) -> Iterator[dict[str, Any]]:
        yield from self._iter_results(
            "/rest/api/content/search",
            {"cql": cql, "limit": 100},
        )

    def page(self, page_id: str) -> dict[str, Any]:
        return self._request(
            "GET",
            f"/api/v2/pages/{quote(str(page_id), safe='')}",
            params={"body-format": "storage"},
        )

    def blogpost(self, page_id: str) -> dict[str, Any]:
        return self._request(
            "GET",
            f"/api/v2/blogposts/{quote(str(page_id), safe='')}",
            params={"body-format": "storage"},
        )

    def content(self, page_id: str, content_type: str) -> dict[str, Any]:
        if content_type == "blogpost":
            return self.blogpost(page_id)
        return self.page(page_id)

    def footer_comments(self, page_id: str, content_type: str = "page") -> list[dict[str, Any]]:
        root = "blogposts" if content_type == "blogpost" else "pages"
        return list(
            self._iter_results(
                f"/api/v2/{root}/{quote(str(page_id), safe='')}/footer-comments",
                {"body-format": "storage", "limit": 100},
            )
        )

    def inline_comments(self, page_id: str, content_type: str = "page") -> list[dict[str, Any]]:
        root = "blogposts" if content_type == "blogpost" else "pages"
        return list(
            self._iter_results(
                f"/api/v2/{root}/{quote(str(page_id), safe='')}/inline-comments",
                {"body-format": "storage", "limit": 100},
            )
        )

    def labels(self, page_id: str, content_type: str = "page") -> list[dict[str, Any]]:
        root = "blogposts" if content_type == "blogpost" else "pages"
        return list(
            self._iter_results(
                f"/api/v2/{root}/{quote(str(page_id), safe='')}/labels",
                {"limit": 100},
            )
        )

    def attachments(self, page_id: str, content_type: str = "page") -> list[dict[str, Any]]:
        root = "blogposts" if content_type == "blogpost" else "pages"
        return list(
            self._iter_results(
                f"/api/v2/{root}/{quote(str(page_id), safe='')}/attachments",
                {"limit": 100},
            )
        )

    def space(self, space_id: str) -> dict[str, Any]:
        return self._request(
            "GET",
            f"/api/v2/spaces/{quote(str(space_id), safe='')}",
        )

    def user(self, account_id: str) -> dict[str, Any]:
        return self._request(
            "GET",
            "/rest/api/user",
            params={"accountId": account_id},
        )

    def users(self, account_ids: list[str]) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []
        for account_id in dict.fromkeys(account_ids):
            if not account_id:
                continue
            try:
                payload = self.user(account_id)
            except ConfluenceAPIError as exc:
                if exc.status_code == 404:
                    continue
                raise
            if isinstance(payload, dict):
                found.append(payload)
        return found
