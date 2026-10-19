"""Select connector prompt guidance for one question and processing step.

Global instructions stay in the existing prompt templates. This module only
returns the connector text that should be added to ``{user_prompt}`` or
``{query_guidance}`` for the current call.
"""

from __future__ import annotations

import re

_JIRA_KEY = re.compile(r"\b[A-Za-z][A-Za-z0-9]+-\d+\b")
_ZENDESK_URL = re.compile(
    r"zendesk\.com/\S*?/tickets/\d+|\bzendesk\b",
    re.IGNORECASE,
)
_RECORD_QUESTION = re.compile(
    r"\b(ticket|issue|assignee|reporter|comment|status|priority)\b",
    re.IGNORECASE,
)

_GUIDANCE = {
    "jira": {
        "planner": (
            "## Jira guidance\n"
            "Apply this section only to JiraIssue, JiraComment, JiraChange, "
            "JiraUser, JiraProject, and JIRA_ relationships.\n"
            "- A full project key and number, such as GML-2186, identifies one "
            "issue. Match issue_key case-insensitively. A bare number does not "
            "identify one issue: retrieve matching full keys and ask the user "
            "to choose when more than one exists. A follow-up may use the Jira "
            "issue already established in the conversation.\n"
            "- Jira data has two complementary forms. Structural retrieval "
            "contains current JiraIssue fields; assignee and reporter through "
            "JIRA_ASSIGNED_TO and JIRA_REPORTED_BY; project through "
            "JIRA_BELONGS_TO; comments through JIRA_COMMENT_ON and "
            "JIRA_COMMENTED_BY; and status, assignee, priority, or resolution "
            "events through JIRA_HAS_CHANGE and JIRA_CHANGE_BY. Hybrid search "
            "contains the description and supporting text from comments and "
            "changes. Include the exact issue key in retrieval questions.\n"
            "- For a broad issue summary, plan structural retrieval for the "
            "current record, assignee, reporter, and project, plus hybrid "
            "search for the description and recent activity. For history or "
            "who-did-what questions, retrieve JiraChange events and comments "
            "with their authors. For release questions, retrieve fix_versions, "
            "due, status, and resolution plus any release text in passages.\n"
            "- Counts, lists, filters, and aggregations over issue attributes "
            "are structural-only unless the question also needs description "
            "or comment text. Keep parent and linked-issue traversals in "
            "separate focused structural steps."
        ),
        "query": (
            "## Jira query guidance\n"
            "- Return scalar attributes, never a bare vertex. For JiraIssue, "
            "return the requested fields such as issue_key, summary, status, "
            "status_category, priority, resolution, fix_versions, due, "
            "created, updated, and url. For JiraUser return display_name. For "
            "JiraProject return project_key, name, and url.\n"
            "- Use an exact case-insensitive issue_key match when a full key "
            "is known. When only a number is known, return all matching full "
            "issue keys and summaries so the user can disambiguate.\n"
            "- Use focused queries: current issue with assignee, reporter, and "
            "project; JiraComment body and author via JIRA_COMMENT_ON and "
            "JIRA_COMMENTED_BY; JiraChange field, from_value, to_value, "
            "created, and changer via JIRA_HAS_CHANGE and JIRA_CHANGE_BY.\n"
            "- Query JIRA_HAS_PARENT and JIRA_LINKS_TO only when requested, in "
            "a separate traversal. Return the related issue_key and requested "
            "edge attributes rather than the edge object."
        ),
        "answer": (
            "## Jira answer guidance\n"
            "- Treat structured rows and passages as complementary evidence "
            "for the same issue. Prefer structured values for current status, "
            "assignee, reporter, priority, resolution, fix version, and due "
            "date. Use passages for description and supporting activity text.\n"
            "- Never show internal ids such as jira:user, jira:comment, or "
            "jira:change as display values. Use display_name; if unavailable, "
            "say the name is unavailable.\n"
            "- A broad question should include the issue key, summary, URL, "
            "current fields, description, and relevant recent comments or "
            "changes. A history question should present changes in time order "
            "and state the current status. A release question must not invent "
            "a release when fix_versions, due date, and supporting text do not "
            "provide one.\n"
            "- If a bare number matches several Jira keys, list those keys and "
            "ask which issue the user means without mixing their details."
        ),
    },
    "zendesk": {
        "planner": (
            "## Zendesk guidance\n"
            "Apply this section only to ZendeskTicket, ZendeskComment, "
            "ZendeskUser, ZENDESK_REQUESTED_BY, and ZENDESK_COMMENT_ON.\n"
            "- A Zendesk ticket is identified by its ticket_id together with "
            "the configured Zendesk source. Do not apply Jira issue-key rules "
            "or rewrite a ticket number as a Jira key. A bare ticket number is "
            "ambiguous unless the user says Zendesk or the conversation has "
            "already established Zendesk.\n"
            "- Structural retrieval contains ZendeskTicket ticket_id, subject, "
            "status, and url; its requester is a ZendeskUser reached through "
            "ZENDESK_REQUESTED_BY; comments are ZendeskComment vertices reached "
            "through ZENDESK_COMMENT_ON. Hybrid search contains ticket subject, "
            "description, and comment text.\n"
            "- For a broad ticket summary, plan structural retrieval for the "
            "ticket and requester plus hybrid search for the description and "
            "conversation. For status or requester questions, use structural "
            "retrieval. For what-happened or discussion questions, combine the "
            "ticket state with relevant comment passages.\n"
            "- Do not request assignee, priority, dates, tags, or other fields "
            "that are not present in the installed Zendesk schema."
        ),
        "query": (
            "## Zendesk query guidance\n"
            "- Match ZendeskTicket.ticket_id for a ticket lookup and return "
            "named fields: ticket_id, subject, status, and url. Do not return "
            "a bare ZendeskTicket vertex.\n"
            "- Retrieve requester user_id and display_name through "
            "ZENDESK_REQUESTED_BY. Retrieve comment_id and body from "
            "ZendeskComment through ZENDESK_COMMENT_ON.\n"
            "- A Zendesk ticket number is not a Jira issue key. Do not query "
            "JiraIssue unless the question explicitly selects Jira too."
        ),
        "answer": (
            "## Zendesk answer guidance\n"
            "- Prefer the structured ZendeskTicket status, subject, URL, and "
            "requester for current facts. Use description and comment passages "
            "for the ticket narrative and conversation.\n"
            "- Never present internal zendesk: vertex ids as user-facing names. "
            "Use ZendeskUser.display_name when available.\n"
            "- A broad summary should include ticket id, subject, URL, current "
            "status, requester, description, and relevant comments. Do not "
            "invent fields that are absent from the Zendesk schema, and never "
            "treat the ticket number as a Jira issue key."
        ),
    },
}

_CROSS = {
    "planner": (
        "## Jira and Zendesk relationship guidance\n"
        "Plan focused retrievals for both records and traverse "
        "CROSS_SOURCE_LINK between JiraIssue and ZendeskTicket. Retrieve each "
        "record's current structured fields and use source-specific hybrid "
        "search only when description or comment evidence is needed. The edge "
        "must exist; do not infer a relationship from similar numbers, titles, "
        "customer names, or text."
    ),
    "query": (
        "## Jira and Zendesk relationship query guidance\n"
        "Traverse CROSS_SOURCE_LINK only between JiraIssue and ZendeskTicket. "
        "Match each endpoint with its own identifier and return named fields "
        "from both endpoints. Do not join issue_key to ticket_id and do not "
        "infer an edge from matching text."
    ),
    "answer": (
        "## Jira and Zendesk relationship answer guidance\n"
        "If CROSS_SOURCE_LINK is present, identify both records and explain "
        "their source-specific facts separately before describing the link. "
        "If the edge is absent, say that no explicit cross-source link was "
        "found. Do not claim a relationship from similar content alone."
    ),
}

_CLARIFY = (
    "The source system is ambiguous. Ask the user whether they mean a Jira "
    "issue or a Zendesk ticket before answering, unless the conversation has "
    "already established the source. Do not guess from a bare number, and do "
    "not apply either connector's rules until the source is known."
)


def selected_connectors(question: str) -> set[str]:
    text = question or ""
    selected: set[str] = set()
    if _JIRA_KEY.search(text) or re.search(r"\bjira\b", text, re.IGNORECASE):
        selected.add("jira")
    if _ZENDESK_URL.search(text):
        selected.add("zendesk")
    return selected


def guidance_for(question: str, step: str) -> str:
    """Return connector guidance for this step, or a clarification request."""
    selected = selected_connectors(question)
    if not selected:
        if step in {"planner", "answer"} and _RECORD_QUESTION.search(question or ""):
            return _CLARIFY
        return ""
    parts = [_GUIDANCE[name][step] for name in sorted(selected) if step in _GUIDANCE[name]]
    if selected == {"jira", "zendesk"} and re.search(
        r"\b(related|relationship|link|linked|mention|reference|connected|"
        r"compare|comparison|versus|vs|both|across)\b",
        question or "",
        re.IGNORECASE,
    ):
        parts.append(_CROSS[step])
    return "\n".join(parts)


def allows_type(type_name: str, selected: set[str]) -> bool:
    """Keep core types plus the vertex types owned by the selected connectors."""
    name = type_name.split(" ", 1)[0]
    if name in {"Document", "DocumentChunk", "Content", "PendingRef", "PendingLink"}:
        return True
    if "jira" in selected and name.startswith("Jira"):
        return True
    if "zendesk" in selected and name.startswith("Zendesk"):
        return True
    if not selected:
        return True
    return False
