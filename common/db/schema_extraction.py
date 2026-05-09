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
from typing import Iterable, List

from langchain.prompts import PromptTemplate
from langchain_core.output_parsers import StrOutputParser

from common.db.schema_utils import (
    GRAPHRAG_STRUCTURAL_EDGE_TYPES,
    GRAPHRAG_STRUCTURAL_VERTEX_TYPES,
    get_gsql_reserved_words,
)

logger = logging.getLogger(__name__)


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
    max_chars: int,
) -> str:
    """Concatenate sample-doc markdown into a single blob, with each
    document preceded by an ``# <doc_id>`` heading. Truncates at
    *max_chars* total characters; truncation is logged.

    *samples* is an iterable of ``{"doc_id": str, "content": str}``
    dicts (the same shape ``extract_text_from_file_with_images_as_docs``
    returns).
    """
    parts: List[str] = []
    total = 0
    for s in samples:
        doc_id = s.get("doc_id", "doc")
        content = s.get("content", "") or ""
        header = f"\n\n# {doc_id}\n\n"
        budget = max_chars - total
        if budget <= 0:
            logger.warning("Sample doc budget exhausted; later files truncated.")
            break
        chunk = (header + content)[:budget]
        parts.append(chunk)
        total += len(chunk)
    return "".join(parts).lstrip()


def extract_schema_gsql(
    llm_service,
    samples: Iterable[dict],
    max_chars: int = 200_000,
) -> str:
    """Run the schema-extraction prompt against *llm_service*. Returns
    the raw GSQL string the model produced (caller passes it to
    ``schema_utils.parse_gsql_schema``).

    *llm_service* must expose ``schema_extraction_prompt`` (from
    :class:`common.llm_services.base_llm.LLM_Model`) and the standard
    ``invoke_with_parser(prompt, parser, inputs, caller_name)`` entry
    point. Per-graph prompt overrides are picked up automatically by
    ``schema_extraction_prompt``'s resolution chain.
    """
    prompt = _build_prompt(llm_service)
    samples_blob = concatenate_samples(samples, max_chars=max_chars)
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
