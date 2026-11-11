"""Select connector prompt guidance for one question and processing step.

Global instructions stay in the existing prompt templates. This module only
returns the connector text that should be added to ``{user_prompt}`` or
``{query_guidance}`` for the current call.
"""

from __future__ import annotations

import re

_JIRA_KEY = re.compile(r"\b[A-Za-z][A-Za-z0-9]+-\d+\b")
_SPACED_JIRA_KEY = re.compile(
    r"\b([A-Za-z][A-Za-z0-9]+)\s*-\s*(\d+)\b"
)
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
            "search for the description and recent activity. When the question "
            "asks for a linked Zendesk ticket, traverse links_issue "
            "from ZendeskTicket to this JiraIssue. For history or "
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
            "edge attributes rather than the edge object.\n"
            "- For a linked Zendesk ticket, traverse links_issue from "
            "ZendeskTicket to JiraIssue. Return ticket_id, subject, status, "
            "and url. Do not join issue_key to a number found in text."
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
            "changes. A linked Zendesk ticket is a ZendeskTicket reached by "
            "links_issue. If that edge is absent, say no Zendesk "
            "link was ingested. A history question should present changes in time order "
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
            "Apply this section to ZendeskTicket, ZendeskComment, "
            "ZendeskChange, Person, Organization, Attachment, requested, "
            "assigned, member, authored, changed, and links_issue.\n"
            "- A Zendesk ticket is identified by its ticket_id. A bare number "
            "is ambiguous unless the user says Zendesk or the conversation "
            "has already established Zendesk.\n"
            "- Read subject, description, status, priority, ticket_type, tags, "
            "custom_fields, problem_id, followup_ids, source_ticket_id, "
            "group_name, url, created, and updated from ZendeskTicket. "
            "A summary reads subject, description, status, and the comment "
            "bodies. Current status is ZendeskTicket.status. Next steps and "
            "action items are drafted from the description and comment bodies. "
            "There is no next-step field and no action-item field. "
            "Urgency is priority. The current assignee is assigned. "
            "created and updated are when the ticket was opened and last "
            "changed. Status history is ZendeskChange where ticket_id matches "
            "and field is status. The person who changed it is changed. "
            "Comments store ticket_id, created, and author_role. The author "
            "is authored. A support-team response is a public comment whose "
            "author_role is agent or admin, and the last one is the comment "
            "with the latest created. The requester is requested. The customer "
            "is member. Linked Zendesk tickets are problem_id, followup_ids, "
            "source_ticket_id, and other tickets whose problem_id is this "
            "ticket. Files are Attachment rows whose parent_id is the ticket id.\n"
            "- A link to Jira is links_issue from ZendeskTicket to JiraIssue. "
            "Use it only when the question asks for the linked Jira issue. "
            "Do not invent a link from a key or URL in the description.\n"
            "- Use word search only when the question names no field."
        ),
        "query": (
            "## Zendesk query guidance\n"
            "- Match ZendeskTicket.ticket_id and return named attributes, "
            "including description, priority, created, updated, problem_id, "
            "followup_ids, and source_ticket_id. Do not return a bare vertex.\n"
            "- Comments have no edge to the ticket. Filter "
            "ZendeskComment.ticket_id and return body, visibility, author_role, "
            "and created. The author is authored. The current assignee is "
            "assigned. Changes have no edge to the ticket. Filter "
            "ZendeskChange.ticket_id and field. The requester is requested. "
            "The customer is member.\n"
            "- Traverse links_issue only when the question asks for the linked "
            "Jira issue. Do not infer that edge from text."
        ),
        "answer": (
            "## Zendesk answer guidance\n"
            "- Quote description and comment body from the stored attributes. "
            "Current status is ZendeskTicket.status. A summary uses subject, "
            "status, description, and the comment bodies. Next steps and "
            "action items are drafted from that stored text. Urgency is "
            "priority. The assignee is the assigned person. A support-team "
            "response time is the latest created on a public comment whose "
            "author_role is agent or admin. Linked tickets are problem_id, "
            "followup_ids, source_ticket_id, incidents with that problem_id, "
            "and Jira issues on links_issue.\n"
            "- Use Person.display_name. Do not print internal vertex ids. "
            "If several tickets match, list ticket id, subject, and status "
            "and ask which one. If a field is not on the ticket, say so. "
            "If the linked Jira issue has empty attributes and a Jira MCP "
            "server is configured, fetch those details with issue_key."
        ),
    },
}

_CROSS = {
    "planner": (
        "## Jira and Zendesk relationship guidance\n"
        "Plan focused retrievals for both records. Zendesk writes links_issue "
        "from the ticket to the Jira issue. Retrieve each record's current "
        "fields. The edge must exist; do not infer a relationship from similar "
        "numbers, titles, customer names, or text."
    ),
    "query": (
        "## Jira and Zendesk relationship query guidance\n"
        "Traverse links_issue from ZendeskTicket to JiraIssue. "
        "Match each endpoint with its own identifier and return named fields "
        "from both endpoints. Do not join issue_key to ticket_id and do not "
        "infer an edge from matching text."
    ),
    "answer": (
        "## Jira and Zendesk relationship answer guidance\n"
        "If links_issue is present, identify both records and explain "
        "their source-specific facts separately before describing the link. "
        "If the edge is absent, say that no explicit Zendesk Jira link was "
        "found. Do not claim a relationship from similar content alone."
    ),
}

_CLARIFY = (
    "The source system is ambiguous. Ask the user whether they mean a Jira "
    "issue or a Zendesk ticket before answering, unless the conversation has "
    "already established the source. Do not guess from a bare number, and do "
    "not apply either connector's rules until the source is known."
)

def normalize_connector_question(text: str) -> str:
    """Normalize qualified connector identifiers without changing bare ids."""
    return _SPACED_JIRA_KEY.sub(
        lambda match: f"{match.group(1).upper()}-{match.group(2)}",
        text or "",
    )


def selected_connectors(question: str) -> set[str]:
    text = normalize_connector_question(question)
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
    if name in {"Document", "DocumentChunk", "Content"}:
        return True
    if name in {"Person", "Organization", "Attachment"}:
        return True
    zendesk_types = {"ZendeskTicket", "ZendeskComment", "ZendeskChange"}
    if "jira" in selected and (name.startswith("Jira") or name == "ZendeskTicket"):
        return True
    if "zendesk" in selected and (name in zendesk_types or name == "JiraIssue"):
        return True
    if not selected:
        return True
    return False
