# Copyright (c) 2024-2026 TigerGraph, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0

"""Gatekeepers for user-customized prompt templates.

When a user saves a customized prompt via the *Customize Prompts* UI,
two things must hold before the file is written:

1. **Required placeholders are present.** Every prompt type has a fixed
   set of ``{var}`` tokens the calling code substitutes at runtime
   (e.g. ``community_summarization`` always interpolates
   ``{entity_name}`` and ``{description_list}``). If the user removes
   one of these, the corresponding feature breaks at the next call.
   ``validate_and_escape_prompt`` returns the missing list so the API
   can reject the save with a 400.

2. **Stray brace tokens are escaped.** Users frequently include literal
   ``{example}`` or ``{TODO}`` text in their prompts as documentation
   or examples. ``str.format`` / ``PromptTemplate`` interpret those as
   placeholders and either substitute the wrong thing or raise
   ``KeyError``. ``validate_and_escape_prompt`` rewrites any
   ``{ident}`` whose name isn't a recognized placeholder for the
   prompt type into ``{{ident}}`` so the runtime treats it as literal.

The placeholder sets are derived from ``input_variables=[…]`` at the
caller site (e.g. ``agent_generation.py``, ``community_summarizer.py``,
``map_question_to_schema.py``). Add a new entry here when a new
user-customizable prompt is wired up.
"""

from __future__ import annotations

import re
from typing import List, Set, Tuple


#: Variables every customized prompt of this type MUST contain. Derived
#: from the ``input_variables`` arguments passed to the
#: ``PromptTemplate`` / ``ChatPromptTemplate`` constructors at the call
#: sites that consume each prompt.
REQUIRED_VARS_BY_PROMPT_TYPE: dict = {
    # Used by graphrag/app/agent/agent_generation.py and the supportai
    # retrievers' final answer step.
    "chatbot_response": {"question", "context"},
    # System message in LLMEntityRelationshipExtractor — input arrives
    # via separate human messages, so the customizable prompt doesn't
    # need any required placeholders of its own.
    "entity_relationship": set(),
    # ecc/app/graphrag/community_summarizer.py.
    "community_summarization": {"entity_name", "description_list"},
    # graphrag/app/tools/map_question_to_schema.py.
    "query_generation": {
        "question",
        "conversation",
        "vertices",
        "verticesAttrs",
        "edges",
        "edgesInfo",
    },
    # common/db/schema_extraction.py.
    "schema_extraction": {"samples", "structural_types", "tg_keywords"},
    # Free-form partial injected into the four query-related templates;
    # no required placeholders — the user content IS the body.
    "query_guidance": set(),
}


#: Variables the runtime supplies as ``partial_variables`` (or via a
#: separate prompt message) — they MAY appear in the user content but
#: aren't required. Listed so the escaper doesn't double-brace them.
ALLOWED_PARTIALS_BY_PROMPT_TYPE: dict = {
    "chatbot_response": {"format_instructions", "query", "history"},
    "entity_relationship": {"format_instructions", "input"},
    "community_summarization": {"format_instructions"},
    # ``query_guidance`` is a partial the runtime supplies; allowing
    # it here keeps a user-pasted ``{query_guidance}`` from being
    # double-braced into a literal.
    "query_generation": {"format_instructions", "query_guidance"},
    "schema_extraction": set(),
    "query_guidance": set(),
}


# Match a single-brace placeholder like ``{ident}`` BUT NOT a
# double-brace ``{{ident}}`` (Python's str.format escape) and NOT
# ``{}`` / ``{123}`` (no leading letter or underscore).
#
# The negative lookbehind ``(?<!\{)`` rejects the second ``{`` of a
# ``{{`` pair; the negative lookahead ``(?!\})`` rejects the first ``}``
# of a ``}}`` pair. Both are fixed-width so the standard ``re`` module
# accepts them.
_PLACEHOLDER_RE = re.compile(
    r"(?<!\{)\{([A-Za-z_][A-Za-z0-9_]*)\}(?!\})"
)


def validate_and_escape_prompt(
    content: str,
    prompt_type: str,
) -> Tuple[str, List[str]]:
    """Run both gatekeepers on *content* for *prompt_type*.

    Returns ``(escaped_content, missing_required)`` where:

    * ``escaped_content`` is *content* with every stray ``{ident}``
      rewritten to ``{{ident}}``. Tokens whose name is in the
      required + partials set are left as-is.
    * ``missing_required`` lists the required placeholder names the
      user did NOT include. Caller should reject the save when this
      list is non-empty.

    For unknown ``prompt_type`` (e.g. a future addition that this
    module hasn't been updated for), returns ``(content, [])``
    unchanged so the save isn't blocked — better to ship a forward-
    compatible passthrough than fail-closed on a name typo.
    """
    if prompt_type not in REQUIRED_VARS_BY_PROMPT_TYPE:
        return content, []

    required: Set[str] = REQUIRED_VARS_BY_PROMPT_TYPE[prompt_type]
    allowed_partials: Set[str] = ALLOWED_PARTIALS_BY_PROMPT_TYPE.get(
        prompt_type, set()
    )
    legal: Set[str] = required | allowed_partials

    found_idents: Set[str] = set()

    def _replace(m: re.Match) -> str:
        ident = m.group(1)
        found_idents.add(ident)
        if ident in legal:
            return m.group(0)
        return "{{" + ident + "}}"

    escaped = _PLACEHOLDER_RE.sub(_replace, content)
    missing = sorted(required - found_idents)
    return escaped, missing
