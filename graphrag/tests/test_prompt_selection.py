"""Connector prompt selection: which guidance each question and step receives."""

from __future__ import annotations

import re

import pytest

from connectors import cross_source_prompts
from connectors.jira import prompts as jira_prompts
from connectors.jira.schema import jira_schema_proposal
from connectors.zendesk import prompts as zendesk_prompts
from connectors.zendesk.schema import zendesk_schema_proposal
from connectors.prompt_selection import (
    allows_type,
    bare_record_number,
    guidance_for,
    selected_connectors,
)


def test_jira_key_or_word_selects_jira():
    assert selected_connectors("Summarize GML-2186") == {"jira"}
    assert selected_connectors("summary of gml - 2186") == {"jira"}
    assert selected_connectors("open jira issues in October") == {"jira"}
    assert selected_connectors("what is our refund policy") == set()


@pytest.mark.parametrize("step", ["planner", "query", "answer"])
def test_jira_guidance_names_only_the_installed_schema(step):
    text = jira_prompts.GUIDANCE[step]
    proposal = jira_schema_proposal()
    vertices = {v.name for v in proposal.vertices}
    edges = {e.name for e in proposal.edges}
    for name in re.findall(r"\b(Jira[A-Z][A-Za-z]*)\b", text):
        assert name in vertices, f"{name} is not a Jira schema vertex"
    for name in re.findall(r"\[:([A-Za-z_]+)\]", text):
        assert name in edges, f"{name} is not a Jira schema edge"
    assert "JIRA_" not in text
    assert "JiraUser" not in text and "JiraProject" not in text


@pytest.mark.parametrize("step", ["planner", "query", "answer"])
def test_zendesk_guidance_names_only_the_installed_schema(step):
    texts = [zendesk_prompts.GUIDANCE[step], zendesk_prompts.JIRA_PAIR[step]]
    proposal = zendesk_schema_proposal({"JiraIssue"})
    vertices = {v.name for v in proposal.vertices} | {"JiraIssue"}
    edges = {e.name for e in proposal.edges}
    for text in texts:
        for name in re.findall(r"\b(Zendesk[A-Z][A-Za-z]*)\b", text):
            assert name in vertices, f"{name} is not a Zendesk schema vertex"
        for name in re.findall(r"\[:([A-Za-z_]+)\]", text):
            assert name in edges, f"{name} is not a Zendesk schema edge"
        assert "ZendeskUser" not in text and "ZendeskTicketRef" not in text


def test_zendesk_and_jira_relation_uses_links_issue():
    query = guidance_for("Does Zendesk ticket 24680 exist in Jira?", "query")
    assert "## Zendesk query guidance" in query
    assert "MATCH (t:ZendeskTicket)-[:links_issue]->(i:JiraIssue)" in query
    month = guidance_for("How many Zendesk tickets were created in March 2025?", "query")
    assert "(first day of the next month)" in month
    assert "deleted" in month


def test_jira_guidance_is_added_to_each_step_with_the_shared_rules():
    planner = guidance_for("When will ABC-101 be fixed?", "planner")
    assert "## Jira guidance" in planner
    assert "merged_<version>" in planner
    assert "An empty result is the answer" in planner
    query = guidance_for("How many jira issues were created in March 2025?", "query")
    assert "returns only the count, for example RETURN" in query
    assert "project_key is not empty" in query
    answer = guidance_for("Summarize ABC-102", "answer")
    assert "documentation, not record data" in answer


def test_query_guidance_asks_for_one_query_without_optional_match():
    query = guidance_for("Summarize ABC-102.", "query")
    assert "Write exactly one query" in query
    assert "no semicolon" in query
    assert "Do not use OPTIONAL MATCH" in query
    assert "WHERE i.project_key <> \"\"" in query


def test_field_questions_are_structural_and_summaries_add_hybrid():
    planner = guidance_for("Status of ABC-103?", "planner")
    assert "use structural steps only. Do not add a hybrid search" in planner
    summary = guidance_for("Summarize ABC-102.", "planner")
    assert "plus a hybrid search that includes the record's identifier" in summary
    assert "a hybrid search that includes the issue key" in summary


def test_a_bare_number_is_looked_up_in_every_source_before_asking():
    question = "Status and plan for #12345."
    assert selected_connectors(question) == set()
    assert bare_record_number(question) == "12345"
    planner = guidance_for(question, "planner")
    assert "12345" in planner
    assert 'issue_key ends with "-12345"' in planner
    assert "Plan one structural step for each source" in planner
    assert "## Jira guidance" in planner
    assert "add for each source the steps for the related records" in planner
    assert "ask which" in guidance_for(question, "answer")
    assert "graph_queries_with_no_rows" in guidance_for(question, "answer")


def test_a_year_is_not_a_record_number():
    assert bare_record_number("How many tickets were created in March 2025?") is None
    assert bare_record_number("Summarize 67890.") == "67890"
    assert bare_record_number("Summarize GML-2186") is None


def test_a_record_question_without_a_number_or_source_asks_which_source():
    text = guidance_for("What is the status of the ticket?", "planner")
    assert "Ask the user which source" in text
    assert "only when the question is about one record" in text


def test_a_data_question_naming_no_source_gets_every_connector_and_shared_rule():
    workload = guidance_for("Break down open issues by priority and by status.", "planner")
    assert "## Source guidance" in workload
    assert "## Jira guidance" in workload
    assert "one structural step per breakdown" in workload
    assert guidance_for("What is our refund policy?", "planner") == ""
    # Query rules hold for any structural query, whatever the question names.
    assert "Match a topic, title, subject, or summary word by word" in guidance_for(
        "What is our refund policy?", "query")
    customer = guidance_for("Summarize the tickets of customer Acme.", "planner")
    assert "## Zendesk guidance" in customer
    assert "A customer's records are the tickets requested by members" in customer


def test_relation_guidance_uses_installed_pairs(monkeypatch):
    # One source: only pairs whose connectors are all installed.
    assert cross_source_prompts.guidance_for(
        {"jira"}, "is ABC-1 linked", "planner", installed={"jira"}
    ) == ""
    assert "Jira and Zendesk" in cross_source_prompts.guidance_for(
        {"jira"}, "is ABC-1 linked", "planner", installed={"jira", "zendesk"}
    )
    text = cross_source_prompts.guidance_for(
        {"jira", "zendesk"}, "Does this Zendesk ticket exist in Jira?", "planner"
    )
    assert "Cross-source guidance" in text
    assert "only when the graph stores an edge" in text
    monkeypatch.setitem(
        cross_source_prompts.PAIRS,
        frozenset({"jira", "zendesk"}),
        {"planner": "Follow links_issue from ZendeskTicket to JiraIssue."},
    )
    text = cross_source_prompts.guidance_for(
        {"jira", "zendesk"}, "Which Jira issue is linked to Zendesk ticket 24680?", "planner"
    )
    assert "Follow links_issue" in text
    assert cross_source_prompts.guidance_for(
        {"jira", "zendesk"}, "Count open items", "planner"
    ) == ""


def test_jira_questions_keep_shared_and_jira_types_only():
    assert allows_type("JiraIssue", {"jira"})
    assert allows_type("Person", {"jira"})
    assert allows_type("DocumentChunk", {"jira"})
    assert not allows_type("ConfluencePage", {"jira"})
    assert allows_type("ZendeskTicket", {"jira"})
    assert allows_type("ZendeskComment", {"zendesk"})
    assert allows_type("ConfluencePage", set())


def test_query_rules_apply_to_every_structural_query():
    query = guidance_for("What is our refund policy?", "query")
    assert "Match a topic, title, subject, or summary word by word" in query
    assert "Filter by date first, then traverse" in query
    assert guidance_for("What is our refund policy?", "planner") == ""


def test_jira_status_categories_are_the_stored_values():
    query = guidance_for("Which jira issues are in progress?", "query")
    assert "status_category is new, indeterminate (in progress), or done" in query


def test_a_step_can_depend_on_an_identifier_found_earlier():
    planner = guidance_for("Who reported ABC-102 and what did they comment?", "planner")
    assert "list that step in depends_on" in planner


def test_zendesk_counts_filter_dates_before_traversing():
    query = guidance_for("How many Zendesk tickets did each assignee get in March 2025?", "query")
    assert "WITH t MATCH (p:Person)-[:assigned]->(t)" in query
    assert "tags is a reserved word" in query
