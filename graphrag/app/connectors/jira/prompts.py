"""Jira guidance added to the planner, query, and answer prompts.

prompt_selection.py adds these texts when a question names a Jira key or the
word "jira". They describe the schema in schema.py; keep the two in step.
"""

from __future__ import annotations

import re

NAME = "jira"
LABEL = "Jira issue"

_KEY = re.compile(r"\b[A-Za-z][A-Za-z0-9]+-\d+\b")
_WORD = re.compile(r"\bjira\b", re.IGNORECASE)


def selects(question: str) -> bool:
    return bool(_KEY.search(question) or _WORD.search(question))


def owns_type(name: str) -> bool:
    return name.startswith("Jira")


# How a bare number is looked up in Jira when the question names no source.
IDENTIFIER = (
    "Jira: JiraIssue rows whose issue_key ends with \"-<number>\" "
    "(case-insensitive). Return issue_key, summary, and status."
)

PLANNER = (
    "## Jira guidance\n"
    "Applies to JiraIssue, JiraComment, JiraChange, Person, Attachment, and "
    "the edges assigned_to, reported, authored, changed, part_of, and "
    "relates_to.\n"
    "- A project key plus a number, such as GML-2186, identifies one issue. "
    "Match issue_key case-insensitively. A follow-up that does not repeat "
    "the key refers to the issue already established in the conversation; "
    "carry that key into every retrieval step.\n"
    "- Current fields are JiraIssue attributes: issue_key, summary, "
    "description, project_key, project_name, issue_type, status, "
    "status_category, priority, resolution, labels, components, "
    "fix_versions, custom_fields, created, updated, due, url, story_points. "
    "There is no project vertex; project_key is the key prefix.\n"
    "- People: the assignee is assigned_to and the reporter is reported, both "
    "from JiraIssue to Person (display_name, email). Comments are "
    "JiraComment rows whose issue_key is the issue; the comment author is "
    "authored. Status, assignee, priority, and resolution history are "
    "JiraChange rows whose issue_key is the issue (field, from_value, "
    "to_value, created); who made the change is changed. The parent epic is "
    "part_of. Linked issues are relates_to, whose link_type holds the Jira "
    "phrase such as blocks. Files are Attachment rows whose parent_id is the "
    "issue key.\n"
    "- Synced issues have project_key and status set. Issues reached only "
    "through part_of or relates_to, or created by another connector's link, "
    "store only issue_key and sometimes summary and url. Exclude them from "
    "counts, distributions, and lists of ingested or open issues unless the "
    "question asks about links.\n"
    "- Each structural step asks for one kind of record, because each step "
    "runs one query: the issue fields, the assignee and reporter, the "
    "comments with their authors, or the changes with their authors. Plan "
    "only the kinds the question needs.\n"
    "- A summary or 'tell me about' question needs the issue fields, the "
    "assignee and reporter, the recent comments, and a hybrid search that "
    "includes the issue key. Add the changes for history questions.\n"
    "- A release, 'when will it be fixed', or 'which version' question reads "
    "fix_versions, due, status, resolution, and labels, plus the comments. "
    "When fix_versions is empty, the release evidence is the labels and the "
    "comments: merged_<version> means merged into that version, "
    "tg_<version> is only the target release, and a comment saying the fix "
    "was cancelled, not possible, or moved overrides any label. Always "
    "retrieve the comments for these questions.\n"
    "- Counts, lists, filters, and distributions over issue attributes are "
    "structural only, unless the question also needs description or comment "
    "text."
)

QUERY = (
    "## Jira query guidance\n"
    "- Return named scalar attributes, never a bare vertex. For Person return "
    "display_name and email.\n"
    "- Use an exact case-insensitive match on issue_key when the full key is "
    "known.\n"
    "- Edges, with their direction: (JiraIssue)-[:assigned_to]->(Person), "
    "(JiraIssue)-[:reported]->(Person), (JiraComment)-[:authored]->(Person), "
    "(JiraChange)-[:changed]->(Person), (JiraIssue)-[:part_of]->(JiraIssue), "
    "(JiraIssue)-[:relates_to]->(JiraIssue).\n"
    "- Write exactly one query: one connected MATCH pattern, one RETURN, no "
    "semicolon. Use the pattern for the kind of record the step asks for:\n"
    "  - issue fields: MATCH (i:JiraIssue) WHERE toLower(i.issue_key) = "
    "\"<key>\" RETURN the fields\n"
    "  - assignee: MATCH (i:JiraIssue)-[:assigned_to]->(p:Person) WHERE "
    "toLower(i.issue_key) = \"<key>\" RETURN p.display_name (reported for the "
    "reporter)\n"
    "  - comments: MATCH (c:JiraComment)-[:authored]->(p:Person) WHERE "
    "toLower(c.issue_key) = \"<key>\" RETURN c.body, c.created, "
    "p.display_name, without filtering the comment text\n"
    "  - changes: MATCH (ch:JiraChange)-[:changed]->(p:Person) WHERE "
    "toLower(ch.issue_key) = \"<key>\" RETURN ch.field, ch.from_value, "
    "ch.to_value, ch.created, p.display_name\n"
    "  There is no edge from a comment or change to its issue, so never MATCH "
    "the JiraIssue together with its comments or changes.\n"
    "- Project keys listed together, such as \"GML TSE and GLE\", are separate "
    "keys: filter with project_key IN the list and group by project_key, "
    "returning every listed key.\n"
    "- created, updated, and due are DATETIME. For a calendar month, use "
    "created >= first day of the month and created < first day of the next "
    "month.\n"
    "- An ingested (synced) issue is a JiraIssue whose project_key is not "
    "empty: WHERE i.project_key <> \"\". That filter alone decides it; do "
    "not join Document, DocumentChunk, or another source's records to "
    "decide whether an issue was ingested."
)

ANSWER = (
    "## Jira answer guidance\n"
    "- Prefer structured values for status, assignee, reporter, priority, "
    "resolution, fix versions, labels, and dates. Use passages for the "
    "description and discussion.\n"
    "- Never show internal ids such as jira:user, jira:comment, or "
    "jira:change. Use display_name, or say the name is unavailable.\n"
    "- A release answer states the version from fix_versions. When that is "
    "empty, a merged_<version> label means merged into that version; a "
    "tg_<version> label is only the target; a comment saying the fix was "
    "cancelled, not possible, or moved overrides the labels. If none of "
    "these give a version, say no version is recorded; do not invent one.\n"
    "- A history answer lists changes in time order and ends with the current "
    "status.\n"
    "- If a bare number matches several Jira keys, list those keys and ask "
    "which issue is meant without mixing their details."
)

GUIDANCE = {"planner": PLANNER, "query": QUERY, "answer": ANSWER}
