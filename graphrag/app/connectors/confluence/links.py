"""Issue keys and Zendesk ticket URLs found on a Confluence page."""

from __future__ import annotations

import re

_JIRA_KEY = re.compile(r"\b([A-Za-z][A-Za-z0-9]+-\d+)\b")
_ZENDESK_URL = re.compile(
    r"https?://([a-z0-9-]+)\.zendesk\.com/[^\s\"'<>]*/tickets/(\d+)",
    re.IGNORECASE,
)


def issue_keys(*texts: str) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for text in texts:
        for match in _JIRA_KEY.finditer(text or ""):
            key = match.group(1).upper()
            if key not in seen:
                seen.add(key)
                found.append(key)
    return found


def zendesk_tickets(*texts: str) -> list[tuple[str, str]]:
    """Return (subdomain, ticket_id) pairs from Zendesk ticket URLs."""
    found: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for text in texts:
        for match in _ZENDESK_URL.finditer(text or ""):
            pair = (match.group(1).lower(), match.group(2))
            if pair not in seen:
                seen.add(pair)
                found.append(pair)
    return found
