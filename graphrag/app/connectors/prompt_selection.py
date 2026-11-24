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
# A question about stored records, as opposed to a general question: it
# names a record type or field of an installed connector (RECORD_WORDS).
_RECORD_QUESTION = re.compile(
    r"\b("
    + "|".join(sorted({w for m in _CONNECTORS.values() for w in m.RECORD_WORDS}))
    + r")s?\b",
    re.IGNORECASE,
)
# A record number on its own, such as "#12345" or "summarize 67890". Four-digit
# years are not record numbers.
_BARE_NUMBER = re.compile(r"(?<![\w-])#?(\d{4,})\b")
_YEAR = re.compile(r"(?:19|20)\d{2}")

_SHARED = {
    "planner": (
        "## Record question guidance\n"
        "- When the answer is made of stored attribute values of identified "
        "records, use structural steps only. Do not add a hybrid search.\n"
        "- When the answer also needs a record's own text (description, "
        "comments, or page body), use structural steps for its attributes "
        "plus a hybrid search that includes the record's identifier.\n"
        "- A count, existence, list, or filter question over records is "
        "answered by a structural step. An empty result is the answer (none, "
        "or 0); it is not a reason to search the documents instead.\n"
        "- When the answer needs several breakdowns of the same set of "
        "records, plan one structural step per breakdown, each a grouped "
        "count over the same filter.\n"
        "- When a step needs an identifier that another step finds, such as "
        "a record id, list that step in depends_on; the dependent step "
        "receives the earlier step's rows and queries by that identifier."
    ),
    "query": (
        "## Record query guidance\n"
        "- A count question returns only the count, for example RETURN "
        "count(t) AS total, so the result is one row and 0 when nothing "
        "matches. Do not add other columns or literals to a count. A count per "
        "group returns the group value and its count.\n"
        "- Match a topic, title, subject, or summary word by word: "
        "toLower(x.summary) CONTAINS each distinctive word, combined with AND. "
        "Stored text rarely contains the user's exact phrase.\n"
        "- Group and count in the query instead of returning every row. Match "
        "the identifier or filter first, then traverse typed edges in their "
        "schema direction (->); never use untyped patterns such as -[]-. "
        "Never return every vertex of a type.\n"
        "- Do not use OPTIONAL MATCH: TigerGraph often rejects these queries "
        "or returns no rows for them. Fetch each related record in its own "
        "query with MATCH instead.\n"
        "- A range comparison (>=, <) on a date string in the same MATCH as "
        "an edge returns no rows in TigerGraph. Filter by date first, then "
        "traverse: MATCH (x:Type) WHERE x.created >= \"<from>\" AND "
        "x.created < \"<to>\" WITH x MATCH (x)-[:edge]->(y) RETURN ....\n"
        "- Joining two records by a stored id (WITH ... MATCH ... WHERE "
        "a.id_field = b.other_field) works only when the first record is "
        "matched by equality, not CONTAINS. With a partial name, find the id "
        "in an earlier step and query by its value.\n"
        "- Do not use CASE, coalesce, or alternative edge types such as "
        "[:requested|assigned]; TigerGraph does not run them here. To break "
        "counts down, group by the stored attribute itself. Records without "
        "the related record are counted in a separate query."
    ),
    "answer": (
        "## Record answer guidance\n"
        "- Text that explains the schema, a field's possible values, or how a "
        "connector works is documentation, not record data. Never answer a "
        "question about specific records from it.\n"
        "- graph_queries_with_no_rows lists graph queries that ran and found "
        "nothing. For a count or existence question that is the answer: 0, or "
        "no matching records. For a record lookup it means that source has no "
        "such record."
    ),
}

_UNNAMED = {
    "planner": (
        "## Source guidance\n"
        "The question names no source. Use every installed source below whose "
        "records fit the question, following that source's guidance. Ask the "
        "user which source they mean only when the question is about one "
        "record that it does not identify."
    ),
    "answer": (
        "## Source guidance\n"
        "The question names no source. Say which source each fact comes from. "
        "Ask the user which source they mean only when the question is about "
        "one record that it does not identify."
    ),
}


def normalize_connector_question(text: str) -> str:
    """Normalize qualified connector identifiers without changing bare ids."""
    return _SPACED_JIRA_KEY.sub(
        lambda match: f"{match.group(1).upper()}-{match.group(2)}",
        text or "",
    )


def selected_connectors(question: str) -> set[str]:
    text = normalize_connector_question(question)
    selected = {name for name, module in _CONNECTORS.items() if module.selects(text)}
    if selected:
        # A question that names one source can also be about another
        # connector's records, e.g. the Confluence page about a ticket.
        for name, module in _CONNECTORS.items():
            words = getattr(module, "ALSO_SELECTED_BY", ())
            if name not in selected and any(
                re.search(rf"\b{re.escape(w)}s?\b", text, re.IGNORECASE) for w in words
            ):
                selected.add(name)
    return selected


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
            "Do not ask first. Plan one structural step for each source "
            "below; each step looks the number up in that one source only, "
            "because each step runs one query:\n"
            f"{lookups.replace('<number>', number)}\n"
            "In the same plan, add for each source the steps for the related "
            "records the question asks for, with the number as that source's "
            "identifier and following that "
            "source's guidance below. Steps for a source that has no such "
            "record return no rows. If several sources have a record, list the "
            "matches and ask which one is meant."
        )
    if step == "query":
        return (
            "## Record number query guidance\n"
            f"The step looks {number} up in one of the sources below. Query "
            "only the source the step names, with that source's lookup, and "
            "return the record's identifier and title or summary:\n"
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
            # The source is unknown, so every connector's rules apply until
            # the lookup shows which source has the record.
            parts = [_identifier_guidance(number, step)]
            parts += [
                _CONNECTORS[name].GUIDANCE[step]
                for name in sorted(_CONNECTORS)
                if step in _CONNECTORS[name].GUIDANCE
            ]
            parts.append(_SHARED.get(step, ""))
            return "\n".join(part for part in parts if part)
        if not _RECORD_QUESTION.search(question or ""):
            # The query rules hold for any structural query on this graph,
            # whatever the question names.
            return _SHARED["query"] if step == "query" else ""
        # No source named: every connector's rules apply, and the shared
        # record rules always do.
        parts = [_UNNAMED.get(step, "")]
        parts += [
            _CONNECTORS[name].GUIDANCE[step]
            for name in sorted(_CONNECTORS)
            if step in _CONNECTORS[name].GUIDANCE
        ]
        parts.append(_SHARED.get(step, ""))
        return "\n".join(part for part in parts if part)
    parts = [
        _CONNECTORS[name].GUIDANCE[step]
        for name in sorted(selected)
        if step in _CONNECTORS[name].GUIDANCE
    ]
    if step in _SHARED:
        parts.append(_SHARED[step])
    cross = cross_source_prompts.guidance_for(
        selected, question, step, installed=set(_CONNECTORS)
    )
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
    if any(name in cross_source_prompts.LINKED_TYPES.get(c, ()) for c in selected):
        return True
    return any(_CONNECTORS[c].owns_type(name) for c in selected if c in _CONNECTORS)
