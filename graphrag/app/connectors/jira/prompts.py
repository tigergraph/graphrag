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


# Record types and fields of this schema that mark a question as being about
# stored records.
RECORD_WORDS = (
    "issue", "comment", "assignee", "reporter", "status", "priority",
    "resolution", "label", "component", "epic",
)


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
    "- A project key plus a number, such as ABC-123, identifies one issue. "
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
    "- An ingested (synced) issue is one whose project_key is set; that "
    "filter alone identifies it. Other JiraIssue rows, written for a link, "
    "store only issue_key and sometimes summary and url, so counts, "
    "distributions, and lists of issues filter on project_key.\n"
    "- Each structural step asks for one kind of record, because each step "
    "runs one query: the issue fields, the assignee and reporter, the "
    "comments with their authors, or the changes with their authors. Plan "
    "only the kinds the question needs. The issue-fields step asks only for "
    "JiraIssue attributes; the assignee, reporter, parent, and linked issues "
    "are steps of their own.\n"
    "- An answer about the issue as a whole needs the issue fields, the "
    "assignee and reporter, the recent comments, and a hybrid search that "
    "includes the issue key. History needs the changes. When the answer is "
    "not a stored field of the issue, add a structural step for its "
    "comments.\n"
    "- When the answer involves a release, a version, or a fix date, read "
    "fix_versions, due, status, resolution, and labels, plus the comments. "
    "When fix_versions is empty, the release evidence is the labels and the "
    "comments: merged_<version> means merged into that version, "
    "tg_<version> is only the target release, and a comment saying the fix "
    "was cancelled, not possible, or moved overrides any label. Always "
    "retrieve the comments when release information is needed.\n"
    "- Work that is in progress is status_category indeterminate; work that "
    "is not done is new or indeterminate. Ask a step for that stored value, "
    "not for statuses whose names look open or in progress.\n"
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
    "semicolon. A MATCH on a relation the issue does not have, such as a "
    "parent or a linked issue, removes the issue from the result, so add "
    "part_of or relates_to only when the step asks for them. Use the pattern "
    "for the kind of record the step asks for:\n"
    "  - issue fields: MATCH (i:JiraIssue) WHERE toLower(i.issue_key) = "
    "\"<key>\" RETURN the fields as i.<attribute> values only, with no other "
    "MATCH, list comprehension, or properties(); the step for the assignee, "
    "reporter, or parent fetches those\n"
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
    "- Jira issues about a topic, such as a feature, a product, or another "
    "tool named in the question, match that topic in the issue's summary, "
    "description, components, or labels. Do not traverse to another "
    "source's records unless the question asks for linked records.\n"
    "- Project keys listed together, such as \"ABC DEF and XYZ\", are separate "
    "keys: filter with project_key IN the list and group by project_key, "
    "returning every listed key.\n"
    "- status_category is new, indeterminate (in progress), or done; filter "
    "on these stored values, not on words like \"in progress\".\n"
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
    "- For release information, state the version from fix_versions. When that is "
    "empty, a merged_<version> label means merged into that version; a "
    "tg_<version> label is only the target; a comment saying the fix was "
    "cancelled, not possible, or moved overrides the labels. If none of "
    "these give a version, say no version is recorded; do not invent one. "
    "Lead with the answer, then give the evidence found: the fix version, "
    "the labels, and what each comment that confirms, cancels, or moves the "
    "fix says, with its date and author. Leave out comments that do not "
    "bear on the release.\n"
    "- Give a status category only when it was retrieved, and call it To Do, "
    "In Progress, or Done, not new, indeterminate, or done. Never infer a "
    "category from the status name.\n"
    "- List changes in time order and end with the current status.\n"
    "- If a bare number matches several Jira keys, list those keys and ask "
    "which issue is meant without mixing their details."
)

GUIDANCE = {"planner": PLANNER, "query": QUERY, "answer": ANSWER}

# What the stored values of these types mean, shown with the graph schema so
# that any tool that filters on them uses the right attribute and value.
FIELD_NOTES = {
    "JiraIssue": (
        "project_key is the project code, such as GLE, and the prefix of "
        "issue_key; project_name is the project's display name, which "
        "differs from the code. A row with an empty project_key is a "
        "placeholder written for a link, not an ingested issue. "
        "status_category is new, indeterminate (in progress), or done. In "
        "labels, merged_<version> marks the version a fix was merged into, and "
        "tg_<version> is only the target release."
    ),
    "JiraComment": (
        "issue_key is the issue the comment belongs to; no edge links a "
        "comment to its issue."
    ),
    "JiraChange": (
        "issue_key is the issue that changed; field is status, assignee, "
        "priority, or resolution."
    ),
}
