"""Zendesk Support REST client used by connection tests and ticket ingest."""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable

import httpx

from .config import ZendeskDataSource

logger = logging.getLogger(__name__)


class ZendeskAPIError(RuntimeError):
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code


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
                raise ZendeskAPIError(
                    response.status_code,
                    self._error_message(response),
                )
            if response.status_code == 204:
                return None
            return response.json()
        raise ZendeskAPIError(503, f"Zendesk request failed: {last_error}")

    def myself(self) -> dict[str, Any]:
        payload = self._request("GET", "/api/v2/users/me.json")
        user = payload.get("user") if isinstance(payload, dict) else None
        if not isinstance(user, dict):
            raise ZendeskAPIError(502, "Zendesk did not return the current user.")
        return user

    def search_query(self) -> str:
        parts = ["type:ticket"]
        updated_after = self.source.scope.updated_after
        if updated_after is not None:
            parts.append(f"updated>={updated_after.isoformat()}")
        for status in self.source.scope.statuses:
            parts.append(f"status:{status}")
        return " ".join(parts)

    def ticket_count(self) -> int:
        payload = self._request(
            "GET",
            "/api/v2/search/count.json",
            params={"query": self.search_query()},
        )
        return int((payload or {}).get("count") or 0)

    def iter_ticket_pages(
        self,
        start_time: int,
    ) -> Iterator[tuple[list[dict[str, Any]], int | None, bool]]:
        """Yield incremental ticket pages as ``(tickets, end_time, done)``."""
        path = "/api/v2/incremental/tickets/cursor.json"
        params: dict[str, Any] | None = {"start_time": max(int(start_time), 1)}
        seen: set[str] = set()
        while path and path not in seen:
            seen.add(path)
            payload = self._request("GET", path, params=params) or {}
            params = None
            tickets = payload.get("tickets") or []
            end_time = payload.get("end_time")
            done = bool(payload.get("end_of_stream"))
            yield list(tickets), (int(end_time) if end_time else None), done
            if done:
                return
            path = payload.get("after_url") or ""
            if not path:
                cursor = payload.get("after_cursor")
                if not cursor:
                    return
                path = "/api/v2/incremental/tickets/cursor.json"
                params = {"cursor": cursor}

    def comments(self, ticket_id: str) -> list[dict[str, Any]]:
        path = f"/api/v2/tickets/{ticket_id}/comments.json"
        collected: list[dict[str, Any]] = []
        seen: set[str] = set()
        while path and path not in seen:
            seen.add(path)
            payload = self._request("GET", path) or {}
            collected.extend(payload.get("comments") or [])
            path = payload.get("next_page") or ""
        return collected

    def users(self, user_ids: list[str]) -> dict[str, dict[str, Any]]:
        found: dict[str, dict[str, Any]] = {}
        unique = [user_id for user_id in dict.fromkeys(user_ids) if user_id]
        for start in range(0, len(unique), 100):
            batch = unique[start : start + 100]
            payload = self._request(
                "GET",
                "/api/v2/users/show_many.json",
                params={"ids": ",".join(batch)},
            ) or {}
            for user in payload.get("users") or []:
                found[str(user.get("id"))] = user
        return found
