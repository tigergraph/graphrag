# Copyright (c) 2024-2026 TigerGraph, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0

"""Tests for ``common.utils.prompt_validation``."""

from __future__ import annotations

from common.utils.prompt_validation import validate_and_escape_prompt


# ---------------------------------------------------------------------------
# Required-placeholder validation
# ---------------------------------------------------------------------------


def test_chatbot_response_missing_required_returns_list():
    out, missing = validate_and_escape_prompt(
        "Hi! Just answer the {question} please.", "chatbot_response"
    )
    assert missing == ["context"]
    # The provided required placeholder is preserved.
    assert "{question}" in out


def test_chatbot_response_all_required_present_returns_empty():
    out, missing = validate_and_escape_prompt(
        "You are a helpful assistant.\n\n"
        "Context: {context}\n"
        "Question: {question}\n",
        "chatbot_response",
    )
    assert missing == []
    assert "{context}" in out and "{question}" in out


def test_community_summarization_required_set():
    template = "Summarize {entity_name} given:\n{description_list}\n"
    out, missing = validate_and_escape_prompt(template, "community_summarization")
    assert missing == []
    assert "{entity_name}" in out and "{description_list}" in out


def test_query_generation_lists_all_missing_placeholders():
    out, missing = validate_and_escape_prompt(
        "Pick a query for {question} given vertices {vertices}.",
        "query_generation",
    )
    assert set(missing) == {"conversation", "edges", "edgesInfo", "verticesAttrs"}
    # Sorted, so we can assert the exact ordering for stability.
    assert missing == sorted(missing)


def test_entity_relationship_has_no_required_placeholders():
    """``entity_relationship`` is a system-message-only prompt — its
    customizable body doesn't need any required placeholders."""
    out, missing = validate_and_escape_prompt(
        "You are a knowledge-graph extractor. Bias toward concrete nouns.",
        "entity_relationship",
    )
    assert missing == []


def test_unknown_prompt_type_passes_through_unchanged():
    """Forward-compatible: a prompt_type this module doesn't know about
    must NOT block the save (avoids fail-closed regressions when a
    new prompt type is added before this module is updated)."""
    out, missing = validate_and_escape_prompt(
        "Hello {world}!", "future_prompt_type_xyz"
    )
    assert out == "Hello {world}!"
    assert missing == []


# ---------------------------------------------------------------------------
# Stray-placeholder escaping
# ---------------------------------------------------------------------------


def test_stray_placeholders_are_double_braced():
    """Tokens that look like placeholders but aren't recognized for the
    prompt type get escaped so str.format / PromptTemplate treats them
    as literal text instead of trying to bind them."""
    template = (
        "Context: {context}\n"
        "Question: {question}\n"
        "For example: when the user asks {example_topic}, respond with "
        "{TODO_fill_in_later}.\n"
    )
    out, missing = validate_and_escape_prompt(template, "chatbot_response")
    assert missing == []
    # Recognized placeholders unchanged.
    assert "{context}" in out
    assert "{question}" in out
    # Stray placeholders escaped.
    assert "{{example_topic}}" in out
    assert "{{TODO_fill_in_later}}" in out
    # And NOT left as bare braces.
    assert "{example_topic}" not in out.replace("{{example_topic}}", "")
    assert "{TODO_fill_in_later}" not in out.replace("{{TODO_fill_in_later}}", "")


def test_already_escaped_double_braces_left_untouched():
    """``{{ident}}`` is the format-string escape for a literal
    ``{ident}``. Don't re-escape these."""
    template = "Context: {context}\nThe user types {{not_a_placeholder}}.\n"
    # Required is missing here; we still verify escaping is idempotent.
    out, _ = validate_and_escape_prompt(template, "chatbot_response")
    assert "{{not_a_placeholder}}" in out
    # Make sure we didn't escape it AGAIN to {{{{...}}}}
    assert "{{{{not_a_placeholder}}}}" not in out


def test_partial_variables_are_recognized_not_escaped():
    """``{format_instructions}`` is provided by the runtime as a
    partial — appearance in user content is fine and must not be
    escaped."""
    template = (
        "Context: {context}\n"
        "Question: {question}\n"
        "Output as: {format_instructions}\n"
    )
    out, missing = validate_and_escape_prompt(template, "chatbot_response")
    assert missing == []
    assert "{format_instructions}" in out  # not escaped


def test_escape_does_not_affect_required_placeholders_when_other_strays_present():
    template = (
        "Hi {question}.\n"
        "Use {context} for facts.\n"
        "Don't say {sensitive_word}.\n"
        "Optional: {history}\n"
    )
    out, missing = validate_and_escape_prompt(template, "chatbot_response")
    assert missing == []
    assert "{question}" in out
    assert "{context}" in out
    assert "{history}" in out  # in the "allowed partials" set
    assert "{{sensitive_word}}" in out  # stray → escaped


def test_numeric_or_empty_brace_tokens_left_alone():
    """``{}`` and ``{123}`` aren't valid Python identifiers; the regex
    requires a leading letter / underscore. They should pass through
    untouched."""
    template = (
        "Context: {context}\n"
        "Question: {question}\n"
        "Empty: {}, numeric-leading: {1abc}, full numeric: {123}.\n"
    )
    out, missing = validate_and_escape_prompt(template, "chatbot_response")
    assert missing == []
    assert "{}" in out
    assert "{1abc}" in out
    assert "{123}" in out


def test_multiline_content_with_strays():
    template = """You are a helpful assistant.

When the user asks {question}, look at:

  - The provided context: {context}
  - Optional: {history}

Examples of malformed inputs to ignore:
  {bad_input_1}
  {bad_input_2}
  {bad_input_3}

Respond as: {format_instructions}
"""
    out, missing = validate_and_escape_prompt(template, "chatbot_response")
    assert missing == []
    assert "{question}" in out and "{context}" in out
    assert "{format_instructions}" in out
    assert "{{bad_input_1}}" in out
    assert "{{bad_input_2}}" in out
    assert "{{bad_input_3}}" in out
