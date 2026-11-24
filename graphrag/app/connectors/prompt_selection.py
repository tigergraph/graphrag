"""Select connector prompt guidance for one question and processing step.

Global instructions stay in the existing prompt templates. This module only
returns the connector text that should be added to ``{user_prompt}`` or
``{query_guidance}`` for the current call. The text is applied automatically,
so a graph's own prompt files do not need connector rules.

Each connector keeps its guidance in ``connectors/<name>/prompts.py`` and is
listed in ``_CONNECTORS``. Questions that relate two sources also get the text
from ``cross_source_prompts.py``.
"""

from __future__ import annotations

import re

from connectors import cross_source_prompts
from connectors.jira import prompts as jira_prompts

_CONNECTORS = {module.NAME: module for module in (jira_prompts,)}

_SPACED_JIRA_KEY = re.compile(r"\b([A-Za-z][A-Za-z0-9]+)\s*-\s*(\d+)\b")
_RECORD_QUESTION = re.compile(
    r"\b(ticket|issue|assignee|reporter|comment|status|priority|page)\b",
    re.IGNORECASE,
)
# A record number on its own, such as "#58617" or "summarize 59224". Four-digit
# years are not record numbers.
_BARE_NUMBER = re.compile(r"(?<![\w-])#?(\d{4,})\b")
_YEAR = re.compile(r"(?:19|20)\d{2}")

_SHARED = {
    "planner": (
        "## Record question guidance\n"
        "- A question that asks only for stored fields of a record, such as "
        "its status, assignee, priority, dates, or URL, uses structural steps "
        "only. Do not add a hybrid search for it.\n"
        "- A summary, 'tell me about', what-happened, why, or next-steps "
        "question uses structural steps for the record plus a hybrid search "
        "for its description and discussion text.\n"
        "- A count, existence, list, or filter question over records is "
        "answered by a structural step. An empty result is the answer (none, "
        "or 0); it is not a reason to search the documents instead."
    ),
    "query": (
        "## Record query guidance\n"
        "- A count question returns one row with the count, 0 when nothing "
        "matches. An existence or list question also returns the number of "
        "matching records.\n"
        "- Group and count in the query instead of returning every row. Match "
        "the identifier or filter first, then traverse typed edges in their "
        "schema direction (->); never use untyped patterns such as -[]-. "
        "Never return every vertex of a type.\n"
        "- Do not use OPTIONAL MATCH: TigerGraph often rejects these queries "
        "or returns no rows for them. Fetch each related record in its own "
        "query with MATCH instead."
    ),
    "answer": (
        "## Record answer guidance\n"
        "- Text that explains the schema, a field's possible values, or how a "
        "connector works is documentation, not record data. Never answer a "
        "question about specific records from it. When the structured result "
        "is empty, say that no matching records were found, or 0 for a count."
    ),
}

_CLARIFY = (
    "The source system is ambiguous. Ask the user which source they mean "
    "before answering, unless the conversation has already established it."
)


def normalize_connector_question(text: str) -> str:
    """Normalize qualified connector identifiers without changing bare ids."""
    return _SPACED_JIRA_KEY.sub(
        lambda match: f"{match.group(1).upper()}-{match.group(2)}",
        text or "",
    )


def selected_connectors(question: str) -> set[str]:
    text = normalize_connector_question(question)
    return {name for name, module in _CONNECTORS.items() if module.selects(text)}


def bare_record_number(question: str) -> str | None:
    """The record number in a question that names no source, if any."""
    for match in _BARE_NUMBER.finditer(question or ""):
        number = match.group(1)
        if not (len(number) == 4 and _YEAR.fullmatch(number)):
            return number
    return None


def _identifier_guidance(number: str, step: str) -> str:
    lookups = "\n".join(f"  - {module.IDENTIFIER}" for module in _CONNECTORS.values())
    sources = ", ".join(module.LABEL for module in _CONNECTORS.values())
    if step == "planner":
        return (
            "## Record number guidance\n"
            f"The question gives the number {number} without naming a source. "
            "Plan one structural step that looks the number up in every "
            "installed source below, and do not ask first:\n"
            f"{lookups.replace('<number>', number)}\n"
            "If exactly one source has a record, continue with that record "
            "using that source's guidance. If several sources match, list the "
            "matches and ask which one is meant."
        )
    if step == "query":
        return (
            "## Record number query guidance\n"
            f"Look {number} up in each source below and return which source "
            "matched with the record's identifier and title or summary:\n"
            f"{lookups.replace('<number>', number)}"
        )
    if step == "answer":
        return (
            "## Record number answer guidance\n"
            f"{number} was given without a source ({sources}). Answer from the "
            "single source that has a record with that number. If several "
            "match, list them and ask which is meant. If none match, say the "
            "number was not found in any source."
        )
    return ""


def guidance_for(question: str, step: str) -> str:
    """Return connector guidance for this step, or a clarification request."""
    selected = selected_connectors(question)
    if not selected:
        number = bare_record_number(question)
        if number:
            parts = [_identifier_guidance(number, step), _SHARED.get(step, "")]
            return "\n".join(part for part in parts if part)
        if step in {"planner", "answer"} and _RECORD_QUESTION.search(question or ""):
            return _CLARIFY
        return ""
    parts = [
        _CONNECTORS[name].GUIDANCE[step]
        for name in sorted(selected)
        if step in _CONNECTORS[name].GUIDANCE
    ]
    if step in _SHARED:
        parts.append(_SHARED[step])
    cross = cross_source_prompts.guidance_for(selected, question, step)
    if cross:
        parts.append(cross)
    return "\n".join(parts)


def allows_type(type_name: str, selected: set[str]) -> bool:
    """Keep core types plus the vertex types owned by the selected connectors."""
    name = type_name.split(" ", 1)[0]
    if name in {"Document", "DocumentChunk", "Content"}:
        return True
    if name in {"Person", "Organization", "Attachment"}:
        return True
    if not selected:
        return True
    return any(_CONNECTORS[c].owns_type(name) for c in selected if c in _CONNECTORS)
