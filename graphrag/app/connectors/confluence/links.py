"""Issue keys and Zendesk ticket URLs found on a Confluence page."""

from __future__ import annotations

import re

# A key stands alone: not glued to a word or hyphen before it, and not the
# front of a longer id such as CVE-2022-1234 or a32fd086-90177-11423.
_JIRA_KEY = re.compile(r"(?<![\w-])([A-Za-z][A-Za-z0-9]+-\d+)(?!-?\d)(?!\w)")
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


def jira_project(issue_key: str) -> str:
    return issue_key.rsplit("-", 1)[0].upper()


def known_issue_keys(keys: list[str], projects: set[str]) -> list[str]:
    """Keep the keys whose project is a real Jira project.

    The key pattern also matches test case ids (TC-12), years (CVE-2022),
    and words (FROM-3). Only a known project turns a match into a link.
    """
    allowed = {project.upper() for project in projects}
    return [key for key in keys if jira_project(key) in allowed]


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
