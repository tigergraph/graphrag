"""Connector prompt selection: which guidance each question and step receives."""

from __future__ import annotations

import re

import pytest

from connectors import cross_source_prompts
from connectors.jira import prompts as jira_prompts
from connectors.jira.schema import jira_schema_proposal
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


def test_jira_guidance_is_added_to_each_step_with_the_shared_rules():
    planner = guidance_for("When will GLE-13162 be fixed?", "planner")
    assert "## Jira guidance" in planner
    assert "merged_<version>" in planner
    assert "An empty result is the answer" in planner
    query = guidance_for("How many jira issues were created in October 2026?", "query")
    assert "returns one row with the count, 0 when nothing" in query
    assert "project_key is not empty" in query
    answer = guidance_for("Summarize TSE-1073", "answer")
    assert "documentation, not record data" in answer


def test_query_guidance_asks_for_one_query_without_optional_match():
    query = guidance_for("Summarize TSE-1073.", "query")
    assert "Write exactly one query" in query
    assert "no semicolon" in query
    assert "Do not use OPTIONAL MATCH" in query
    assert "WHERE i.project_key <> \"\"" in query


def test_field_questions_are_structural_and_summaries_add_hybrid():
    planner = guidance_for("Give the current status of GML-2348.", "planner")
    assert "uses structural steps only. Do not add a hybrid search" in planner
    summary = guidance_for("Summarize TSE-1073.", "planner")
    assert "plus a hybrid search for its description and discussion text" in summary
    assert "a hybrid search that includes the issue key" in summary


def test_a_bare_number_is_looked_up_in_every_source_before_asking():
    question = "Current status and the next steps for #58617."
    assert selected_connectors(question) == set()
    assert bare_record_number(question) == "58617"
    planner = guidance_for(question, "planner")
    assert "58617" in planner
    assert 'issue_key ends with "-58617"' in planner
    assert "do not ask first" in planner
    assert "ask which" in guidance_for(question, "answer")


def test_a_year_is_not_a_record_number():
    assert bare_record_number("How many tickets were created in October 2026?") is None
    assert bare_record_number("Summarize 59224.") == "59224"
    assert bare_record_number("Summarize GML-2186") is None


def test_a_record_question_without_a_number_or_source_asks_which_source():
    text = guidance_for("What is the status of the ticket?", "planner")
    assert "Ask the user which source" in text


def test_relation_guidance_needs_two_sources(monkeypatch):
    assert cross_source_prompts.guidance_for({"jira"}, "is GML-1 linked", "planner") == ""
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
        {"jira", "zendesk"}, "Which Jira issue is linked to ticket 59038?", "planner"
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
    assert allows_type("ConfluencePage", set())
