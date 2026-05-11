# Copyright (c) 2024-2026 TigerGraph, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0

"""End-to-end test for the customizable-prompt round-trip.

Stages:
    1. GET ``/ui/prompts`` returns the in-code default for every
       UI-editable prompt; ``editable_content`` is non-empty and
       contains zero ``{placeholder}`` occurrences; placeholders the
       prompt requires live exclusively in ``template_variables``.
    2. POST ``/ui/prompts`` saves a customized ``chatbot_response``;
       a fresh GET returns the customized text (still with
       placeholders hidden).
    3. POST ``/ui/prompts`` reverts ``chatbot_response`` to the
       original; GET returns the original again.
    4. Same load → save → revert flow for ``schema_extraction``.

Requires a live GraphRAG service; ``GRAPHRAG_URL`` env enables the
suite (default ``http://localhost:80``). Test runs against the global
scope (no graphname); per-graph overrides are exercised separately.

Default credentials: ``tigergraph`` / ``tigergraph``. Override via
``TG_USERNAME`` / ``TG_PASSWORD`` env if your TG instance differs.
"""

from __future__ import annotations

import os
import re

import pytest
import requests


GRAPHRAG_URL = os.getenv("GRAPHRAG_URL", "http://localhost:80")
USERNAME = os.getenv("TG_USERNAME", "tigergraph")
PASSWORD = os.getenv("TG_PASSWORD", "tigergraph")
AUTH = (USERNAME, PASSWORD)

# Prompt types the UI exposes through ``/ui/prompts``. Every entry here
# must round-trip through GET → POST → GET → revert.
EDITABLE_PROMPT_TYPES = (
    "chatbot_response",
    "entity_relationship",
    "community_summarization",
    "query_generation",
    "schema_extraction",
)

# Required placeholders per prompt type — these MUST appear in the
# template_variables block returned by GET /prompts. ``entity_relationship``
# is the system-message prompt and has no required placeholders.
REQUIRED_PLACEHOLDERS = {
    "chatbot_response": {"question", "context", "format_instructions"},
    "entity_relationship": set(),
    "community_summarization": {"entity_name", "description_list"},
    "query_generation": {
        "question", "conversation",
        "vertices", "verticesAttrs",
        "edges", "edgesInfo",
    },
    "schema_extraction": {"samples", "structural_types", "tg_keywords"},
}


skip_unless_graphrag = pytest.mark.skipif(
    not os.getenv("GRAPHRAG_URL"),
    reason="E2E tests require a live GraphRAG service. Set GRAPHRAG_URL to run.",
)


_PLACEHOLDER_RE = re.compile(r"(?<!\{)\{([A-Za-z_][A-Za-z0-9_]*)\}(?!\})")


def _placeholder_set(text: str) -> set:
    """Return single-brace ``{ident}`` placeholders, ignoring escaped
    ``{{ident}}`` literals.
    """
    return set(_PLACEHOLDER_RE.findall(text or ""))


# Shared state across ordered stages.
_state: dict = {}


@skip_unless_graphrag
def test_01_get_returns_defaults_with_placeholders_hidden():
    """Every editable prompt resolves to a non-empty default; the
    editable portion is placeholder-free; required placeholders all
    live in template_variables.
    """
    print("\n--- Stage 1: GET defaults; verify placeholder split ---")
    resp = requests.get(f"{GRAPHRAG_URL}/ui/prompts", auth=AUTH, timeout=180)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    prompts = body.get("prompts", {})

    originals: dict = {}
    for ptype in EDITABLE_PROMPT_TYPES:
        assert ptype in prompts, f"GET /ui/prompts missing {ptype!r}"
        entry = prompts[ptype]
        editable = entry.get("editable_content", "")
        template_vars = entry.get("template_variables", "")
        assert editable, f"{ptype}: empty editable_content (expected in-code default)"

        placeholders_in_editable = _placeholder_set(editable)
        assert not placeholders_in_editable, (
            f"{ptype}: placeholders leaked into editable_content: "
            f"{sorted(placeholders_in_editable)}"
        )

        required = REQUIRED_PLACEHOLDERS[ptype]
        if required:
            placeholders_in_tv = _placeholder_set(template_vars)
            missing = required - placeholders_in_tv
            assert not missing, (
                f"{ptype}: required placeholders missing from "
                f"template_variables: {sorted(missing)}"
            )

        originals[ptype] = entry
        print(
            f"  {ptype}: editable={len(editable)}b, "
            f"template={len(template_vars)}b, "
            f"hidden={sorted(_placeholder_set(template_vars))}"
        )

    _state["originals"] = originals


@skip_unless_graphrag
def test_02_save_customized_chatbot_response_round_trips():
    """Saving a customized chatbot_response prompt persists it; a
    follow-up GET returns the customized text with placeholders still
    hidden.

    Wrapped in try/except so a mid-flight assertion failure still
    reverts the file, instead of leaving the test-marker in
    ``configs/prompts/chatbot_response.txt`` for every later run.
    Stage 3 reverts again as its primary action; doing both is
    idempotent.
    """
    if "originals" not in _state:
        pytest.skip("Skipped because Stage 1 did not capture originals")
    print("\n--- Stage 2: customize chatbot_response; verify round-trip ---")

    original = _state["originals"]["chatbot_response"]
    custom_marker = "[E2E TEST EDIT — chatbot_response]"
    new_editable = f"{custom_marker}\n\n{original['editable_content']}"

    saved = False
    try:
        resp = requests.post(
            f"{GRAPHRAG_URL}/ui/prompts",
            json={
                "prompt_type": "chatbot_response",
                "editable_content": new_editable,
                "template_variables": original["template_variables"],
            },
            auth=AUTH,
            timeout=180,
        )
        assert resp.status_code == 200, resp.text
        saved = True

        resp = requests.get(f"{GRAPHRAG_URL}/ui/prompts", auth=AUTH, timeout=180)
        assert resp.status_code == 200, resp.text
        after = resp.json()["prompts"]["chatbot_response"]
        assert custom_marker in after["editable_content"], (
            "Customized marker missing from chatbot_response after save+reload"
        )
        placeholders_in_editable = _placeholder_set(after["editable_content"])
        assert not placeholders_in_editable, (
            f"Placeholders leaked into editable_content after customize: "
            f"{sorted(placeholders_in_editable)}"
        )
        required = REQUIRED_PLACEHOLDERS["chatbot_response"]
        placeholders_in_tv = _placeholder_set(after["template_variables"])
        missing = required - placeholders_in_tv
        assert not missing, (
            f"Required placeholders dropped during round-trip: {sorted(missing)}"
        )
        _state["chatbot_customized"] = True
    except BaseException:
        if saved:
            try:
                requests.post(
                    f"{GRAPHRAG_URL}/ui/prompts",
                    json={
                        "prompt_type": "chatbot_response",
                        "editable_content": original["editable_content"],
                        "template_variables": original["template_variables"],
                    },
                    auth=AUTH,
                    timeout=180,
                )
            except Exception as revert_exc:
                print(f"  chatbot_response revert failed: {revert_exc}")
        raise


@skip_unless_graphrag
def test_03_revert_chatbot_response_to_original():
    """Saving the original ``editable_content`` back removes the
    customization.
    """
    if not _state.get("chatbot_customized"):
        pytest.skip("Skipped — Stage 2 did not customize")
    print("\n--- Stage 3: revert chatbot_response to original ---")

    original = _state["originals"]["chatbot_response"]
    resp = requests.post(
        f"{GRAPHRAG_URL}/ui/prompts",
        json={
            "prompt_type": "chatbot_response",
            "editable_content": original["editable_content"],
            "template_variables": original["template_variables"],
        },
        auth=AUTH,
        timeout=180,
    )
    assert resp.status_code == 200, resp.text

    resp = requests.get(f"{GRAPHRAG_URL}/ui/prompts", auth=AUTH, timeout=180)
    assert resp.status_code == 200, resp.text
    after = resp.json()["prompts"]["chatbot_response"]
    custom_marker = "[E2E TEST EDIT — chatbot_response]"
    assert custom_marker not in after["editable_content"], (
        "Customization marker survived revert"
    )


@skip_unless_graphrag
def test_04_save_customized_schema_extraction_round_trips():
    """Same round-trip flow for schema_extraction (the prompt with
    the largest set of required placeholders / structural-context
    template variables).

    Wrapped in try/finally so a failed assertion mid-flight always
    reverts to the original — otherwise the marker leaks into
    ``configs/prompts/schema_extraction.txt`` and pollutes every
    subsequent extraction call.
    """
    if "originals" not in _state:
        pytest.skip("Skipped because Stage 1 did not capture originals")
    print("\n--- Stage 4: customize schema_extraction; verify round-trip ---")

    original = _state["originals"]["schema_extraction"]
    custom_marker = "[E2E TEST EDIT — schema_extraction]"
    new_editable = f"{custom_marker}\n\n{original['editable_content']}"

    saved = False
    try:
        resp = requests.post(
            f"{GRAPHRAG_URL}/ui/prompts",
            json={
                "prompt_type": "schema_extraction",
                "editable_content": new_editable,
                "template_variables": original["template_variables"],
            },
            auth=AUTH,
            timeout=180,
        )
        assert resp.status_code == 200, resp.text
        saved = True

        resp = requests.get(f"{GRAPHRAG_URL}/ui/prompts", auth=AUTH, timeout=180)
        assert resp.status_code == 200, resp.text
        after = resp.json()["prompts"]["schema_extraction"]
        assert custom_marker in after["editable_content"], (
            "Customized marker missing from schema_extraction after save+reload"
        )
        placeholders_in_editable = _placeholder_set(after["editable_content"])
        assert not placeholders_in_editable, (
            f"Placeholders leaked into editable_content after customize: "
            f"{sorted(placeholders_in_editable)}"
        )
        required = REQUIRED_PLACEHOLDERS["schema_extraction"]
        placeholders_in_tv = _placeholder_set(after["template_variables"])
        missing = required - placeholders_in_tv
        assert not missing, (
            f"Required placeholders dropped during round-trip: {sorted(missing)}"
        )
    finally:
        if saved:
            try:
                requests.post(
                    f"{GRAPHRAG_URL}/ui/prompts",
                    json={
                        "prompt_type": "schema_extraction",
                        "editable_content": original["editable_content"],
                        "template_variables": original["template_variables"],
                    },
                    auth=AUTH,
                    timeout=180,
                )
            except Exception as exc:
                print(f"  schema_extraction revert failed: {exc}")


@skip_unless_graphrag
def test_05_post_rejects_missing_required_placeholders():
    """Saving a query_generation prompt that drops a required
    placeholder must return 400 — the server-side validator catches
    it before persisting.
    """
    if "originals" not in _state:
        pytest.skip("Skipped because Stage 1 did not capture originals")
    print("\n--- Stage 5: validator rejects missing required placeholder ---")

    original = _state["originals"]["query_generation"]
    # Strip the placeholder block entirely — the validator should
    # reject because required placeholders (e.g. {question}) are gone.
    resp = requests.post(
        f"{GRAPHRAG_URL}/ui/prompts",
        json={
            "prompt_type": "query_generation",
            "editable_content": original["editable_content"],
            "template_variables": "",
        },
        auth=AUTH,
        timeout=180,
    )
    assert resp.status_code == 400, (
        f"Expected 400 for missing-placeholder save, got {resp.status_code}: {resp.text}"
    )
    detail = (resp.json() or {}).get("detail", "").lower()
    assert "missing" in detail or "placeholder" in detail, (
        f"Expected error detail to mention missing placeholders, got: {detail}"
    )
