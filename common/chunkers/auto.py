# Copyright (c) 2024-2026 TigerGraph, Inc.
#
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0).
# See the LICENSE file or <https://www.gnu.org/licenses/agpl-3.0.html>
# for the full license text.

"""Content-aware chunker dispatcher.

When ``graphrag_config.chunker = "auto"`` is set on a graph, the ECC
worker instantiates an ``AutoChunker``. For each document passed to
``chunk()``, the dispatcher inspects the content's structural density
and delegates to the most appropriate concrete chunker:

  - HTML tags present (``<html>``, ``<body>``, ``<table>``, ``<h1>``…)
    → ``structured`` chunker (HTML-aware atomic blocks, heading folding)

  - Markdown structure present (multiple ``|...|`` tables, several
    ``![alt](url)`` figures, embedded ``<!-- PAGE N -->`` markers from
    pymupdf4llm) → ``structured`` chunker

  - Several markdown headings but no table / figure / page signals
    → ``markdown`` chunker (heading-aware section splitter)

  - No structure signals → ``semantic`` chunker (LLM-embedding-based
    coherent splitting)

Delegate chunkers are lazily instantiated and cached, so a graph
ingesting 50 markdown documents only instantiates one ``StructuredChunker``.
"""

from __future__ import annotations

import re
from typing import Callable, Dict

from common.chunkers.base_chunker import BaseChunker


# Heuristic thresholds — tuned for typical document corpora.
_SAMPLE_BYTES = 2 * 1024            # how much of the doc to inspect (prefix)
_TABLE_LINE_MIN = 3                 # `|...|` lines to trigger structured
_FIGURE_LINE_MIN = 3                # `![alt](url)` lines to trigger structured
_HEADING_LINE_MIN_FOR_MD = 3        # markdown headings to trigger markdown chunker
_PAGE_MARKER_MIN = 2                # `<!-- PAGE N -->` markers to trigger structured

_HTML_INDICATORS = (
    "<html", "<body", "<head>", "<table", "<div ", "<div>",
    "<h1>", "<h2>", "<h3>", "<p>", "<section", "<article",
)
_TABLE_LINE_RE = re.compile(r"^\s*\|.*\|")
_HEADING_LINE_RE = re.compile(r"^\s*#{1,6}\s")
_FIGURE_LINE_RE = re.compile(r"!\[")
_PAGE_MARKER_RE = re.compile(r"<!--\s*PAGE\s+\d+\s*-->")


def auto_detect_kind(content: str) -> str:
    """Return the chunker name best matched to ``content``."""
    if not content:
        return "single"
    sample = content[:_SAMPLE_BYTES]

    # HTML — even a small fragment is a strong signal.
    lowered = sample.lower()
    if any(tag in lowered for tag in _HTML_INDICATORS):
        return "structured"

    # Density signals on the markdown-shaped path.
    lines = sample.split("\n")
    table_lines = sum(1 for l in lines if _TABLE_LINE_RE.match(l))
    figure_lines = sum(1 for l in lines if _FIGURE_LINE_RE.search(l))
    heading_lines = sum(1 for l in lines if _HEADING_LINE_RE.match(l))
    page_markers = len(_PAGE_MARKER_RE.findall(sample))

    has_atomic_structure = (
        table_lines >= _TABLE_LINE_MIN
        or figure_lines >= _FIGURE_LINE_MIN
        or page_markers >= _PAGE_MARKER_MIN
    )
    if has_atomic_structure:
        return "structured"
    if heading_lines >= _HEADING_LINE_MIN_FOR_MD:
        return "markdown"
    return "semantic"


class AutoChunker(BaseChunker):
    """Dispatches to a concrete chunker per document.

    ``factory`` is a callable that produces a concrete chunker given a
    kind string (``"structured"`` / ``"markdown"`` / ``"semantic"`` /
    ``"single"``). The factory is normally a thin wrapper around
    ``ecc_util.get_chunker`` that closes over the per-graph config.

    Each unique kind is instantiated at most once per ECC pass and
    cached, so a graph with many same-shaped documents reuses one
    delegate instance.
    """

    def __init__(self, factory: Callable[[str], BaseChunker]):
        self._factory = factory
        self._cache: Dict[str, BaseChunker] = {}

    def _delegate(self, kind: str) -> BaseChunker:
        if kind not in self._cache:
            self._cache[kind] = self._factory(kind)
        return self._cache[kind]

    def chunk(self, content: str):
        kind = auto_detect_kind(content)
        return self._delegate(kind).chunk(content)
