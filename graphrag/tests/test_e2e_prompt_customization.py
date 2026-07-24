# Copyright (c) 2024-2026 TigerGraph, Inc.
#
# This program may be redistributed and/or modified under the terms of the GNU
# Affero General Public License as published by the Free Software Foundation,
# either version 3 of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE. See the GNU Affero General Public License for more
# details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.

"""End-to-end test for the system/user prompt-split round-trip.

Model under test: each split prompt is a hardcoded system prompt (rules +
runtime placeholders) plus a user-editable portion injected at ``{user_prompt}``.
The prompts API only ever exposes/saves the *user portion* — never the system
rules.

Stages:
    1. GET ``/ui/prompts`` returns, for each split prompt, an ``editable_content``
       that is the user portion only: it contains zero ``{placeholder}`` tokens,
       carries no ``template_variables``, and does not leak the system rules.
    2. POST a custom user portion (marker + a stray ``{placeholder}``); a fresh
       GET returns the marker with the placeholder stripped.
    3. Revert by POSTing an empty user portion; GET returns it placeholder-free.

Requires a live GraphRAG service; ``GRAPHRAG_URL`` enables the suite (default
``http://localhost:80``). Runs against the global scope. Default credentials:
``tigergraph`` / ``tigergraph`` (override via ``TG_USERNAME`` / ``TG_PASSWORD``).
"""

from __future__ import annotations

import os
import re

import pytest
import requests


GRAPHRAG_URL = os.getenv("GRAPHRAG_URL", "http://localhost:80")
AUTH = (os.getenv("TG_USERNAME", "tigergraph"), os.getenv("TG_PASSWORD", "tigergraph"))

# Split prompts: their saved content is a user portion only.
SPLIT_PROMPT_TYPES = (
    "chatbot_response",
    "entity_relationship",
    "community_summarization",
    "schema_extraction",
)

# A distinctive phrase from each prompt's hardcoded system rules — it must NOT
# appear in the user portion the API returns (proves the rules aren't exposed).
SYSTEM_RULE_MARKER = {
    "chatbot_response": "AI-Powered Knowledge Graph Assistant",
    "entity_relationship": "top-tier algorithm",
    "community_summarization": "comprehensive summary",
    "schema_extraction": "schema architect",
}

skip_unless_graphrag = pytest.mark.skipif(
    not os.getenv("GRAPHRAG_URL"),
    reason="E2E tests require a live GraphRAG service. Set GRAPHRAG_URL to run.",
)

_PLACEHOLDER_RE = re.compile(r"(?<!\{)\{([A-Za-z_][A-Za-z0-9_]*)\}(?!\})")


def _placeholder_set(text: str) -> set:
    return set(_PLACEHOLDER_RE.findall(text or ""))


def _get_prompts() -> dict:
    r = requests.get(f"{GRAPHRAG_URL}/ui/prompts", auth=AUTH, timeout=30)
    r.raise_for_status()
    return r.json()["prompts"]


def _save(prompt_type: str, editable_content: str):
    r = requests.post(
        f"{GRAPHRAG_URL}/ui/prompts",
        json={"prompt_type": prompt_type, "editable_content": editable_content},
        auth=AUTH,
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


_MARKER = "E2E_PROMPT_SPLIT_MARKER_42"


@skip_unless_graphrag
def test_01_get_returns_user_portion_only():
    print("\n--- Stage 1: GET exposes user portion only (no system rules) ---")
    prompts = _get_prompts()
    for ptype in SPLIT_PROMPT_TYPES:
        assert ptype in prompts, f"{ptype} missing from GET /ui/prompts"
        entry = prompts[ptype]
        editable = entry.get("editable_content", "")
        # User portion carries no runtime placeholders.
        assert _placeholder_set(editable) == set(), (
            f"{ptype}: placeholders leaked into editable_content: "
            f"{sorted(_placeholder_set(editable))}"
        )
        # template_variables is obsolete for split prompts.
        assert not entry.get("template_variables"), (
            f"{ptype}: unexpected template_variables in response"
        )
        # The hardcoded system rules must not be exposed.
        assert SYSTEM_RULE_MARKER[ptype] not in editable, (
            f"{ptype}: system rules leaked into editable_content"
        )
        print(f"  {ptype}: OK (user portion len={len(editable)})")


@skip_unless_graphrag
def test_02_save_user_portion_strips_placeholders_and_round_trips():
    print("\n--- Stage 2: save user portion; placeholder stripped ---")
    _save("chatbot_response", f"{_MARKER}\nQuote {{question}} exactly and stay terse.")
    after = _get_prompts()["chatbot_response"]["editable_content"]
    assert _MARKER in after, "custom user portion did not round-trip"
    assert "{question}" not in after, "placeholder was not stripped on save"
    assert _placeholder_set(after) == set()
    print("  chatbot_response: marker present, placeholder stripped")


@skip_unless_graphrag
def test_03_revert_user_portion_to_empty():
    print("\n--- Stage 3: revert user portion to empty ---")
    _save("chatbot_response", "")
    after = _get_prompts()["chatbot_response"]["editable_content"]
    assert _MARKER not in after, "revert did not clear the custom user portion"
    assert _placeholder_set(after) == set()
    print("  chatbot_response: reverted to default (empty user portion)")
