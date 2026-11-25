"""Confluence guidance added to the planner, query, and answer prompts.

prompt_selection.py adds these texts when a question names Confluence or a
Confluence page URL. They describe the schema in schema.py; keep the two in
step.
"""

from __future__ import annotations

import re

NAME = "confluence"
LABEL = "Confluence page"

_SOURCE = re.compile(r"/wiki/|/pages/|\bconfluence\b", re.IGNORECASE)
_TYPES = {"ConfluencePage", "ConfluenceComment"}


def selects(question: str) -> bool:
    return bool(_SOURCE.search(question))


def owns_type(name: str) -> bool:
    return name in _TYPES


# Record types and fields of this schema that mark a question as being about
# stored records.
RECORD_WORDS = (
    "page", "space", "comment", "attachment", "author", "editor", "owner",
    "label", "url", "link",
)

# Words that add Confluence when a question already names another source,
# e.g. a page about a Zendesk ticket or a Jira issue.
ALSO_SELECTED_BY = ("page", "wiki")

# How a bare number is looked up in Confluence when the question names no
# source.
IDENTIFIER = (
    "Confluence: the ConfluencePage whose page_id equals \"<number>\", or whose "
    "title contains \"<number>\". Return page_id, title, and space_key."
)

PLANNER = (
    "## Confluence guidance\n"
    "Applies to ConfluencePage, ConfluenceComment, Person, Organization, "
    "Attachment, and the edges authored, edited, owned, member, links_issue, "
    "and links_ticket.\n"
    "- A page is identified by its page_id or by words of its title. Match a "
    "title by case-insensitive CONTAINS on its distinctive words, never by "
    "equality, because users shorten titles.\n"
    "- Page fields are ConfluencePage attributes: page_id, content_type, "
    "title, body, status, space_key, space_name, parent_id, labels, "
    "version_number, url, created, updated. The page link is url. The page's "
    "content is body; when the answer is something the page says, such as a "
    "cause, a step, or a decision, retrieve body in a structural step and add "
    "a hybrid search that includes the page title.\n"
    "- People: the creator is authored, the last editor is edited, and the "
    "owner is owned, all from the page to Person. Comments are "
    "ConfluenceComment rows whose page_id is the page (body, comment_type, "
    "resolution, created); the comment author is authored. Child pages are "
    "ConfluencePage rows whose parent_id is the page's page_id. Files are "
    "Attachment rows whose parent_id is the page's page_id.\n"
    "- Each structural step asks for one kind of record, because each step "
    "runs one query: the page fields, the people, the comments, the child "
    "pages, the attachments, or the linked Jira issues or Zendesk tickets. "
    "Plan only the kinds the answer needs.\n"
    "- Comments, child pages, and attachments are found by the page's "
    "page_id. When the page is named by title, plan a first step that "
    "returns its page_id and title, and make the comment, child-page, or "
    "attachment step depend on it.\n"
    "- A Jira issue key or a /browse/ URL on the page is links_issue to "
    "JiraIssue. A Zendesk ticket URL is links_ticket to ZendeskTicket. A bare "
    "number in the text is not a ticket.\n"
    "- Counts, lists, and rankings over pages, spaces, authors, or comments "
    "are structural only.\n"
    "- Use word search only when the question names no field."
)

QUERY = (
    "## Confluence query guidance\n"
    "- Return named scalar attributes, never a bare vertex. For Person return "
    "display_name and email.\n"
    "- Edges, with their direction: (ConfluencePage)-[:authored]->(Person), "
    "(ConfluencePage)-[:edited]->(Person), (ConfluencePage)-[:owned]->(Person), "
    "(ConfluenceComment)-[:authored]->(Person), "
    "(Organization)-[:member]->(Person), "
    "(ConfluencePage)-[:links_issue]->(JiraIssue), "
    "(ConfluencePage)-[:links_ticket]->(ZendeskTicket).\n"
    "- Write exactly one query: one connected MATCH pattern, one RETURN, no "
    "semicolon. Use the pattern for the kind of record the step asks for:\n"
    "  - page fields: MATCH (p:ConfluencePage) WHERE toLower(p.title) CONTAINS "
    "\"<words>\" RETURN p.page_id, p.title, p.space_key, p.url, p.created, "
    "p.updated, and p.body when the content is needed\n"
    "  - creator: MATCH (p:ConfluencePage)-[:authored]->(a:Person) WHERE "
    "toLower(p.title) CONTAINS \"<words>\" RETURN a.display_name (edited for "
    "the last editor, owned for the owner)\n"
    "  - comments of a page_id from an earlier step: MATCH "
    "(c:ConfluenceComment)-[:authored]->(a:Person) WHERE c.page_id = "
    "\"<page_id>\" RETURN c.body, c.comment_type, c.resolution, c.created, "
    "a.display_name\n"
    "  - child pages of a page_id from an earlier step: MATCH "
    "(c:ConfluencePage) WHERE c.parent_id = \"<page_id>\" RETURN c.title, "
    "c.url\n"
    "  - attachments of a page_id from an earlier step: MATCH (f:Attachment) "
    "WHERE f.parent_id = \"<page_id>\" RETURN f.file_name, f.content_url\n"
    "  There is no edge from a comment, child page, or attachment to its page, "
    "so never MATCH the page together with them; first find the page_id.\n"
    "- Comment resolution values are open, reopened, resolved, and dangling, "
    "for footer and inline comments alike. An unresolved comment is open or "
    "reopened.\n"
    "- created and updated are ISO-8601 strings such as "
    "\"2025-03-31T23:10:05.000Z\". For a calendar month compare "
    "p.created >= \"2025-03-01\" AND p.created < \"2025-04-01\" (first day of "
    "the next month).\n"
    "- Rankings group by the stored attribute and return titles, not only "
    "ids. Pages per author in a date range: MATCH (p:ConfluencePage) WHERE "
    "p.created >= \"<from>\" WITH p MATCH (p)-[:authored]->(a:Person) RETURN "
    "a.display_name, count(p) AS pages ORDER BY pages DESC. Unresolved "
    "comments per page: MATCH (c:ConfluenceComment) WHERE c.resolution IN "
    "[\"open\", \"reopened\"] RETURN c.page_id, count(c) AS open_comments "
    "ORDER BY open_comments DESC, then the titles of those page_ids in a "
    "step that depends on it."
)

ANSWER = (
    "## Confluence answer guidance\n"
    "- Name pages by title (and url), never by page_id alone.\n"
    "- Quote the title, body, and comment bodies from the stored attributes. "
    "When the answer is what a page says, take it from body, not only the "
    "title. Give the page url when the page is the answer.\n"
    "- A linked Jira issue is reached by links_issue and a linked Zendesk "
    "ticket by links_ticket. Use Person.display_name and Organization.name. "
    "Do not print internal vertex ids.\n"
    "- If several pages match, list page id, title, space key, and status and "
    "ask which one. If a requested fact is not stored, say that fact is not "
    "on the page."
)

GUIDANCE = {"planner": PLANNER, "query": QUERY, "answer": ANSWER}

# What the stored values of these types mean, shown with the graph schema so
# that any tool that filters on them uses the right attribute and value.
FIELD_NOTES = {
    "ConfluencePage": (
        "page_id is the page number, stored as text. title is the page name; "
        "match it by its distinctive words. body holds the page content. "
        "space_key is the short space code and space_name its full name. "
        "parent_id is the page_id of the parent page. version_number counts "
        "edits and is stored as text. created and updated are ISO-8601 text "
        "such as 2025-03-31T23:10:05.000Z. The creator is on authored, the "
        "last editor on edited, and the owner on owned."
    ),
    "ConfluenceComment": (
        "page_id is the page the comment belongs to; no edge links a comment "
        "to its page. comment_type is footer or inline. resolution is open, "
        "reopened, resolved, or dangling; an unresolved comment is open or "
        "reopened."
    ),
}

# Confluence and Jira: links_issue from the page, written from page text.
JIRA_PAIR = {
    "planner": (
        "## Confluence and Jira relationship guidance\n"
        "- Confluence writes links_issue from the page to each Jira issue key "
        "in its title, body, or comments. Find the named record by its own "
        "identifier, follow links_issue, and retrieve the other side's fields "
        "in a separate step; add the page body when the answer is what the "
        "page says about the issue."
    ),
    "query": (
        "## Confluence and Jira relationship query guidance\n"
        "- MATCH (p:ConfluencePage)-[:links_issue]->(i:JiraIssue) WHERE "
        "toLower(i.issue_key) = \"<key>\" RETURN p.page_id, p.title, p.url, "
        "i.issue_key, i.summary, i.status."
    ),
    "answer": (
        "## Confluence and Jira relationship answer guidance\n"
        "- Name each page with its title and url, and the issue with its key "
        "and status. If no links_issue edge is stored, say no page links to "
        "that issue."
    ),
}

# Confluence and Zendesk: links_ticket from the page, written from ticket URLs.
ZENDESK_PAIR = {
    "planner": (
        "## Confluence and Zendesk relationship guidance\n"
        "- Confluence writes links_ticket from the page to each Zendesk ticket "
        "whose URL is on the page. Find the named record by its own "
        "identifier, follow links_ticket, and retrieve the other side's fields "
        "in a separate step; add the page body when the answer is what the "
        "page says about the ticket."
    ),
    "query": (
        "## Confluence and Zendesk relationship query guidance\n"
        "- MATCH (p:ConfluencePage)-[:links_ticket]->(t:ZendeskTicket) WHERE "
        "t.ticket_id = \"<number>\" RETURN p.page_id, p.title, p.url, "
        "t.ticket_id, t.subject, t.status. For a customer's tickets, start "
        "from MATCH (o:Organization)-[:member]->(r:Person)-[:requested]->"
        "(t:ZendeskTicket) and follow links_ticket from the page in a separate "
        "step."
    ),
    "answer": (
        "## Confluence and Zendesk relationship answer guidance\n"
        "- Name each page with its title and url, and the ticket with its id "
        "and subject. If no links_ticket edge is stored, say no page links to "
        "that ticket."
    ),
}
