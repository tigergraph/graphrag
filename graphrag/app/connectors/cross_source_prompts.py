"""Guidance for questions that relate records from two connectors.

Examples: "does Zendesk ticket 59038 have a Jira issue?", "what does the
Confluence design say about GML-2348?". prompt_selection.py adds this text
when a question selects two or more connectors and asks about a relation.

A connector that writes a cross-source edge registers the pair in PAIRS with
the edge it writes. A pair with no entry still gets the default text, which
only allows a relation the graph stores as an edge.
"""

from __future__ import annotations

import re

_RELATION = re.compile(
    r"\b(related|relationship|relate|link|links|linked|mention|mentions|"
    r"mentioned|reference|references|referenced|connected|compare|"
    r"comparison|versus|vs|both|across|exist|exists|in jira|in zendesk|"
    r"in confluence|say|says|said|about|document|documents|documented|"
    r"design|rca|escalat\w*)\b",
    re.IGNORECASE,
)


def asks_relation(question: str) -> bool:
    return bool(_RELATION.search(question or ""))


# frozenset({connector, connector}) -> {"planner"|"query"|"answer": text}
PAIRS: dict[frozenset, dict[str, str]] = {}

_DEFAULT = {
    "planner": (
        "## Cross-source guidance\n"
        "- The question relates records from more than one source. First "
        "identify the record in the source the user names, with that source's "
        "identifier. Then follow the cross-source edge from that record and "
        "retrieve the other source's records it reaches. Use one focused "
        "structural step per source; add a hybrid search only for text the "
        "question asks about, such as what a page says.\n"
        "- A relation exists only when the graph stores an edge for it. Do "
        "not join records by similar numbers, titles, customer names, or text."
    ),
    "query": (
        "## Cross-source query guidance\n"
        "- Match each record with its own identifier and attribute, then "
        "traverse the stored cross-source edge between them. Return named "
        "fields from both ends. Never compare one source's identifier with "
        "another source's identifier."
    ),
    "answer": (
        "## Cross-source answer guidance\n"
        "- Describe each record with its own source's facts, then state the "
        "link and which edge it came from. If the edge is absent, say no "
        "stored link was found between them; do not claim a relation from "
        "similar content alone."
    ),
}


def guidance_for(selected: set[str], question: str, step: str) -> str:
    """Relation guidance for two or more selected connectors, or ''."""
    if len(selected) < 2 or not asks_relation(question):
        return ""
    parts = [_DEFAULT[step]] if step in _DEFAULT else []
    names = sorted(selected)
    for i, left in enumerate(names):
        for right in names[i + 1:]:
            text = PAIRS.get(frozenset((left, right)), {}).get(step)
            if text:
                parts.append(text)
    return "\n".join(parts)
