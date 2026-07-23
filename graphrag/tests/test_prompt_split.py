# Copyright (c) 2024-2026 TigerGraph, Inc.
#
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0).
# See the LICENSE file or <https://www.gnu.org/licenses/agpl-3.0.html>
# for the full license text.

"""Unit tests for the base_llm system/user prompt-split helpers.

Skipped where ``langchain_core`` isn't installed (e.g. a bare host); runs in the
container / CI where the LLM stack is present.
"""

from __future__ import annotations

import pytest

pytest.importorskip("langchain_core")

from common.llm_services.base_llm import LLM_Model  # noqa: E402


def _model(prompt_path="/nonexistent-prompts-dir/"):
    # Bypass __init__ (needs full config); set only what the helpers touch.
    m = LLM_Model.__new__(LLM_Model)
    m._graphname = None
    m.prompt_path = prompt_path  # no override files -> default (empty) user portion
    return m


SPLIT_PROPS = [
    "chatbot_response_prompt",
    "community_summarize_prompt",
    "entity_relationship_extraction_prompt",
    "schema_extraction_prompt",
]


def test_split_props_inject_sentinel_and_keep_placeholders():
    m = _model()
    for prop in SPLIT_PROPS:
        s = getattr(LLM_Model, prop).fget(m)
        assert "{user_prompt}" not in s, f"{prop}: sentinel leaked"
        assert "## Authority" in s, f"{prop}: guard line missing"
    cb = LLM_Model.chatbot_response_prompt.fget(m)
    for ph in ("{question}", "{context}", "{query}", "{format_instructions}"):
        assert ph in cb, f"chatbot_response lost placeholder {ph}"
    # community owns {format_instructions} now (caller stopped appending it)
    cs = LLM_Model.community_summarize_prompt.fget(m)
    assert "{entity_name}" in cs and "{description_list}" in cs and "{format_instructions}" in cs


def test_non_empty_user_portion_injected_once(tmp_path):
    # Write a per-graph-style override (here via prompt_path) with a user portion.
    d = tmp_path
    (d / "chatbot_response.txt").write_text("Always answer in one sentence.")
    m = _model(prompt_path=str(d) + "/")
    s = LLM_Model.chatbot_response_prompt.fget(m)
    assert "Always answer in one sentence." in s
    assert s.count("## Rules") == 1  # system rules appear exactly once
    assert "{user_prompt}" not in s


def test_legacy_full_prompt_override_is_ignored(tmp_path):
    # A pre-split full prompt (copies the system title) must be ignored.
    d = tmp_path
    sysp = LLM_Model._CHATBOT_RESPONSE_SYSTEM
    (d / "chatbot_response.txt").write_text(sysp.replace("{user_prompt}", "x"))
    m = _model(prompt_path=str(d) + "/")
    s = LLM_Model.chatbot_response_prompt.fget(m)
    # Rules appear once (legacy override ignored -> default empty user portion).
    assert s.count("## Rules") == 1


def test_is_legacy_detection():
    m = _model()
    sysp = LLM_Model._CHATBOT_RESPONSE_SYSTEM
    assert m._is_legacy_full_prompt("see {question} here", sysp) is True
    assert m._is_legacy_full_prompt("# AI-Powered Knowledge Graph Assistant\nx", sysp) is True
    assert m._is_legacy_full_prompt("Answer concisely. Example: revenue.", sysp) is False
    er = LLM_Model._ENTITY_RELATIONSHIP_SYSTEM  # no runtime placeholders
    assert m._is_legacy_full_prompt("# Knowledge Graph Extraction\n...", er) is True
    assert m._is_legacy_full_prompt("Prefer Bond / Issuer types.", er) is False


def test_repair_json_escapes():
    m = _model()
    assert m._repair_json_escapes(r'''{"s":"instr\'s"}''') == '{"s":"instr' + "'" + 's"}'
    # Valid escapes (incl. escaped backslash) untouched.
    assert m._repair_json_escapes(r'{"s":"a\nb\t\\x"}') == r'{"s":"a\nb\t\\x"}'


def test_get_user_portion_default_empty_when_no_file():
    m = _model()
    assert m.get_user_portion("chatbot_response.txt") == ""
