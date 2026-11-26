"""Zendesk guidance added to the planner, query, and answer prompts.

prompt_selection.py adds these texts when a question names Zendesk or a
zendesk.com ticket URL. They describe the schema in schema.py; keep the two
in step.
"""

from __future__ import annotations

import re

NAME = "zendesk"
LABEL = "Zendesk ticket"

_SOURCE = re.compile(r"zendesk\.com/\S*?/tickets/\d+|\bzendesk\b", re.IGNORECASE)


def selects(question: str) -> bool:
    return bool(_SOURCE.search(question))


_TYPES = {"ZendeskTicket", "ZendeskComment", "ZendeskChange"}


def owns_type(name: str) -> bool:
    return name in _TYPES


# Record types and fields of this schema that mark a question as being about
# stored records.
RECORD_WORDS = (
    "ticket", "comment", "requester", "assignee", "customer", "organization",
    "status", "priority", "tag",
)


# How a bare number is looked up in Zendesk when the question names no source.
IDENTIFIER = (
    "Zendesk: the ZendeskTicket whose ticket_id equals \"<number>\". Return "
    "ticket_id, subject, and status."
)

PLANNER = (
    "## Zendesk guidance\n"
    "Applies to ZendeskTicket, ZendeskComment, ZendeskChange, Person, "
    "Organization, Attachment, and the edges requested, assigned, member, "
    "authored, changed, and links_issue.\n"
    "- A Zendesk ticket is identified by its ticket_id. A follow-up that does "
    "not repeat the number refers to the ticket already established in the "
    "conversation.\n"
    "- Current fields are ZendeskTicket attributes: ticket_id, subject, "
    "description, status, priority, ticket_type, tags, custom_fields, "
    "problem_id, followup_ids, source_ticket_id, group_name, url, created, "
    "updated. Urgency is priority.\n"
    "- People: the requester is requested and the current assignee is "
    "assigned, both from Person to the ticket. The customer is the "
    "Organization that has the requester as a member. Comments are "
    "ZendeskComment rows whose ticket_id is the ticket (body, visibility, "
    "author_role, created); the author is authored. Status, priority, and "
    "assignee history are ZendeskChange rows whose ticket_id is the ticket; "
    "who changed it is changed. Files are Attachment rows whose parent_id is "
    "the ticket id.\n"
    "- Each structural step asks for one kind of record, because each step "
    "runs one query: the ticket fields, the requester and customer, the "
    "assignee, the comments, or the changes. Plan only the kinds the "
    "question needs.\n"
    "- An answer about the ticket as a whole needs the ticket fields, the "
    "comments, and a hybrid search that includes the ticket id. When the "
    "answer is not a stored field of the ticket, add a structural step for "
    "its comments, and draft the answer from the description and the "
    "comments.\n"
    "- A support-team response is a public comment whose author_role is "
    "agent or admin; order them by created.\n"
    "- When a ticket was solved or closed, and how long it was open, come "
    "from its status changes: the ZendeskChange rows whose field is status, "
    "with to_value solved or closed and their created dates. The ticket's "
    "updated date is not when it was solved.\n"
    "- Linked tickets are independent lookups: first the named ticket's "
    "problem_id, followup_ids, and source_ticket_id; then, separately, the "
    "tickets whose problem_id or source_ticket_id equals the named ticket_id. "
    "A linked Jira issue is links_issue, used only when the question asks "
    "for it. Do not invent a link from a key or URL in the text.\n"
    "- A customer's records are the tickets requested by members of that "
    "Organization. List them in a structural step (ticket_id, subject, "
    "priority, status, created), and use a hybrid search that names the "
    "customer for what the tickets describe. Do not traverse links_issue "
    "unless Jira is asked about.\n"
    "- An ingested ticket is one whose status is set. Other ZendeskTicket "
    "rows are placeholders written for a link, so counts, lists, and "
    "distributions of tickets filter on status not empty.\n"
    "- Counts, lists, and distributions over ticket attributes, customers, "
    "or assignees are structural only.\n"
    "- Use word search only when the question names no field."
)

QUERY = (
    "## Zendesk query guidance\n"
    "- Return named scalar attributes, never a bare vertex. For Person return "
    "display_name and email; for Organization return name.\n"
    "- Edges, with their direction: (Person)-[:requested]->(ZendeskTicket), "
    "(Person)-[:assigned]->(ZendeskTicket), "
    "(Organization)-[:member]->(Person), "
    "(ZendeskComment)-[:authored]->(Person), "
    "(ZendeskChange)-[:changed]->(Person), "
    "(ZendeskTicket)-[:links_issue]->(JiraIssue).\n"
    "- Write exactly one query: one connected MATCH pattern, one RETURN, no "
    "semicolon. Use the pattern for the kind of record the step asks for:\n"
    "  - ticket fields: MATCH (t:ZendeskTicket) WHERE t.ticket_id = "
    "\"<number>\" RETURN the fields\n"
    "  - requester and customer: MATCH (o:Organization)-[:member]->"
    "(p:Person)-[:requested]->(t:ZendeskTicket) WHERE t.ticket_id = "
    "\"<number>\" RETURN p.display_name, o.name\n"
    "  - assignee: MATCH (p:Person)-[:assigned]->(t:ZendeskTicket) WHERE "
    "t.ticket_id = \"<number>\" RETURN p.display_name\n"
    "  - comments: MATCH (c:ZendeskComment)-[:authored]->(p:Person) WHERE "
    "c.ticket_id = \"<number>\" RETURN c.body, c.visibility, c.author_role, "
    "c.created, p.display_name\n"
    "  - changes: MATCH (ch:ZendeskChange)-[:changed]->(p:Person) WHERE "
    "ch.ticket_id = \"<number>\" RETURN ch.field, ch.from_value, "
    "ch.to_value, ch.created, p.display_name\n"
    "  There is no edge from a comment or change to its ticket, so never MATCH "
    "the ZendeskTicket together with its comments or changes.\n"
    "- A linked-ticket lookup returns the named ticket's problem_id, "
    "followup_ids, and source_ticket_id directly. Do not split followup_ids "
    "or MATCH a second ticket in that query.\n"
    "- created and updated are ISO-8601 strings such as "
    "\"2025-03-31T23:10:05Z\". For a calendar month compare "
    "t.created >= \"2025-03-01\" AND t.created < \"2025-04-01\" (first day of "
    "the next month), so the last day of the month is included.\n"
    "- The attribute tags is a reserved word in TigerGraph openCypher and "
    "cannot be read in a query; leave it out.\n"
    "- An ingested ticket has a status: counts, lists, and distributions of "
    "tickets add WHERE t.status <> \"\".\n"
    "- Stored status and priority values are lowercase: status is new, open, "
    "pending, hold, solved, closed, or deleted; priority is low, normal, "
    "high, urgent, or empty. Compare with toLower(). A ticket is still open "
    "when its status is new, open, pending, or hold.\n"
    "- A customer's tickets: MATCH (o:Organization)-[:member]->(p:Person)"
    "-[:requested]->(t:ZendeskTicket) WHERE toLower(o.name) CONTAINS "
    "\"<customer>\" RETURN t.ticket_id, t.subject, t.priority, t.status, "
    "t.created.\n"
    "- Counts per assignee in a date range: MATCH (t:ZendeskTicket) WHERE "
    "t.created >= \"<from>\" AND t.created < \"<to>\" WITH t MATCH "
    "(p:Person)-[:assigned]->(t) RETURN p.display_name, count(t). Counts per "
    "customer: the same first MATCH, then WITH t MATCH (o:Organization)"
    "-[:member]->(p:Person)-[:requested]->(t) RETURN o.name, count(t)."
)

ANSWER = (
    "## Zendesk answer guidance\n"
    "- Prefer structured values for status, priority, assignee, requester, "
    "customer, and dates. Quote the description and comment bodies from the "
    "stored attributes.\n"
    "- Anything the ticket does not store as a field is drafted from the "
    "stored description and comments; say so when the ticket is closed and "
    "nothing is pending.\n"
    "- An action item or next step comes from the latest comment or note "
    "that states one, read together with the comment it answers, so the "
    "answer says who must do what and why. A comment that only names a "
    "person is not an action item without that context.\n"
    "- A support-team response is a public comment whose author_role is "
    "agent or admin; give its date and author.\n"
    "- Linked tickets are problem_id, followup_ids, source_ticket_id, the "
    "incidents with this ticket as problem_id, and Jira issues on "
    "links_issue. If none are set, say the ticket has no linked tickets. "
    "Call tickets linked, follow-ups, or duplicates only when these stored "
    "fields say so; a similar subject is not a link.\n"
    "- Name a person by their stored display_name, even when a comment is "
    "signed with another form of the name. Do not print internal vertex "
    "ids. If several "
    "tickets match, list ticket id, subject, and status and ask which one. "
    "If a field is not on the ticket, say so. If a linked Jira issue has "
    "empty attributes and a Jira MCP server is configured, fetch those "
    "details with issue_key."
)

GUIDANCE = {"planner": PLANNER, "query": QUERY, "answer": ANSWER}

# What the stored values of these types mean, shown with the graph schema so
# that any tool that filters on them uses the right attribute and value.
FIELD_NOTES = {
    "ZendeskTicket": (
        "ticket_id is the ticket number, stored as text. status is lowercase: "
        "new, open, pending, hold, solved, closed, or deleted; a ticket is "
        "still open when it is new, open, pending, or hold. priority is low, "
        "normal, high, urgent, or empty. created and updated are ISO-8601 "
        "text such as 2025-03-31T23:10:05Z. A row with an empty status is a "
        "placeholder written for a link, not an ingested ticket. The "
        "requester and assignee are Person rows on requested and assigned, "
        "and the customer is the Organization the requester is a member of. "
        "tags cannot be read in a query."
    ),
    "ZendeskComment": (
        "ticket_id is the ticket the comment belongs to; no edge links a "
        "comment to its ticket. visibility is public or internal, and "
        "author_role is end-user, agent, or admin."
    ),
    "ZendeskChange": (
        "ticket_id is the ticket that changed; field is status, assignee, or "
        "priority."
    ),
}

# Jira and Zendesk: the link Zendesk writes from its Jira link inventory.
JIRA_PAIR = {
    "planner": (
        "## Jira and Zendesk relationship guidance\n"
        "- Zendesk writes links_issue from the ticket to the Jira issue, from "
        "Zendesk's Jira link inventory. Find the named record by its own "
        "identifier, follow links_issue, and retrieve the other record's "
        "current fields in a separate step.\n"
        "- A Jira issue whose summary names the ticket number, such as an RCA "
        "request for the ticket, is found by a separate structural step "
        "matching the number in the issue summary. Report it as an issue "
        "that names the ticket, not as a stored link.\n"
        "- For a set of tickets, such as a customer's tickets in a month, "
        "check each ticket in the set, not only the first one found."
    ),
    "query": (
        "## Jira and Zendesk relationship query guidance\n"
        "- MATCH (t:ZendeskTicket)-[:links_issue]->(i:JiraIssue) with the "
        "named record's identifier, returning t.ticket_id, t.subject, "
        "t.status, i.issue_key, i.summary, and i.status. Never compare "
        "issue_key with ticket_id."
    ),
    "answer": (
        "## Jira and Zendesk relationship answer guidance\n"
        "- If links_issue is present, describe the ticket and the issue with "
        "their own facts, then the link. If it is absent, say no Zendesk-Jira "
        "link is stored for that record."
    ),
}
