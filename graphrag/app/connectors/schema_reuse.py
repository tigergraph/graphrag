"""Decide when a connector may reuse a vertex type that is already installed."""

from __future__ import annotations


def _normalize_type_name(name: str) -> str:
    return "".join(character for character in name if character.isalnum()).casefold()


def must_remain_separate(left: str, right: str) -> bool:
    """Jira issues and Zendesk tickets are different entities."""
    return {_normalize_type_name(left), _normalize_type_name(right)} == {
        "jiraissue",
        "zendeskticket",
    }


def can_reuse_vertex(
    proposed_name: str,
    proposed_attributes: dict[str, str],
    existing_name: str,
    existing_attributes: dict[str, str],
) -> bool:
    """Reuse only the same normalized type when attribute types agree.

    Similar identifiers do not make two different type names compatible.
    A non-empty existing attribute is not replaced when its type differs.
    """
    if must_remain_separate(proposed_name, existing_name):
        return False
    if _normalize_type_name(proposed_name) != _normalize_type_name(existing_name):
        return False
    for name, attribute_type in proposed_attributes.items():
        current = existing_attributes.get(name.casefold())
        if current and current.upper() != attribute_type.upper():
            return False
    return True
