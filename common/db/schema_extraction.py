# Copyright (c) 2024-2026 TigerGraph, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0

"""Schema-extraction over sample documents (Phase 1, sample-doc path).

The endpoint accepts up to N representative documents, this module
turns them into a single concatenated markdown blob and asks the LLM
to emit ``VERTEX`` / ``DIRECTED EDGE`` / ``UNDIRECTED EDGE``
statements (the same GSQL form the *paste* path accepts), so both
sources funnel through ``schema_utils.parse_gsql_schema``.

Prompt loading is delegated to
``common.llm_services.base_llm.LLM_Model.schema_extraction_prompt`` —
the same per-graph-override → provider-default resolution used by every
other customizable prompt. The prompt itself lives at
``<prompt_path>/schema_extraction.txt`` with a per-graph override at
``configs/graph_configs/<graphname>/prompts/schema_extraction.txt``.
"""

from __future__ import annotations

import logging
from typing import Iterable, List, Optional

from langchain.prompts import PromptTemplate
from langchain_core.output_parsers import StrOutputParser

from common.db.schema_utils import (
    GRAPHRAG_STRUCTURAL_EDGE_TYPES,
    GRAPHRAG_STRUCTURAL_VERTEX_TYPES,
    get_gsql_reserved_words,
)

logger = logging.getLogger(__name__)


# Specific known model builds → context window in tokens. Matched by
# longest-prefix substring against the lowercased ``llm_model`` value.
# When a configured model hits this table, no warning is logged.
_MODEL_CONTEXT_TOKENS = {
    # Anthropic Claude — Opus 4.7 1M is keyed first so its longer prefix
    # wins over the 200K Opus 4.x default.
    "claude-opus-4-7": 1_000_000,
    "claude-opus-4": 200_000,
    "claude-sonnet-4": 200_000,
    "claude-haiku-4": 200_000,
    "claude-3-5-sonnet": 200_000,
    "claude-3-5-haiku": 200_000,
    "claude-3-opus": 200_000,
    "claude-3-sonnet": 200_000,
    "claude-3-haiku": 200_000,
    # OpenAI GPT-4
    "gpt-4o": 128_000,
    "gpt-4-turbo": 128_000,
    "gpt-4-1106": 128_000,
    "gpt-4-0125": 128_000,
    "gpt-4-32k": 32_000,
    "gpt-4": 8_000,
    # OpenAI GPT-3.5
    "gpt-3.5-turbo-16k": 16_000,
    "gpt-3.5-turbo": 16_000,
    "gpt-3.5": 4_000,
    # Google Gemini
    "gemini-1.5-pro": 1_000_000,
    "gemini-1.5-flash": 1_000_000,
    "gemini-1.0-pro": 32_000,
    # Meta Llama
    "llama-3.1": 128_000,
    "llama-3": 8_000,
    "llama-2": 4_000,
}
# Family-level fallbacks for unknown variants. When the specific-build
# table misses, the first matching family is used and a warning is
# logged so the operator knows the value is a guess.
_FAMILY_FALLBACK_TOKENS = [
    ("claude", 200_000),
    ("gpt-4", 128_000),
    ("gpt-3.5", 16_000),
    ("gpt", 128_000),       # unknown gpt-* — assume modern
    ("gemini", 1_000_000),
    ("llama", 128_000),     # unknown llama — assume modern
    ("mistral", 32_000),
    ("mixtral", 32_000),
    ("deepseek", 128_000),
    ("qwen", 32_000),
    ("titan", 32_000),
    ("cohere", 128_000),
    ("nova", 128_000),
]
_DEFAULT_CONTEXT_TOKENS_FALLBACK = 128_000
# Tokens reserved for the prompt template, structural-types list, the
# reserved-words list, and the LLM's output. The remaining budget is
# spent on sample content.
_PROMPT_OVERHEAD_TOKENS = 4_000
# Lower bound so unknown / tiny-context models still get *something*.
_MIN_SAMPLE_TOKENS = 1_000
# Approximation used to convert the resolved token budget into the
# character budget that ``concatenate_samples`` consumes. English
# markdown averages ~4 chars/token.
_CHARS_PER_TOKEN = 4


def _default_context_tokens(model_name: Optional[str]) -> int:
    if not model_name:
        logger.warning(
            "schema_extraction: no llm_model configured; defaulting to %d tokens",
            _DEFAULT_CONTEXT_TOKENS_FALLBACK,
        )
        return _DEFAULT_CONTEXT_TOKENS_FALLBACK
    name = model_name.lower()
    # Longest-prefix substring match against the specific-build table.
    for prefix in sorted(_MODEL_CONTEXT_TOKENS, key=len, reverse=True):
        if prefix in name:
            return _MODEL_CONTEXT_TOKENS[prefix]
    # Specific table missed — pick a similar family and warn so the
    # operator knows the value was guessed.
    for family, tokens in _FAMILY_FALLBACK_TOKENS:
        if family in name:
            logger.warning(
                "schema_extraction: model %r not in known-build table; "
                "using %s-family default of %d tokens. Add it to "
                "_MODEL_CONTEXT_TOKENS for an exact value.",
                model_name, family, tokens,
            )
            return tokens
    logger.warning(
        "schema_extraction: model %r unknown; using fallback default of %d tokens",
        model_name, _DEFAULT_CONTEXT_TOKENS_FALLBACK,
    )
    return _DEFAULT_CONTEXT_TOKENS_FALLBACK


def _resolve_sample_token_budget(llm_service) -> int:
    """Pick the sample-text token budget from the LLM's configured
    ``token_limit``, falling back to the model's default context window
    when ``token_limit`` is not set.

    Reserves ``_PROMPT_OVERHEAD_TOKENS`` for the prompt scaffolding and
    LLM output. Returns tokens — callers convert to characters at the
    truncation boundary.
    """
    cfg = getattr(llm_service, "config", None) or {}
    token_budget = int(cfg.get("token_limit") or 0)
    if token_budget <= 0:
        token_budget = _default_context_tokens(cfg.get("llm_model"))
    return max(token_budget - _PROMPT_OVERHEAD_TOKENS, _MIN_SAMPLE_TOKENS)


def _build_prompt(llm_service) -> PromptTemplate:
    """Wrap *llm_service*'s ``schema_extraction_prompt`` text in a
    ``PromptTemplate`` with the three required input variables.
    """
    template_str = llm_service.schema_extraction_prompt
    return PromptTemplate(
        template=template_str,
        input_variables=["samples", "structural_types", "tg_keywords"],
    )


def concatenate_samples(
    samples: Iterable[dict],
    max_tokens: int,
) -> str:
    """Concatenate sample-doc markdown into a single blob, with each
    document preceded by an ``# <doc_id>`` heading.

    The budget is expressed in *tokens*; this function converts to
    characters internally at ~4 chars/token for ``len()``-based
    truncation. The budget is distributed across files so every
    uploaded sample contributes — files are not silently dropped when
    the first file is large. Each file gets
    ``remaining_budget // remaining_files`` characters of head sample;
    if a file uses less, the leftover rolls forward to subsequent files.

    *samples* is an iterable of ``{"doc_id": str, "content": str}``
    dicts (the same shape ``extract_text_from_file_with_images_as_docs``
    returns).
    """
    samples_list = list(samples)
    n = len(samples_list)
    if n == 0:
        return ""

    max_chars = max_tokens * _CHARS_PER_TOKEN
    parts: List[str] = []
    remaining_budget = max_chars
    remaining_files = n
    truncated_any = False
    for s in samples_list:
        doc_id = s.get("doc_id", "doc")
        content = s.get("content", "") or ""
        header = f"\n\n# {doc_id}\n\n"
        per_file = remaining_budget // max(remaining_files, 1)
        full = header + content
        if len(full) > per_file:
            truncated_any = True
        chunk = full[:per_file]
        parts.append(chunk)
        remaining_budget -= len(chunk)
        remaining_files -= 1

    if truncated_any:
        logger.warning(
            "Schema-extraction samples truncated to fit %d-token budget across %d files",
            max_tokens,
            n,
        )
    return "".join(parts).lstrip()


def extract_schema_gsql(
    llm_service,
    samples: Iterable[dict],
    max_tokens: Optional[int] = None,
) -> str:
    """Run the schema-extraction prompt against *llm_service*. Returns
    the raw GSQL string the model produced (caller passes it to
    ``schema_utils.parse_gsql_schema``).

    *llm_service* must expose ``schema_extraction_prompt`` (from
    :class:`common.llm_services.base_llm.LLM_Model`) and the standard
    ``invoke_with_parser(prompt, parser, inputs, caller_name)`` entry
    point. Per-graph prompt overrides are picked up automatically by
    ``schema_extraction_prompt``'s resolution chain.

    When *max_tokens* is ``None`` (the production path), the sample
    budget is resolved from ``llm_service.config.token_limit`` if set,
    otherwise from the model's default context window. Tests can pass
    an explicit *max_tokens* to pin behavior independently of config.
    """
    if max_tokens is None:
        max_tokens = _resolve_sample_token_budget(llm_service)
    prompt = _build_prompt(llm_service)
    samples_blob = concatenate_samples(samples, max_tokens=max_tokens)
    structural_types = ", ".join(
        sorted(GRAPHRAG_STRUCTURAL_VERTEX_TYPES | GRAPHRAG_STRUCTURAL_EDGE_TYPES)
    )
    tg_keywords = ", ".join(sorted(get_gsql_reserved_words()))

    raw = llm_service.invoke_with_parser(
        prompt,
        StrOutputParser(),
        {
            "samples": samples_blob,
            "structural_types": structural_types,
            "tg_keywords": tg_keywords,
        },
        caller_name="schema_extraction",
    )
    if isinstance(raw, str):
        return raw.strip()
    return str(raw).strip()
