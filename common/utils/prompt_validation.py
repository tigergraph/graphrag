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
#: Prompt types that use the system/user split: the rules + runtime
#: placeholders live in a hardcoded system prompt (base_llm), and only a
#: free-form user portion is editable. Their saved content is a user portion —
#: it has NO required placeholders and is sanitized (see ``sanitize_user_portion``)
#: rather than escaped.
SPLIT_PROMPT_TYPES: Set[str] = {
    "chatbot_response",
    "entity_relationship",
    "community_summarization",
    "schema_extraction",
}

REQUIRED_VARS_BY_PROMPT_TYPE: dict = {
    # Split prompts: the user portion has no required placeholders — the
    # runtime placeholders live in the hardcoded system prompt.
    "chatbot_response": set(),
    "entity_relationship": set(),
    "community_summarization": set(),
    "schema_extraction": set(),
    # graphrag/app/tools/map_question_to_schema.py — NOT split; still a full
    # template override, so it keeps its required placeholders.
    "query_generation": {
        "question",
        "conversation",
        "vertices",
        "verticesAttrs",
        "edges",
        "edgesInfo",
    },
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


def find_placeholders(content: str) -> List[str]:
    """Return the sorted, unique placeholder-style ``{ident}`` tokens in *content*.

    Used at save / compatibility-check time to TELL the user which tokens will be
    removed by ``sanitize_user_portion`` (the silent runtime gatekeeper strips
    them on every call; this surfaces them so the edit isn't silently altered).
    """
    return sorted(set(_PLACEHOLDER_RE.findall(content or "")))


def sanitize_user_portion(content: str) -> str:
    """Strip placeholder-style ``{ident}`` tokens from a split-prompt user portion.

    A user portion is injected into a hardcoded system prompt that owns every
    runtime placeholder, so the user portion must contain none. Any ``{ident}``
    is removed entirely — it can neither introduce a phantom placeholder nor
    re-wire a runtime variable. Double-braced ``{{...}}`` literals and bare
    ``{}`` / ``{123}`` are left untouched (``_PLACEHOLDER_RE`` doesn't match them).
    """
    return _PLACEHOLDER_RE.sub("", content)


# Phrases that signal an attempt to countermand the fixed system rules from
# within the (advisory) user portion. Targeted at *meta* overrides — language
# aimed at the rules / system / output format — to keep false positives low
# (ordinary instructions like "do not abbreviate" must not trip these).
_OVERRIDE_PATTERNS = [
    r"\bignore\b.{0,40}\b(rule|rules|instruction|instructions|above|system|prompt|guard|format|schema)\b",
    r"\bdisregard\b.{0,40}\b(rule|rules|instruction|instructions|above|system|prompt|format|schema)\b",
    r"\boverrid(?:e|es|ing)\b.{0,40}\b(rule|rules|instruction|instructions|system|prompt|format|above)\b",
    r"\bbypass\b.{0,40}\b(rule|rules|instruction|instructions|system|prompt|format|guard|above)\b",
    r"\b(?:do not|don't|never)\b.{0,40}\b(?:follow|obey|apply|adhere to)\b.{0,25}\b(rule|rules|instruction|instructions|above|system)\b",
    r"\bregardless of\b.{0,40}\b(rule|rules|instruction|instructions|format|above|system)\b",
    r"\binstead of\b.{0,40}\b(?:the rules|json|the format|the schema|the system prompt|the above)\b",
    r"\b(?:do not|don't|never|stop)\b.{0,25}\b(?:output|return|respond(?:ing)? in|produce)\b.{0,15}\bjson\b",
    r"\b(?:respond|answer|reply|output)\b.{0,15}\bin (?:plain text|prose)\b.{0,15}\b(?:not|instead of)\b.{0,10}\bjson\b",
    r"\byou (?:may|can|should)\b.{0,25}\bescape\b.{0,15}\bsingle[ -]?quote",
    r"\b(?:these|the) (?:rules|instructions) (?:do not|don't) apply\b",
]


def review_user_portion(user_portion: str) -> dict:
    """Local (no-LLM) heuristic: does a split-prompt user portion try to override
    the fixed system rules?

    The user portion is advisory — the system prompt's Authority guard already
    makes the rules win at inference time. This is a best-effort heads-up so the
    UI can tell the user which lines would be ignored, without an LLM round-trip
    on every save / restart.

    Returns ``{"has_conflict": bool, "keep": str, "remove": str, "reason": str}``:
    line-oriented, with ``remove`` the lines that match an override pattern and
    ``keep`` the rest. Subtle semantic conflicts are NOT detected here (they are
    still neutralized at runtime by the Authority guard).
    """
    text = (user_portion or "").strip()
    if not text:
        return {"has_conflict": False, "keep": "", "remove": "", "reason": ""}
    pats = [re.compile(p, re.IGNORECASE) for p in _OVERRIDE_PATTERNS]
    keep_lines: List[str] = []
    remove_lines: List[str] = []
    for line in text.splitlines():
        if line.strip() and any(p.search(line) for p in pats):
            remove_lines.append(line)
        else:
            keep_lines.append(line)
    has_conflict = bool(remove_lines)
    reason = (
        "Some lines appear to override or countermand the fixed system rules. "
        "They are advisory only and will be ignored at answer time; remove them "
        "to keep the prompt clear."
        if has_conflict else ""
    )
    return {
        "has_conflict": has_conflict,
        "keep": "\n".join(keep_lines).strip(),
        "remove": "\n".join(remove_lines).strip(),
        "reason": reason,
    }
