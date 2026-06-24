# Copyright (c) 2024-2026 TigerGraph, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Page- and structure-aware chunker (v2.0 — GML-2121).

Replaces char-count slicing for PDF and HTML ingest with an atomic-unit
chunker that respects markdown / HTML structure:

- Tables (``|...|`` in markdown; ``<table>`` in HTML) are never split mid-row.
- Figures (``![alt](url)`` in markdown; ``<figure>`` / ``<img>`` in HTML) keep
  their caption.
- Lists (``<ol>`` / ``<ul>`` / ``<dl>``) stay atomic up to a size threshold;
  larger lists split at ``<li>`` boundaries with each subset still atomic.
- Code blocks (fenced markdown; ``<pre>`` / ``<code>``) stay whole.
- Prose paragraphs char-split as today, bounded by ``chunk_size``.

The chunker is format-agnostic. Markdown and HTML inputs both reduce to a
uniform ``Element`` stream; a single ``pack`` step turns that stream into
``StructuredChunk`` instances (a ``str`` subclass — drop-in for existing
consumers that pass chunk text to embedding / entity extraction, with
metadata accessible via attributes for newer consumers).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Iterable, List, Literal, Optional, Tuple

from common.chunkers.base_chunker import BaseChunker
from common.chunkers.separators import TEXT_SEPARATORS

logger = logging.getLogger(__name__)


_DEFAULT_CHUNK_SIZE = 2048
_DEFAULT_OVERLAP_DIV = 8  # overlap defaults to chunk_size / 8 to match other chunkers


# --- public chunk type ------------------------------------------------------

ChunkKind = Literal["prose", "table", "figure", "code", "list", "heading", "mixed"]


class StructuredChunk(str):
    """A chunk that behaves like ``str`` but carries structure metadata.

    Subclassing ``str`` keeps existing consumers (embedding, entity
    extraction, GSQL upserts) working unchanged — they see a string. New
    consumers read ``chunk_kind`` / ``page_no`` / ``under_heading`` /
    ``continues_from_page`` / ``continues_to_page`` via attributes.
    """

    chunk_kind: ChunkKind
    page_no: Optional[int]
    under_heading: Optional[str]
    continues_from_page: Optional[int]
    continues_to_page: Optional[int]

    def __new__(
        cls,
        text: str,
        *,
        chunk_kind: ChunkKind = "prose",
        page_no: Optional[int] = None,
        under_heading: Optional[str] = None,
        continues_from_page: Optional[int] = None,
        continues_to_page: Optional[int] = None,
    ) -> "StructuredChunk":
        instance = super().__new__(cls, text)
        instance.chunk_kind = chunk_kind
        instance.page_no = page_no
        instance.under_heading = under_heading
        instance.continues_from_page = continues_from_page
        instance.continues_to_page = continues_to_page
        return instance

    def metadata(self) -> dict:
        return {
            "chunk_kind": self.chunk_kind,
            "page_no": self.page_no,
            "under_heading": self.under_heading,
            "continues_from_page": self.continues_from_page,
            "continues_to_page": self.continues_to_page,
        }


# --- internal element type --------------------------------------------------

ElementKind = Literal["prose", "table", "figure", "code", "list", "heading"]


@dataclass
class Element:
    """One typed unit extracted from a markdown or HTML source.

    Atomic kinds (``table``, ``figure``, ``code``, ``list``) are never
    split below this granularity by the packer. ``heading`` elements are
    promoted to the ``heading`` field of subsequent elements so each
    packed chunk carries the most-recent section title.
    """
    kind: ElementKind
    text: str
    heading: Optional[str] = None       # most recent heading text
    page: Optional[int] = None          # PDF only — present when source has page metadata
    # For lists too long to keep atomic: pre-split sub-items the packer
    # can re-pack while keeping each subset atomic at ``<li>`` boundaries.
    splittable_items: Optional[List[str]] = field(default=None, repr=False)


# --- markdown adapter -------------------------------------------------------

# Pure markdown table: a line starting with `|` and at least one more `|`.
_MD_TABLE_LINE = re.compile(r"^\s*\|.*\|\s*$")
# Markdown image / figure reference.
_MD_IMG_LINE = re.compile(r"^\s*!\[.*?\]\(.*?\)\s*$")
# Fenced code block delimiter.
_MD_CODE_FENCE = re.compile(r"^\s*```")
# Markdown heading line.
_MD_HEADING = re.compile(r"^\s*(#{1,6})\s+(.+?)\s*$")
# An HTML comment from pymupdf4llm chunk markers — informational only.
_MD_HTML_COMMENT = re.compile(r"^\s*<!--.*-->\s*$")
# Page marker emitted by the PDF text extractor (see common/utils/text_extractors.py).
# Lines matching this update the "current page" for following elements without
# emitting an element themselves.
_MD_PAGE_MARKER = re.compile(r"^\s*<!--\s*PAGE\s+(\d+)\s*-->\s*$")
# pymupdf4llm artifacts:
#  • "==> picture [WxH] intentionally omitted <==" — image dropped (skip line)
#  • "----- Start of picture text -----" / "----- End of picture text -----"
#    bracket OCR'd content inside an image; we fold the body into the figure
#    so chart-internal labels stay with the image chunk.
_MD_PICTURE_OMITTED = re.compile(r"^\s*\*+\s*==>\s*picture\b.*intentionally omitted\s*<==\s*\*+.*$", re.IGNORECASE)
_MD_PICTURE_TEXT_START = re.compile(r"^\s*\*+\s*-+\s*Start of picture text\s*-+\s*\*+\s*(<br\s*/?>)?\s*$", re.IGNORECASE)
_MD_PICTURE_TEXT_END = re.compile(r"^\s*\*+\s*-+\s*End of picture text\s*-+\s*\*+\s*(<br\s*/?>)?\s*$", re.IGNORECASE)


def _flush_prose(buf: List[str], heading: Optional[str], page: Optional[int], out: List[Element]) -> None:
    if not buf:
        return
    text = "\n".join(buf).strip()
    if text:
        out.append(Element(kind="prose", text=text, heading=heading, page=page))
    buf.clear()


# A caption is a short single-or-double-line prose block that immediately
# precedes a table or figure with no blank line between them. We fold it
# into the atomic element so retrieval of "図表２ 残高表(抜粋)" returns
# the table, not a sibling prose chunk.
_CAPTION_MAX_CHARS = 200
_CAPTION_MAX_LINES = 2


def _take_caption(buf: List[str]) -> Optional[str]:
    """If ``buf`` looks like a caption (short, ≤2 lines), pop and return it.
    Otherwise return None and leave ``buf`` untouched.

    Handles the no-blank-line case where the caption sits directly above
    the table in the source:

        図表２　残高表（抜粋）
        |...|...|

    The blank-line case (pymupdf4llm typically emits this shape) is
    handled by ``_take_caption_from_out`` instead.
    """
    if not buf:
        return None
    if len(buf) > _CAPTION_MAX_LINES:
        return None
    joined = "\n".join(buf).strip()
    if not joined or len(joined) > _CAPTION_MAX_CHARS:
        return None
    buf.clear()
    return joined


def _take_caption_from_out(out: List[Element]) -> Optional[str]:
    """If the most recently emitted element is a short prose block, pop
    and return its text. Handles the blank-line case:

        図表 7-2 都道府県別総預貯金額 ( 兆円 )
        ← blank line, prose flushed to ``out`` here

        |都道府県|...

    A heading or any non-prose immediately preceding the table blocks
    the lookback (returns None), preserving the rule that a caption
    above a section heading belongs to the section, not the next table.
    """
    if not out or out[-1].kind != "prose":
        return None
    last = out[-1]
    if len(last.text) > _CAPTION_MAX_CHARS:
        return None
    # Lines in the stored element text use single \n separators.
    if last.text.count("\n") + 1 > _CAPTION_MAX_LINES:
        return None
    return out.pop().text


def markdown_to_elements(md: str, page: Optional[int] = None) -> List[Element]:
    """Tokenize markdown into a stream of typed elements.

    Handles GFM-style tables (consecutive ``|...|`` rows), fenced code
    blocks, image lines, headings, and prose paragraphs separated by
    blank lines. HTML comments are dropped (pymupdf4llm leaves chunk
    markers in some flows).
    """
    out: List[Element] = []
    heading: Optional[str] = None
    prose_buf: List[str] = []

    lines = md.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # 1. Heading line.
        m = _MD_HEADING.match(line)
        if m:
            _flush_prose(prose_buf, heading, page, out)
            heading = m.group(2).strip()
            out.append(Element(kind="heading", text=heading, heading=heading, page=page))
            i += 1
            continue

        # 2. Fenced code block — collect until matching fence.
        if _MD_CODE_FENCE.match(line):
            _flush_prose(prose_buf, heading, page, out)
            block = [line]
            i += 1
            while i < len(lines):
                block.append(lines[i])
                if _MD_CODE_FENCE.match(lines[i]):
                    i += 1
                    break
                i += 1
            out.append(Element(kind="code", text="\n".join(block), heading=heading, page=page))
            continue

        # 3. Standalone image (figure) line.
        if _MD_IMG_LINE.match(line):
            caption = _take_caption(prose_buf)
            _flush_prose(prose_buf, heading, page, out)
            if caption is None:
                caption = _take_caption_from_out(out)
            body = line.strip()
            if caption:
                body = f"{caption}\n\n{body}"
            out.append(Element(kind="figure", text=body, heading=heading, page=page))
            i += 1
            continue

        # 4. Markdown table — collect contiguous `|...|` lines, folding any
        #    short prose line immediately before it as the caption (e.g.
        #    "図表２ 2011年３月末の資金循環統計の残高表（抜粋）" — the
        #    caption must travel with the table or retrieval misses it).
        #    The caption may sit directly above the table (in prose_buf)
        #    OR be separated by a blank line (already flushed to ``out``);
        #    we check both locations in that order.
        if _MD_TABLE_LINE.match(line):
            caption = _take_caption(prose_buf)
            _flush_prose(prose_buf, heading, page, out)
            if caption is None:
                caption = _take_caption_from_out(out)
            block = [line]
            i += 1
            while i < len(lines) and _MD_TABLE_LINE.match(lines[i]):
                block.append(lines[i])
                i += 1
            body = "\n".join(block)
            if caption:
                body = f"{caption}\n\n{body}"
            out.append(Element(kind="table", text=body, heading=heading, page=page))
            continue

        # 5a. Page marker — updates current page for following elements.
        pm = _MD_PAGE_MARKER.match(line)
        if pm:
            _flush_prose(prose_buf, heading, page, out)
            try:
                page = int(pm.group(1))
            except ValueError:
                pass
            i += 1
            continue

        # 5b. Other HTML comments (chunk markers etc.) — skip.
        if _MD_HTML_COMMENT.match(line):
            i += 1
            continue

        # 5c. pymupdf4llm "==> picture ... intentionally omitted <==" — drop.
        if _MD_PICTURE_OMITTED.match(line):
            i += 1
            continue

        # 5d. pymupdf4llm picture-text block: ----- Start ... End of picture
        #     text ----- wraps OCR'd content (chart axis labels, legends).
        #     Fold the body into the immediately preceding figure when
        #     present so chart-internal text travels with the image.
        if _MD_PICTURE_TEXT_START.match(line):
            _flush_prose(prose_buf, heading, page, out)
            i += 1
            block: List[str] = []
            while i < len(lines) and not _MD_PICTURE_TEXT_END.match(lines[i]):
                block.append(lines[i])
                i += 1
            if i < len(lines):
                i += 1  # skip the End marker
            # Inline <br> tags become line breaks for readability.
            body = "\n".join(block)
            body = re.sub(r"<br\s*/?>", "\n", body, flags=re.IGNORECASE).strip()
            if not body:
                continue
            if out and out[-1].kind == "figure":
                out[-1].text = f"{out[-1].text}\n\n{body}"
            else:
                # No preceding figure — emit as a standalone figure element
                # (treating the OCR'd image content as a figure with no URL).
                out.append(Element(kind="figure", text=body, heading=heading, page=page))
            continue

        # 6. Blank line — flush current prose paragraph.
        if not stripped:
            _flush_prose(prose_buf, heading, page, out)
            i += 1
            continue

        # 7. Default: accumulate as prose.
        prose_buf.append(line)
        i += 1

    _flush_prose(prose_buf, heading, page, out)
    return out


def markdown_pages_to_elements(pages: Iterable[dict]) -> List[Element]:
    """Convert ``pymupdf4llm.to_markdown(..., page_chunks=True)`` output
    (a list of per-page dicts) into a flat element stream with each
    element carrying its ``page`` number.

    pymupdf4llm exposes the page index under ``metadata.page_number``
    (1-based). ``metadata.page`` is a filename-style label and may be
    absent, so we check both keys.
    """
    out: List[Element] = []
    for p in pages or []:
        page_no = None
        md = p.get("text") or ""
        meta = p.get("metadata") or {}
        for key in ("page_number", "page"):
            if key in meta:
                try:
                    page_no = int(meta[key])
                    break
                except (TypeError, ValueError):
                    page_no = None
        out.extend(markdown_to_elements(md, page=page_no))
    return out


# --- html adapter -----------------------------------------------------------

_HTML_ATOMIC = {"table", "pre", "ol", "ul", "dl", "figure", "blockquote"}
_HTML_PROSE = {"p"}
_HTML_HEADS = {f"h{i}" for i in range(1, 7)}
_HTML_SKIP = {"script", "style", "noscript", "meta", "link", "head"}


def html_to_elements(html: str) -> List[Element]:
    """Walk an HTML document (or fragment) and emit a typed element
    stream. See the design notes on GML-2121 for the tag classification.
    """
    try:
        from bs4 import BeautifulSoup, NavigableString
    except ImportError as exc:  # pragma: no cover — bs4 is a runtime dep
        raise RuntimeError("structured chunker (HTML) requires beautifulsoup4") from exc

    soup = BeautifulSoup(html, "html.parser")
    out: List[Element] = []
    root = soup.body or soup
    _walk_html(root, out, heading=None, NavigableString=NavigableString)
    return out


def _walk_html(node, out: List[Element], heading: Optional[str], NavigableString) -> None:
    # Local import-bound NavigableString avoids re-importing in every recursive call.
    for child in getattr(node, "children", []):
        if isinstance(child, NavigableString):
            text = str(child).strip()
            if text:
                out.append(Element(kind="prose", text=text, heading=heading))
            continue
        tag = (child.name or "").lower()
        if not tag or tag in _HTML_SKIP:
            continue
        if tag in _HTML_HEADS:
            heading = child.get_text(strip=True)
            if heading:
                out.append(Element(kind="heading", text=heading, heading=heading))
            continue
        if tag in _HTML_ATOMIC:
            # Tables / blockquotes / code / figures stay atomic with their HTML preserved.
            # Lists carry splittable_items so the packer can re-pack at <li> when too long.
            if tag in {"ol", "ul", "dl"}:
                # Collect every direct block-level child as a splittable unit
                # (nested <ol>/<ul>/<table>/<p>, not just <li>).
                items: List[str] = []
                for c in child.children:
                    if isinstance(c, NavigableString):
                        t = str(c).strip()
                        if t:
                            items.append(t)
                        continue
                    cname = (c.name or "").lower()
                    if not cname or cname in _HTML_SKIP:
                        continue
                    items.append(str(c))
                out.append(Element(
                    kind="list",
                    text=str(child),
                    heading=heading,
                    splittable_items=items or None,
                ))
            elif tag == "table":
                out.append(Element(kind="table", text=str(child), heading=heading))
            elif tag == "blockquote":
                # Blockquote is prose-shaped but we keep it atomic.
                out.append(Element(
                    kind="prose",
                    text=child.get_text(separator=" ", strip=True),
                    heading=heading,
                ))
            elif tag == "figure":
                out.append(Element(kind="figure", text=str(child), heading=heading))
            else:
                out.append(Element(kind="code", text=str(child), heading=heading))
            continue
        if tag in _HTML_PROSE:
            text = child.get_text(separator=" ", strip=True)
            if text:
                out.append(Element(kind="prose", text=text, heading=heading))
            continue
        # Standalone <img> outside a <figure>.
        if tag == "img":
            alt = (child.get("alt") or "").strip()
            src = (child.get("src") or "").strip()
            label = f'![{alt}]({src})' if src else alt
            if label:
                out.append(Element(kind="figure", text=label, heading=heading))
            continue
        # walk-into: <div>, <section>, <article>, <main>, <aside>, <nav>,
        # <header>, <footer>, <li> (when nested directly), custom elements,
        # malformed HTML — recurse.
        _walk_html(child, out, heading, NavigableString)


# --- packer -----------------------------------------------------------------


def _split_prose(text: str, max_chars: int, overlap: int) -> List[str]:
    """Char-split a long prose block. Reuses langchain's recursive
    splitter so the behaviour matches our existing prose chunkers.
    """
    if len(text) <= max_chars:
        return [text]
    # Lazy import — only loaded when we actually need to split.
    from langchain_text_splitters import RecursiveCharacterTextSplitter
    splitter = RecursiveCharacterTextSplitter(
        separators=TEXT_SEPARATORS,
        chunk_size=max_chars,
        chunk_overlap=overlap,
    )
    return splitter.split_text(text)


def _pack_list_items(
    items: List[str],
    max_chars: int,
) -> List[str]:
    """Re-pack <li> items into the largest groups that fit ``max_chars``.

    Each returned string is a sequence of consecutive ``<li>`` blocks
    wrapped (caller adds <ul>/<ol> outer tags if it wants to). Single
    items longer than ``max_chars`` are emitted alone — we don't split
    inside a single list item.
    """
    out: List[str] = []
    buf: List[str] = []
    buf_len = 0
    for it in items:
        ilen = len(it)
        if buf and buf_len + ilen > max_chars:
            out.append("\n".join(buf))
            buf = [it]
            buf_len = ilen
        else:
            buf.append(it)
            buf_len += ilen
    if buf:
        out.append("\n".join(buf))
    return out


def _atomic_kind_for(elem: Element) -> ChunkKind:
    if elem.kind in ("table", "figure", "code", "list"):
        return elem.kind
    return "prose"


# Paragraphs longer than ``max_chars * _PROSE_OVERSIZE_RATIO`` are
# considered pathological (e.g. an entire legal contract glued together,
# or a code dump mis-classified as prose) and fall back to recursive
# char-splitting so we don't hand the embedding model an input larger
# than its context window. For ordinary content this threshold is never
# tripped — paragraphs stay whole.
_PROSE_OVERSIZE_RATIO = 16

# Atomic blocks (tables, figures, code, lists) are preserved whole by
# default — splitting a table mid-row, or a figure caption from its
# image, destroys retrieval semantics. But the embedding model has a
# hard input cap (Bedrock Titan: 8192 tokens ≈ 16k Japanese chars,
# fewer for Latin). An atomic block larger than this ceiling cannot
# be embedded at all and ends up with an empty vector, breaking
# similarity search. The safety valve: when an atomic block exceeds
# ``_ATOMIC_HARD_MAX_CHARS``, split it via the same recursive char
# splitter used for oversized prose. Pieces retain the original
# ``chunk_kind`` so retrieval still knows they came from a table /
# figure / code block.
_ATOMIC_HARD_MAX_CHARS = 12000


# Markup-aware splitters used by ``_split_atomic_oversized`` so an oversized
# atomic block stays semantically usable rather than getting char-split mid-row.
_TR_BLOCK_RE = re.compile(r"<tr\b[^>]*>.*?</tr>", re.IGNORECASE | re.DOTALL)
_TABLE_OPEN_RE = re.compile(r"<table\b[^>]*>", re.IGNORECASE)
_TABLE_CLOSE_RE = re.compile(r"</table>", re.IGNORECASE)


def _split_table_at_rows(
    text: str,
    hard_cap: int,
) -> List[str]:
    """Split an HTML table at ``<tr>`` boundaries, preserving the table
    envelope and the header row(s) on every emitted piece.

    Strategy: locate the outermost ``<table ...>``…``</table>``. The first
    one or two ``<tr>`` blocks are treated as headers (kept on every
    piece). Remaining body rows are packed greedily into pieces of at
    most ``hard_cap`` chars. Each piece is wrapped as
    ``<table ...>{headers}{body_rows}</table>``.

    Falls back to plain char-split when no ``<tr>`` boundaries are
    found (e.g. the table is a single huge cell or the markup is
    non-standard).
    """
    open_match = _TABLE_OPEN_RE.search(text)
    close_match = _TABLE_CLOSE_RE.search(text)
    if not open_match or not close_match or close_match.start() < open_match.end():
        return [text]

    prefix = text[:open_match.start()]
    open_tag = text[open_match.start():open_match.end()]
    body = text[open_match.end():close_match.start()]
    close_tag = text[close_match.start():close_match.end()]
    suffix = text[close_match.end():]

    rows = _TR_BLOCK_RE.findall(body)
    if len(rows) < 2:
        return [text]  # nothing to split at; let the caller char-split

    # Treat the first <tr> as the header. If the header is short and the
    # second row contains <th>, treat it as a continuation of the header.
    header_count = 1
    if header_count < len(rows) and "<th" in rows[header_count].lower():
        header_count = 2
    headers = "".join(rows[:header_count])
    body_rows = rows[header_count:]

    envelope_overhead = len(prefix) + len(open_tag) + len(headers) + len(close_tag) + len(suffix)
    row_budget = hard_cap - envelope_overhead
    if row_budget < 200:
        # The envelope alone eats the budget — header row is huge or the
        # table is tiny outside <tr> structure. Fall back to char-split.
        return [text]

    pieces: List[str] = []
    buf: List[str] = []
    buf_len = 0
    for row in body_rows:
        rlen = len(row)
        if buf and buf_len + rlen > row_budget:
            pieces.append(prefix + open_tag + headers + "".join(buf) + close_tag + suffix)
            buf = [row]
            buf_len = rlen
        else:
            buf.append(row)
            buf_len += rlen
    if buf:
        pieces.append(prefix + open_tag + headers + "".join(buf) + close_tag + suffix)
    return pieces or [text]


def _split_list_at_items(text: str, hard_cap: int) -> List[str]:
    """Split a long <ul>/<ol> at <li> boundaries. Header (the opening
    <ol>/<ul> + everything before the first <li>) is preserved on each
    piece, and each piece is closed properly. Falls back to char-split
    when no <li> boundaries are found.
    """
    li_blocks = re.findall(r"<li\b[^>]*>.*?</li>", text, re.IGNORECASE | re.DOTALL)
    if len(li_blocks) < 2:
        return [text]
    # Find the wrapper open / close
    wrap_open = re.search(r"<(?:ul|ol)\b[^>]*>", text, re.IGNORECASE)
    wrap_close = re.search(r"</(?:ul|ol)>", text, re.IGNORECASE)
    if not wrap_open or not wrap_close or wrap_close.start() < wrap_open.end():
        return [text]
    prefix = text[:wrap_open.start()]
    open_tag = text[wrap_open.start():wrap_open.end()]
    close_tag = text[wrap_close.start():wrap_close.end()]
    suffix = text[wrap_close.end():]

    envelope = len(prefix) + len(open_tag) + len(close_tag) + len(suffix)
    item_budget = hard_cap - envelope
    if item_budget < 200:
        return [text]

    pieces: List[str] = []
    buf: List[str] = []
    buf_len = 0
    for item in li_blocks:
        ilen = len(item)
        if buf and buf_len + ilen > item_budget:
            pieces.append(prefix + open_tag + "".join(buf) + close_tag + suffix)
            buf = [item]
            buf_len = ilen
        else:
            buf.append(item)
            buf_len += ilen
    if buf:
        pieces.append(prefix + open_tag + "".join(buf) + close_tag + suffix)
    return pieces or [text]


def _split_atomic_oversized(
    text: str,
    kind: "ChunkKind",
    page: Optional[int],
    heading: Optional[str],
    max_chars: int,
    overlap: int,
    hard_cap: int,
) -> List["StructuredChunk"]:
    """Split an atomic block that exceeds the embedding cap.

    Dispatches by ``kind``:
      * ``"table"`` — split at ``<tr>`` boundaries via
        :func:`_split_table_at_rows`, preserving the table envelope and
        header row on every piece so each piece reads as a valid
        sub-table for retrieval.
      * ``"list"`` — split at ``<li>`` boundaries via
        :func:`_split_list_at_items`, preserving the list wrapper.
      * Other kinds (figure, code, prose) — fall back to the recursive
        char splitter used for oversized prose.

    Returns one StructuredChunk per piece, all carrying the original
    chunk_kind / page_no / under_heading. The caller is responsible for
    appending these to the chunk stream.
    """
    pieces: List[str]
    if kind == "table":
        pieces = _split_table_at_rows(text, hard_cap)
        # If the table can't be row-split (no <tr> boundaries), fall back
        # to char-split so we still respect the embedding cap.
        if len(pieces) == 1 and len(pieces[0]) > hard_cap:
            pieces = _split_prose(text, min(max_chars, hard_cap), overlap)
    elif kind == "list":
        pieces = _split_list_at_items(text, hard_cap)
        if len(pieces) == 1 and len(pieces[0]) > hard_cap:
            pieces = _split_prose(text, min(max_chars, hard_cap), overlap)
    else:
        pieces = _split_prose(text, min(max_chars, hard_cap), overlap)
    return [
        StructuredChunk(
            piece,
            chunk_kind=kind,
            page_no=page,
            under_heading=heading,
        )
        for piece in pieces
    ]


def pack(
    elements: List[Element],
    max_chars: int = _DEFAULT_CHUNK_SIZE,
    overlap: Optional[int] = None,
) -> List[StructuredChunk]:
    """Convert a typed element stream into ``StructuredChunk`` instances.

    Rules:
    - Atomic elements (table / figure / code / list) emit standalone chunks
      with their ``kind`` preserved. A list element longer than ``max_chars``
      is re-packed at ``<li>`` boundaries via ``splittable_items``.
    - **Prose paragraphs are also atomic** — a single paragraph is never
      split mid-sentence regardless of size. Multiple short paragraphs
      under the same heading get packed together up to ``max_chars``;
      a paragraph larger than ``max_chars`` becomes one oversized chunk
      (matches table behaviour). Safety valve: a paragraph larger than
      ``max_chars * _PROSE_OVERSIZE_RATIO`` falls back to recursive char
      splitting so we don't exceed the embedding model's context window.
    - Headings annotate following chunks' ``under_heading`` but do not
      themselves emit chunks.
    - ``page`` from the source flows onto each emitted chunk; multi-page
      atomic blocks (today: none — pymupdf4llm assigns one page per
      element) get ``continues_from_page`` / ``continues_to_page`` set
      via the page-tracking pass below.
    """
    if overlap is None:
        overlap = max(0, max_chars // _DEFAULT_OVERLAP_DIV)
    oversize_threshold = max_chars * _PROSE_OVERSIZE_RATIO

    chunks: List[StructuredChunk] = []
    # prose_buf packs whole-paragraph Elements until adding the next one
    # would exceed max_chars, then flushes. No element is ever split.
    prose_buf: List[Element] = []
    prose_heading: Optional[str] = None
    prose_len = 0

    def flush_prose() -> None:
        nonlocal prose_buf, prose_heading, prose_len
        if not prose_buf:
            return
        text = "\n\n".join(e.text for e in prose_buf).strip()
        if not text:
            prose_buf, prose_len = [], 0
            return
        pages = [e.page for e in prose_buf if e.page is not None]
        first_page = pages[0] if pages else None
        last_page = pages[-1] if pages else None
        cont_from = first_page if (last_page is not None and first_page != last_page) else None
        cont_to = last_page if (last_page is not None and first_page != last_page) else None
        chunks.append(StructuredChunk(
            text,
            chunk_kind="prose",
            page_no=first_page,
            under_heading=prose_heading,
            continues_from_page=cont_from,
            continues_to_page=cont_to,
        ))
        prose_buf, prose_len = [], 0

    def emit_oversized_prose(elem: Element) -> None:
        """Safety valve: pathologically long single paragraph. Char-split
        as a last resort and emit each piece as its own prose chunk."""
        for piece in _split_prose(elem.text, max_chars, overlap):
            chunks.append(StructuredChunk(
                piece,
                chunk_kind="prose",
                page_no=elem.page,
                under_heading=elem.heading,
            ))

    for elem in elements:
        if elem.kind == "heading":
            flush_prose()
            prose_heading = elem.heading
            # The heading itself does not become a chunk; following
            # elements carry .heading via their Element fields, and
            # prose_heading is the packer-side memo for chunk metadata.
            continue

        if elem.kind in ("table", "figure", "code"):
            flush_prose()
            kind = _atomic_kind_for(elem)
            if len(elem.text) > _ATOMIC_HARD_MAX_CHARS:
                chunks.extend(_split_atomic_oversized(
                    elem.text, kind, elem.page, elem.heading,
                    max_chars, overlap, _ATOMIC_HARD_MAX_CHARS,
                ))
            else:
                chunks.append(StructuredChunk(
                    elem.text,
                    chunk_kind=kind,
                    page_no=elem.page,
                    under_heading=elem.heading,
                ))
            continue

        if elem.kind == "list":
            flush_prose()
            if len(elem.text) <= max_chars or not elem.splittable_items:
                if len(elem.text) > _ATOMIC_HARD_MAX_CHARS:
                    chunks.extend(_split_atomic_oversized(
                        elem.text, "list", elem.page, elem.heading,
                        max_chars, overlap, _ATOMIC_HARD_MAX_CHARS,
                    ))
                else:
                    chunks.append(StructuredChunk(
                        elem.text,
                        chunk_kind="list",
                        page_no=elem.page,
                        under_heading=elem.heading,
                    ))
            else:
                for body in _pack_list_items(elem.splittable_items, max_chars):
                    if len(body) > _ATOMIC_HARD_MAX_CHARS:
                        chunks.extend(_split_atomic_oversized(
                            body, "list", elem.page, elem.heading,
                            max_chars, overlap, _ATOMIC_HARD_MAX_CHARS,
                        ))
                    else:
                        chunks.append(StructuredChunk(
                            body,
                            chunk_kind="list",
                            page_no=elem.page,
                            under_heading=elem.heading,
                        ))
            continue

        # Prose: atomic paragraph packing.
        # Different heading context → flush before adopting the new one.
        if elem.heading != prose_heading and prose_buf:
            flush_prose()
        prose_heading = elem.heading

        elem_len = len(elem.text)

        # Pathologically long single paragraph → safety-valve fallback.
        if elem_len > oversize_threshold:
            flush_prose()
            emit_oversized_prose(elem)
            continue

        # Packing rule: if adding this paragraph would push the buffer
        # past max_chars and the buffer is non-empty, flush first so each
        # output chunk fits the target size. A single paragraph that
        # alone exceeds max_chars is still emitted whole (atomic-prose).
        if prose_buf and (prose_len + elem_len > max_chars):
            flush_prose()

        prose_buf.append(elem)
        prose_len += elem_len

    flush_prose()

    # Merge tiny adjacent chunks so heading-only and section-marker
    # fragments ("目次", "《留意点》", "＜7-1＞", ...) don't pollute the
    # embedding index. A chunk smaller than ``_MIN_CHUNK_CHARS`` is
    # absorbed into a neighbor when:
    #   * the same ``chunk_kind`` (don't merge a table into prose),
    #   * the same ``under_heading`` (don't cross section boundaries),
    #   * the resulting merged chunk stays under ``max_chars``.
    # Prefers merging tiny chunks into the previous chunk; falls back to
    # the next chunk when the previous doesn't qualify.
    chunks = _merge_tiny_chunks(chunks, max_chars=max_chars)
    return chunks


_MIN_CHUNK_CHARS_RATIO = 0.5  # min size = max_chars * ratio


def _merge_tiny_chunks(
    chunks: List[StructuredChunk],
    max_chars: int,
) -> List[StructuredChunk]:
    """Merge chunks smaller than ``max_chars * _MIN_CHUNK_CHARS_RATIO``
    into a neighbor when the merge keeps the result under ``max_chars``
    and the neighbor matches ``chunk_kind`` + ``under_heading``.

    Walks the chunk list once. For each chunk, checks whether it's
    small enough to be merged; if so, absorbs into the previous chunk
    when compatible, else into the next; else leaves it standalone.
    """
    if not chunks:
        return chunks
    min_chars = int(max_chars * _MIN_CHUNK_CHARS_RATIO)
    merged: List[StructuredChunk] = []
    pending: List[StructuredChunk] = list(chunks)
    i = 0
    while i < len(pending):
        c = pending[i]
        if len(c) >= min_chars:
            merged.append(c)
            i += 1
            continue
        # c is tiny — try to merge into the previous chunk first.
        if merged and _can_merge(merged[-1], c, max_chars):
            merged[-1] = _merge_pair(merged[-1], c)
            i += 1
            continue
        # else try to merge into the next chunk.
        if i + 1 < len(pending) and _can_merge(c, pending[i + 1], max_chars):
            pending[i + 1] = _merge_pair(c, pending[i + 1])
            i += 1
            continue
        # No compatible neighbor — keep the tiny chunk standalone.
        merged.append(c)
        i += 1
    return merged


def _can_merge(a: StructuredChunk, b: StructuredChunk, max_chars: int) -> bool:
    """Two chunks are mergeable when they share kind + heading and the
    combined length fits ``max_chars``. We don't merge atomic kinds
    (table / figure / code / list) into anything — those carry HTML
    envelopes that can't be naively concatenated.
    """
    if a.chunk_kind != b.chunk_kind:
        return False
    if a.chunk_kind in ("table", "figure", "code", "list"):
        return False
    if (a.under_heading or "") != (b.under_heading or ""):
        return False
    # +2 accounts for the "\n\n" joiner.
    return len(a) + len(b) + 2 <= max_chars


def _merge_pair(a: StructuredChunk, b: StructuredChunk) -> StructuredChunk:
    """Concatenate two compatible chunks. Page metadata: if both share a
    page, keep it; otherwise mark continues_from / continues_to.
    """
    text = (str(a).rstrip() + "\n\n" + str(b).lstrip()).strip()
    same_page = a.page_no == b.page_no
    return StructuredChunk(
        text,
        chunk_kind=a.chunk_kind,
        page_no=a.page_no if same_page else a.page_no,
        under_heading=a.under_heading,
        continues_from_page=a.continues_from_page if same_page else a.page_no,
        continues_to_page=a.continues_to_page if same_page else b.page_no,
    )


# --- chunker wrapper --------------------------------------------------------


class StructuredChunker(BaseChunker):
    """Structure-aware chunker.

    ``chunk(input_text)`` accepts either a markdown string or an HTML string
    — format auto-detected by leading ``<`` content (HTML) versus anything
    else (markdown). For multi-page PDF inputs, callers should instead use
    ``chunk_pages(pages)`` with the per-page dict list from
    ``pymupdf4llm.to_markdown(..., page_chunks=True)`` so page numbers
    propagate to chunk metadata.
    """

    def __init__(
        self,
        chunk_size: int = 0,
        overlap_size: int = -1,
    ):
        self.chunk_size = chunk_size if chunk_size > 0 else _DEFAULT_CHUNK_SIZE
        self.overlap_size = (
            overlap_size if overlap_size >= 0 else self.chunk_size // _DEFAULT_OVERLAP_DIV
        )

    def chunk(self, input_text: str) -> List[StructuredChunk]:
        elements = self._detect_and_tokenize(input_text)
        return pack(elements, max_chars=self.chunk_size, overlap=self.overlap_size)

    def chunk_pages(self, pages: Iterable[dict]) -> List[StructuredChunk]:
        elements = markdown_pages_to_elements(pages)
        return pack(elements, max_chars=self.chunk_size, overlap=self.overlap_size)

    @staticmethod
    def _detect_and_tokenize(text: str) -> List[Element]:
        stripped = (text or "").lstrip()
        looks_html = stripped.startswith("<") and (
            "<html" in stripped[:200].lower()
            or "<body" in stripped[:200].lower()
            or "<div" in stripped[:200].lower()
            or "<p" in stripped[:200].lower()
            or "<table" in stripped[:200].lower()
        )
        if looks_html:
            return html_to_elements(text)
        return markdown_to_elements(text)
