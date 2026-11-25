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
    agent_guidance,
    allows_type,
    bare_record_number,
    guidance_for,
    schema_notes,
    selected_connectors,
    selection_text,
    user_question,
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
    assert "add for every source, not only the first, the" in planner
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


def test_an_agent_gets_the_planner_query_and_answer_rules_together():
    text = agent_guidance("How many Jira issues were created in March 2025?")
    assert text.startswith("## Connector guidance")
    assert "A structural step is one structured lookup: query_graph" in text
    for heading in ("## Jira guidance", "## Jira query guidance", "## Jira answer guidance",
                    "## Record question guidance", "## Record query guidance"):
        assert heading in text
    assert agent_guidance("What is our refund policy?") == ""


def test_field_notes_cover_only_the_connector_types_on_the_graph():
    notes = schema_notes(["JiraIssue", "JiraComment", "Document"])
    assert notes.startswith("Field notes from the data source connectors:")
    assert "- JiraIssue: project_key is the project code" in notes
    assert "merged_<version>" in notes
    assert "- JiraComment:" in notes
    assert "JiraChange" not in notes
    assert schema_notes(["Document", "DocumentChunk"]) == ""


def test_query_guidance_follows_the_users_question_inside_an_agent_call():
    narrow = "Count JiraIssue vertices created in March 2025 per project"
    assert "## Jira query guidance" not in guidance_for(selection_text(narrow), "query")
    with user_question("How are ingested Jira issues distributed by status?"):
        text = guidance_for(selection_text(narrow), "query")
    assert "## Jira query guidance" in text
    assert selection_text(narrow) == narrow


def test_the_issue_fields_step_fetches_no_related_records():
    planner = guidance_for("Summarize ABC-102.", "planner")
    assert "The issue-fields step asks only for JiraIssue attributes" in planner
    query = guidance_for("Summarize ABC-102.", "query")
    assert "no other MATCH, list comprehension, or properties()" in query


def test_answers_use_the_users_terms_and_name_the_release_evidence():
    answer = guidance_for("When will ABC-101 be fixed?", "answer")
    assert "not by attribute name such as status_category or project_key" in answer
    assert "query column names such as i_created" in answer
    assert "never show vertex or edge type names" in answer
    assert "Lead with the answer, then give the evidence found" in answer
    assert "Never infer a category from the status name" in answer
    assert "Work that is in progress is status_category indeterminate" in guidance_for("Which ABC issues are in progress?", "planner")
    agent = agent_guidance("Summarize ABC-102.")
    assert "Answer only what the question asks" in agent
    assert "not with an offer of more lookups" in agent


def test_numbers_come_from_the_query_and_topics_match_issue_text():
    answer = guidance_for("How many ABC issues are open?", "answer")
    assert "Never count, group, or total listed rows yourself" in answer
    assert "without explaining how they map" in answer
    query = guidance_for("Which jira issues for the billing feature are open?", "query")
    assert "Do not traverse to another source's records" in query
    release = guidance_for("When will ABC-101 be fixed?", "answer")
    assert "Leave out comments that do not bear on the release" in release


def test_a_link_named_only_in_text_is_not_called_confirmed():
    answer = guidance_for("Which ABC issues also show up in support tickets?", "answer")
    assert "Only a stored edge confirms a link between records" in answer
    assert "Do not call such a pair confirmed or verified" in answer
    assert 'never close with a line such as "If you want' in agent_guidance("Summarize ABC-102.")


def test_zendesk_field_notes_are_shown_with_the_schema():
    notes = schema_notes(["ZendeskTicket", "ZendeskComment", "JiraIssue"])
    assert "- ZendeskTicket: ticket_id is the ticket number" in notes
    assert "A row with an empty status is a placeholder" in notes
    assert "- ZendeskComment:" in notes
    assert "- JiraIssue:" in notes
    assert "ZendeskChange" not in notes


def test_a_zendesk_question_gives_the_agent_the_zendesk_rules():
    text = agent_guidance("Summarize zendesk #12345.")
    assert "## Zendesk guidance" in text
    assert "## Zendesk query guidance" in text
    assert "## Zendesk answer guidance" in text


def test_an_unnamed_number_plans_related_steps_for_every_source():
    planner = guidance_for("What are the next steps for #12345?", "planner")
    assert "add for every source, not only the first" in planner
    assert "includes each source's comments step" in planner


def test_zendesk_counts_cover_ingested_tickets_only():
    planner = guidance_for("How are zendesk tickets distributed by status?", "planner")
    assert "An ingested ticket is one whose status is set" in planner
    query = guidance_for("How are zendesk tickets distributed by status?", "query")
    assert 'WHERE t.status <> ""' in query


def test_answers_state_conditions_in_words_and_stay_short():
    answer = guidance_for("How many zendesk tickets are closed?", "answer")
    assert "This holds for every sentence" in answer
    assert "never quote a question that was put to the graph" in answer
    assert "even when a comment is signed with another form of the name" in answer
    assert "stays under about 300 words" in guidance_for("What went wrong for zendesk customer ACME?", "answer")
    assert "never in the answer text" in agent_guidance("What went wrong for zendesk customer ACME?")
    assert "a similar subject is not a link" in guidance_for("List the linked tickets for zendesk #12345.", "answer")
