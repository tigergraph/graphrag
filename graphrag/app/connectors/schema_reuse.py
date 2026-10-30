"""Decide when a connector may reuse a vertex type that is already installed."""

from __future__ import annotations

_SEPARATE = {frozenset({"jiraissue", "zendeskticket"})}


def must_remain_separate(proposed_name: str, existing_name: str) -> bool:
    """Reject treating a Zendesk ticket as a Jira issue, even if ids look alike."""
    return (
        frozenset({proposed_name.casefold(), existing_name.casefold()}) in _SEPARATE
    )


def _types(attributes: dict[str, str]) -> dict[str, str]:
    return {
        str(name).casefold(): str(attribute_type).upper()
        for name, attribute_type in attributes.items()
    }


def can_reuse_vertex(
    proposed_name: str,
    proposed_attributes: dict[str, str],
    existing_name: str,
    existing_attributes: dict[str, str],
) -> bool:
    """Reuse a vertex only when the normalized name matches and types agree."""
    if must_remain_separate(proposed_name, existing_name):
        return False
    if proposed_name.casefold() != existing_name.casefold():
        return False
    existing = _types(existing_attributes)
    for name, attribute_type in _types(proposed_attributes).items():
        if existing.get(name) != attribute_type:
            return False
    return True
