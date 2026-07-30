"""
Text extraction utilities for various file formats.
This module handles the extraction of text content from different document types.
"""
import os
import json
import logging
import base64
import io
import re
import tempfile
import threading
from pathlib import Path
import shutil
import asyncio
from concurrent.futures import ThreadPoolExecutor

logger = logging.getLogger(__name__)

# Global lock for pymupdf4llm calls (not thread-safe)
_pymupdf4llm_lock = threading.Lock()

# regex for markdown images: ![alt](path)
# [^)]+ (not [^)\s]+) so that paths containing spaces are captured correctly.
# pymupdf4llm can generate image filenames with spaces; the narrower \s exclusion
# caused extract_images() to silently return [] for those files, deleting the temp
# folder and leaving broken references in the markdown.
_md_pattern = re.compile(r'!\[([^\]]*)\]\(([^)]+)\)')

# Matches a ColN placeholder header cell produced by pymupdf4llm when it
# cannot detect a column header from the PDF structure (common in form PDFs).
_coln_pattern = re.compile(r'\bCol\d+\b')

# Vertical-CJK-character runs produced when pymupdf4llm encounters a PDF
# cell containing Japanese / Chinese / Korean text laid out top-to-bottom
# (one character per typographic line). pymupdf4llm preserves each
# character on its own logical line and per-character bold formatting,
# producing patterns like:
#   **個**<br>**別**<br>**信**<br>**用**...
#   個<br>別<br>信<br>用...
# which bloat tokens 3-5x and confuse retrieval embeddings. The CJK
# Unicode ranges below cover CJK Unified Ideographs (U+4E00-U+9FFF),
# Hiragana / Katakana / CJK Symbols (U+3000-U+30FF), and full-width
# / half-width forms (U+FF00-U+FFEF) excluding fullwidth digits
# (U+FF10-U+FF19). Collapsing digit runs would glue distinct chart
# values such as 767 and 808 into ``767808``.
# One CJK char excluding fullwidth digits (U+FF10-U+FF19).
_CJK_CHAR = r"(?:[　-鿿]|[＀-／]|[：-￯])"
_VERTICAL_BOLD_CJK = re.compile(
    rf"(?:\*\*{_CJK_CHAR}\*\*(?:<br\s*/?>)){{2,}}\*\*{_CJK_CHAR}\*\*"
)
_VERTICAL_CJK = re.compile(
    rf"(?:{_CJK_CHAR}<br\s*/?>){{2,}}{_CJK_CHAR}"
)

# Within-cell <br> tags inside markdown table rows. pymupdf4llm uses these
# to mark visual line breaks inside a single cell (vertical-numeric runs
# like ``|3<br>4<br>5|``, or single-character mojibake glyph sequences).
# Whatever the cause, the result is a cell that retrieval treats as
# multiple unrelated tokens. Stripping ``<br>`` inside ``|...|`` rows
# reunites the cell text on one logical line; ``<br>`` outside table
# rows is left alone since it usually marks an intentional break.
_TABLE_LINE_RE = re.compile(r"^\s*\|")
_BR_TAG_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)

# pymupdf4llm picture-text blocks (HTML-comment or markdown-dash markers).
# These are figure-associated text from pymupdf4llm, not a separate OCR engine.
_PICTURE_TEXT_BLOCK_RE = re.compile(
    r"(?:<!--\s*Start of picture text\s*-->|\*{0,3}\s*-+\s*Start of picture text\s*-+\s*\*{0,3})"
    r"(.*?)"
    r"(?:<!--\s*End of picture text\s*-->|\*{0,3}\s*-+\s*End of picture text\s*-+\s*\*{0,3})",
    re.IGNORECASE | re.DOTALL,
)

# Adjacent comma-grouped numbers glued with no separator, e.g. ``1,5461,518``.
_GLUED_COMMA_NUMBERS_RE = re.compile(
    r"(?<!\d)(\d{1,3}(?:,\d{3})+)(\d{1,3}(?:,\d{3})+)"
)

# Mojibake detection: PDFs whose embedded font CMap can't be resolved
# emit runs of Latin-1 supplement characters (À-ÿ, ¡-¿), control glyphs,
# or U+FFFD replacement characters. None of these are expected in
# legitimate Japanese or English text at high density. A line whose
# share of suspicious characters exceeds the threshold gets logged.
_MOJIBAKE_HIGH_LATIN1 = re.compile(r"[ -ÿ-]")
_MOJIBAKE_REPLACEMENT = "�"
_MOJIBAKE_LINE_RATIO = 0.20  # report lines where >=20% of chars look corrupt
_MOJIBAKE_MIN_LINE_LEN = 8


def _detect_mojibake(text: str, source_hint: str = "") -> list[dict]:
    """Scan markdown for lines that look like failed glyph decoding.

    Returns a list of finding dicts with line_no, ratio, sample. Callers
    log these so PDFs with broken CMaps can be flagged for re-extraction
    or OCR fallback. We do not attempt to repair the text in-place —
    upstream extraction is the only place where the original glyphs can
    actually be recovered.
    """
    findings: list[dict] = []
    if not text:
        return findings
    for line_no, line in enumerate(text.split("\n"), 1):
        if len(line) < _MOJIBAKE_MIN_LINE_LEN:
            continue
        suspicious = len(_MOJIBAKE_HIGH_LATIN1.findall(line))
        replacement = line.count(_MOJIBAKE_REPLACEMENT)
        weighted = suspicious + replacement * 5
        ratio = weighted / max(1, len(line))
        if ratio >= _MOJIBAKE_LINE_RATIO:
            findings.append({
                "line_no": line_no,
                "ratio": round(ratio, 3),
                "suspicious_chars": suspicious,
                "replacement_chars": replacement,
                "sample": line[:160],
                "source": source_hint,
            })
    return findings


def _strip_br_in_table_rows(text: str) -> str:
    """Replace ``<br>`` tags inside markdown table rows with spaces.

    Using a space (not empty string) keeps stacked chart values distinct
    — ``|767<br>808|`` becomes ``|767 808|``, never ``|767808|``.
    """
    out: list[str] = []
    for line in text.split("\n"):
        if _TABLE_LINE_RE.match(line):
            line = _BR_TAG_RE.sub(" ", line)
            # Collapse runs of whitespace left by consecutive <br> tags.
            line = re.sub(r"[ \t]{2,}", " ", line)
        out.append(line)
    return "\n".join(out)


def _split_glued_comma_numbers(text: str) -> str:
    """Insert a space between adjacent comma-grouped numbers.

    pymupdf4llm / chart extraction sometimes emits ``1,5461,518`` instead of
    ``1,546 1,518``. Repeat until stable for longer glued runs.
    """
    prev = None
    while prev != text:
        prev = text
        text = _GLUED_COMMA_NUMBERS_RE.sub(r"\1 \2", text)
    return text


def _normalize_picture_text_blocks(text: str) -> str:
    """Normalize pymupdf4llm picture-text blocks for chunking + retrieval.

    - Rewrite HTML-comment markers to the markdown form StructuredChunker
      already recognizes.
    - Turn in-block ``<br>`` into newlines (not empty joins) so values like
      ``767`` and ``808`` stay separable.
    - Split glued comma-numbers inside the block.
    """

    def _rewrite(match: re.Match) -> str:
        body = match.group(1) or ""
        body = _BR_TAG_RE.sub("\n", body)
        body = _split_glued_comma_numbers(body)
        # Trim excess blank lines inside the block.
        body = re.sub(r"\n{3,}", "\n\n", body).strip("\n")
        return (
            "***----- Start of picture text -----***\n"
            f"{body}\n"
            "***----- End of picture text -----***"
        )

    return _PICTURE_TEXT_BLOCK_RE.sub(_rewrite, text)


_PAGE_MARKER_RE = re.compile(r"<!--\s*PAGE\s+(\d+)\s*-->")


def _recover_mojibake_pages(
    file_path,
    markdown: str,
    graphname=None,
    max_pages: int = 3,
) -> str:
    """Recover table/chart text from PDF pages with broken ToUnicode CMaps.

    When glyph mapping fails, embedded text often keeps numbers but corrupts
    labels. Drawn (non-embedded) figures also skip the normal image-describe
    pass. For page sections that look both corrupted and table-like, render
    the page and multimodal-transcribe it, then append the result next to the
    original page body. Capped by ``max_pages`` to bound cost.
    """
    if not markdown or max_pages <= 0:
        return markdown

    # Collect page numbers whose section text looks corrupted.
    parts = _PAGE_MARKER_RE.split(markdown)
    # parts: [pre, pageNo, body, pageNo, body, ...]
    bad_pages: list[int] = []
    if len(parts) >= 3:
        for i in range(1, len(parts), 2):
            try:
                page_no = int(parts[i])
            except (TypeError, ValueError):
                continue
            body = parts[i + 1] if i + 1 < len(parts) else ""
            findings = _detect_mojibake(body, source_hint=f"{file_path}:p{page_no}")
            # Prefer pages that look like broken tables (pipe rows + mojibake).
            pipe_rows = sum(1 for ln in body.splitlines() if ln.strip().startswith("|"))
            if len(findings) >= 3 and pipe_rows >= 3:
                bad_pages.append(page_no)
    if not bad_pages:
        return markdown

    try:
        import pymupdf
        from common.utils.image_data_extractor import (
            describe_image_with_llm,
            should_extract_images,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("mojibake page recovery unavailable: %s", e)
        return markdown

    if not should_extract_images(graphname):
        return markdown

    recovered: dict[int, str] = {}
    try:
        doc = pymupdf.open(str(file_path))
    except Exception as e:  # noqa: BLE001
        logger.warning("mojibake recovery: cannot open %s: %s", file_path, e)
        return markdown

    try:
        for page_no in bad_pages[:max_pages]:
            idx = page_no - 1
            if idx < 0 or idx >= doc.page_count:
                continue
            try:
                page = doc[idx]
                # ~150 dpi — enough to read table cells without huge payloads.
                pix = page.get_pixmap(matrix=pymupdf.Matrix(2.0, 2.0), alpha=False)
                tmp = Path(tempfile.mkdtemp(prefix="mojibake_page_")) / f"p{page_no}.png"
                pix.save(str(tmp))
                desc = describe_image_with_llm(str(tmp))
                try:
                    shutil.rmtree(tmp.parent, ignore_errors=True)
                except Exception:
                    pass
                if not desc or "decorative image" in desc.lower():
                    continue
                # Skip if the transcription itself looks glyph-broken.
                if len(_detect_mojibake(desc)) >= 3:
                    continue
                recovered[page_no] = desc.strip()
                logger.info(
                    "mojibake page recovery: %s page %s recovered %s chars",
                    file_path,
                    page_no,
                    len(desc),
                )
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "mojibake page recovery failed for %s p%s: %s",
                    file_path,
                    page_no,
                    e,
                )
    finally:
        doc.close()

    if not recovered:
        return markdown

    # Append recovered transcription under each page marker body.
    out_parts: list[str] = [parts[0]]
    for i in range(1, len(parts), 2):
        page_no_s = parts[i]
        body = parts[i + 1] if i + 1 < len(parts) else ""
        out_parts.append(f"<!-- PAGE {page_no_s} -->")
        out_parts.append(body)
        try:
            page_no = int(page_no_s)
        except (TypeError, ValueError):
            continue
        if page_no in recovered:
            out_parts.append(
                "\n\n<!-- recovered-from-page-image -->\n"
                + recovered[page_no]
                + "\n"
            )
    return "\n".join(out_parts)


def _collapse_vertical_cjk(text: str) -> str:
    """Collapse pymupdf4llm's per-character vertical-CJK runs back into a
    single token. Bold runs ``**X**<br>**Y**<br>**Z**`` become ``**XYZ**``;
    non-bold runs ``X<br>Y<br>Z`` become ``XYZ``.

    Only operates on runs of three or more contiguous CJK characters
    separated by ``<br>`` tags — incidental two-character ``<br>``-joined
    pairs aren't matched so we don't disturb legitimate inline content.
    """
    def _fix_bold(m: re.Match) -> str:
        chars = re.findall(rf"\*\*({_CJK_CHAR})\*\*", m.group(0))
        return f"**{''.join(chars)}**" if chars else m.group(0)

    def _fix_plain(m: re.Match) -> str:
        return re.sub(r"<br\s*/?>", "", m.group(0))

    text = _VERTICAL_BOLD_CJK.sub(_fix_bold, text)
    return _VERTICAL_CJK.sub(_fix_plain, text)


def _clean_pdf_markdown(markdown: str, source_hint: str = "") -> str:
    """Apply post-processing to markdown produced by pymupdf4llm for form PDFs.

    Three specific artefacts are fixed:

    1. **Duplicate table rows** — complex form PDFs (e.g. IRS forms) often have
       overlapping text layers (a rendered background layer plus a searchable text
       layer).  pymupdf4llm can emit the same row twice: once from the background
       layer (no formatting, missing spaces) and once from the text layer (bold,
       correct spacing).  The duplicate row that appears immediately after the
       original is removed; when the content is identical after stripping bold
       markers, the richer (longer) version is kept.

    2. **ColN placeholder headers** — pymupdf4llm uses "Col1", "Col2", … when it
       cannot derive a header from the PDF's column structure.  These are replaced
       with empty strings so the table is still valid markdown but does not expose
       internal artefacts to downstream consumers.

    3. **Vertical-CJK runs** — Japanese / Chinese / Korean characters laid out
       vertically in a PDF table cell get emitted as one character per line
       with ``<br>`` separators and per-character bold markers. The run is
       collapsed back into a single token so embedding and retrieval see the
       intended word (e.g. ``**個別信用購入あっせん**``) rather than ten
       fragments. Fullwidth digits are excluded so chart values are not glued.

    4. **Picture-text blocks** — figure text wrapped in
       ``<!-- Start of picture text -->`` (or the dash-marker form) by
       pymupdf4llm is rewritten so StructuredChunker keeps the block atomic,
       ``<br>`` becomes newlines, and glued comma-numbers like ``1,5461,518``
       are split.
    """
    # --- Pass 1: remove ColN placeholders ---
    markdown = _coln_pattern.sub('', markdown)

    # --- Pass 1b: normalize pymupdf4llm picture-text blocks before CJK/table
    # passes so chart <br> stacks become newlines rather than empty joins.
    markdown = _normalize_picture_text_blocks(markdown)

    # --- Pass 2: collapse vertical-CJK runs (do this BEFORE row dedup so
    # rows that differ only by the collapsed form aren't treated as
    # distinct rows).
    markdown = _collapse_vertical_cjk(markdown)

    # --- Pass 2b: strip <br> inside markdown table rows ---
    markdown = _strip_br_in_table_rows(markdown)

    # --- Pass 2c: split glued comma-grouped numbers globally ---
    markdown = _split_glued_comma_numbers(markdown)

    # --- Pass 2d: log lines that look like mojibake (failed glyph decode).
    # We don't repair these — the underlying glyphs aren't recoverable
    # from the markdown — but logging gives operators a grep target.
    findings = _detect_mojibake(markdown, source_hint)
    if findings:
        logger.warning(
            "[CONVERSION ISSUE] %s: %d line(s) look like mojibake / glyph-decode failure (first 3 shown)",
            source_hint or "<unknown source>",
            len(findings),
        )
        for f in findings[:3]:
            logger.warning(
                "[CONVERSION ISSUE]   line %d (ratio=%.2f, suspicious=%d, replacement=%d): %r",
                f["line_no"], f["ratio"], f["suspicious_chars"], f["replacement_chars"], f["sample"],
            )

    # --- Pass 3: deduplicate consecutive table rows ---
    lines = markdown.splitlines()
    cleaned: list[str] = []
    for line in lines:
        if cleaned and line.startswith('|') and cleaned[-1].startswith('|'):
            prev = cleaned[-1]
            norm_cur = re.sub(r'\*+', '', line).strip()
            norm_prev = re.sub(r'\*+', '', prev).strip()
            if norm_cur == norm_prev:
                if len(line) > len(prev):
                    cleaned[-1] = line
                continue
        cleaned.append(line)

    markdown = '\n'.join(cleaned)

    # --- Pass 4: collapse runs of 3+ blank lines into a single blank
    # line. pymupdf4llm emits large vertical whitespace where the PDF
    # has visual blank space (e.g. below a chart that fills most of a
    # page); these don't add information and bloat chunk sizes.
    markdown = re.sub(r"(?:\r?\n[ \t]*){3,}", "\n\n", markdown)

    return markdown


def extract_images(md_text):
    """
    Returns list of {"path": path, "image_id": image_id}
    image_id = basename without extension
    """
    images = []
    for m in _md_pattern.finditer(md_text):
        path = m.group(2)
        basename = os.path.basename(path)
        image_id = os.path.splitext(basename)[0]
        images.append({"path": path, "image_id": image_id})
    return images

def insert_description_by_id(md_text, image_id, description):
    """
    Replace the description for an image whose basename == image_id.
    """
    safe_desc = _sanitize_alt_text(description)

    def repl(m):
        old_path = m.group(2)
        candidate_id = os.path.splitext(os.path.basename(old_path))[0]

        if candidate_id == image_id:
            return f'![{safe_desc}]({old_path})'

        return m.group(0)
    return _md_pattern.sub(repl, md_text)


# Maximum characters retained from an LLM image description when
# rendered as markdown alt text. Long alt text bloats the chat
# rendering and offers no extra accessibility value beyond the first
# couple of sentences.
_ALT_TEXT_MAX_CHARS = 400


def _sanitize_alt_text(description: str) -> str:
    """Collapse an LLM image description into a single-line, markdown-
    safe alt-text string. The LLM is free to respond with headings,
    paragraph breaks and bracketed phrases; the markdown image syntax
    ``![alt](url)`` doesn't tolerate any of that — a newline or
    unescaped ``]`` terminates the construct and the renderer falls
    back to printing the raw text (the bug this guards against).
    """
    if not description:
        return ""
    text = str(description)
    # Drop a leading markdown heading like ``# Image Description``
    # the LLM tends to emit as a preamble.
    text = re.sub(r"^\s*#{1,6}\s*[^\n]*\n+", "", text, count=1)
    # Drop a literal "Image Description:" / "Description:" prefix that
    # the LLM occasionally writes in place of (or alongside) a heading.
    text = re.sub(r"^\s*(image\s+description|description)\s*:\s*", "", text, count=1, flags=re.IGNORECASE)
    # Replace every newline + run of whitespace with a single space.
    text = re.sub(r"\s+", " ", text).strip()
    # ``]`` would close the alt-text bracket; ``[`` can also confuse
    # some renderers. Swap both for round parens.
    text = text.replace("[", "(").replace("]", ")")
    if len(text) > _ALT_TEXT_MAX_CHARS:
        text = text[: _ALT_TEXT_MAX_CHARS - 1].rstrip() + "…"
    return text


def replace_path_with_tg_protocol(md_text, image_id, tg_reference):
    """
    Replace the file path for an image whose basename == image_id with tg:// protocol reference.
    tg_reference should be like 'Graphs_image_1'
    """
    def repl(m):
        old_path = m.group(2)
        candidate_id = os.path.splitext(os.path.basename(old_path))[0]

        if candidate_id == image_id:
            alt_text = m.group(1)
            return f'![{alt_text}](tg://{tg_reference})'

        return m.group(0)

    return _md_pattern.sub(repl, md_text)

class TextExtractor:
    """Class for handling text extraction from various file formats and cleanup."""

    def __init__(self):
        """Initialize the TextExtractor."""
        self.supported_extensions = {
            '.txt': 'text/plain',
            '.md': 'text/markdown',
            '.pdf': 'application/pdf',
            '.docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
            '.doc': 'application/msword',
            '.html': 'text/html',
            '.htm': 'text/html',
            '.json': 'application/json',
            '.csv': 'text/csv',
            '.xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            '.xls': 'application/vnd.ms-excel',
            '.xml': 'application/xml',
            '.jpeg': 'image/jpeg',
            '.jpg': 'image/jpeg',
            '.png': 'image/png',
            '.gif': 'image/gif',
            '.jsonl': 'application/x-jsonlines'
        }

    async def _process_file_async(self, file_path, graphname, temp_folder):
        """
        Async helper to process a single file.
        Runs in thread pool to avoid blocking on I/O operations.
        Creates one JSONL file per input file.

        Args:
            file_path: Absolute path to the input file to be processed (e.g., "C:/data/docs/report.pdf").
            graphname: Name of the knowledge graph this file belongs to, used for metadata tagging.
            temp_folder: Absolute path to the temporary directory where output JSONL files are written.
        """
        try:
            loop = asyncio.get_event_loop()

            doc_entries = await loop.run_in_executor(
                None,
                extract_text_from_file_with_images_as_docs,
                file_path,
                graphname
            )

            # Create one JSONL file per input file
            if doc_entries:
                # Use the original filename (stem) for the JSONL file
                file_stem = Path(file_path).stem
                jsonl_file = os.path.join(temp_folder, f"{file_stem}.jsonl")
                
                await loop.run_in_executor(
                    None,
                    self._write_to_jsonl,
                    jsonl_file,
                    doc_entries
                )
            
            # Return metadata only, documents already saved to JSONL
            return {
                'success': True,
                'file_path': str(file_path),
                'num_documents': len(doc_entries),
                'jsonl_file': f"{Path(file_path).stem}.jsonl"
            }

        except FileNotFoundError:
            return {'success': False, 'file_path': str(file_path), 'error': 'File not found'}
        except PermissionError:
            return {'success': False, 'file_path': str(file_path), 'error': 'Permission denied'}
        except Exception as e:
            logger.warning(f"Failed to process file {file_path}: {e}")
            return {'success': False, 'file_path': str(file_path), 'error': str(e)}
    
    def _write_to_jsonl(self, jsonl_file, doc_entries):
        """
        Write document entries to a JSONL file (one file per input file).
        Each document is written as a separate line.
        """
        with open(jsonl_file, 'w', encoding='utf-8') as f:
            for doc_data in doc_entries:
                json_line = json.dumps(doc_data, ensure_ascii=False)
                f.write(json_line + '\n')

    async def _process_folder_async(self, folder_path, graphname, temp_folder, filenames=None, max_concurrent=10):
        """
        Async version of process_folder for parallel file processing.
        Creates one JSONL file per input file.

        When *filenames* is supplied, only files whose basename appears
        in that list are processed; everything else in the folder is
        ignored. This lets a caller (e.g. the sample-doc schema-extraction
        flow) reuse a shared upload directory without re-converting
        files that belong to a previous request.
        """
        logger.info(f"Processing local folder ASYNC: {folder_path} for graph: {graphname} (max_concurrent={max_concurrent})")

        folder_path_obj = Path(folder_path)

        if not folder_path_obj.exists():
            raise Exception(f"Folder path does not exist: {folder_path}")

        if not folder_path_obj.is_dir():
            raise Exception(f"Path is not a directory: {folder_path}")

        # Create temp folder for JSONL files
        os.makedirs(temp_folder, exist_ok=True)
        logger.info(f"Saving processed documents to: {temp_folder}")

        allowed_basenames = set(filenames) if filenames is not None else None

        def safe_walk(path):
            try:
                for item in path.iterdir():
                    # ``_schema_*`` subdirs hold sample-doc staging
                    # and must not be re-ingested as regular documents.
                    if item.name.startswith(('.', '~', '$', '_schema_')) or 'BROMIUM' in item.name.upper():
                        continue
                    if item.is_file():
                        yield item
                    elif item.is_dir():
                        yield from safe_walk(item)
            except (PermissionError, OSError) as e:
                logger.warning(f"Cannot access directory {path}: {e}")

        files_to_process = []
        jsonl_files_copied = []
        cached_jsonl_skipped = []
        for file_path in safe_walk(folder_path_obj):
            if file_path.is_file():
                if file_path.name.startswith(('.', '~', '$')) or 'BROMIUM' in file_path.name.upper():
                    continue
                if allowed_basenames is not None and file_path.name not in allowed_basenames:
                    continue
                file_ext = file_path.suffix.lower()
                if file_ext == '.jsonl':
                    dest = os.path.join(temp_folder, file_path.name)
                    shutil.copy2(str(file_path), dest)
                    num_lines = sum(1 for _ in open(dest, 'r', encoding='utf-8'))
                    jsonl_files_copied.append({
                        'file_path': str(file_path),
                        'num_documents': num_lines,
                        'jsonl_file': file_path.name,
                        'status': 'success'
                    })
                    logger.info(f"Copied JSONL file directly: {file_path.name} ({num_lines} documents)")
                elif file_ext in self.supported_extensions:
                    # If a previous run (e.g. schema extraction) already
                    # produced a matching JSONL in *temp_folder*, reuse
                    # it instead of re-converting the source file. This
                    # saves the per-file PDF / image conversion cost
                    # when the user uploaded sample files via the
                    # Initialize Graph dialog and is now ingesting them.
                    cached_jsonl = os.path.join(
                        temp_folder, f"{file_path.stem}.jsonl"
                    )
                    if os.path.exists(cached_jsonl):
                        try:
                            num_lines = sum(
                                1 for _ in open(cached_jsonl, 'r', encoding='utf-8')
                            )
                        except Exception:
                            num_lines = 0
                        cached_jsonl_skipped.append({
                            'file_path': str(file_path),
                            'num_documents': num_lines,
                            'jsonl_file': os.path.basename(cached_jsonl),
                            'status': 'success',
                            'cached': True,
                        })
                        logger.info(
                            f"Reusing cached JSONL for {file_path.name} "
                            f"({num_lines} documents) — skipping re-conversion"
                        )
                    else:
                        files_to_process.append(file_path)

        logger.info(
            f"Found {len(files_to_process)} files to process, "
            f"{len(jsonl_files_copied)} JSONL files copied directly, "
            f"{len(cached_jsonl_skipped)} skipped via cached JSONL"
        )

        semaphore = asyncio.Semaphore(max_concurrent)

        async def process_with_semaphore(file_path):
            async with semaphore:
                return await self._process_file_async(file_path, graphname, temp_folder)

        tasks = [process_with_semaphore(fp) for fp in files_to_process]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        processed_files_info = list(jsonl_files_copied) + list(cached_jsonl_skipped)
        total_docs = sum(
            f['num_documents'] for f in jsonl_files_copied + cached_jsonl_skipped
        )

        for result in results:
            if isinstance(result, Exception):
                logger.error(f"File processing failed with exception: {result}")
                continue

            if result.get('success'):
                num_docs = result.get('num_documents', 0)
                total_docs += num_docs
                
                processed_files_info.append({
                    'file_path': result['file_path'],
                    'num_documents': num_docs,
                    'jsonl_file': result.get('jsonl_file'),
                    'status': 'success'
                })
            else:
                processed_files_info.append({
                    'file_path': result['file_path'],
                    'status': 'failed',
                    'error': result.get('error', 'Unknown error')
                })

        logger.info(f"Processed {len(processed_files_info)} files, extracted {total_docs} total documents")
        logger.info(f"Created {len([f for f in processed_files_info if f.get('status') == 'success'])} JSONL files in {temp_folder}")

        return {
            'statusCode': 200,
            'message': f'Processed {len(processed_files_info)} files, {total_docs} documents',
            'files': processed_files_info,
            'num_documents': total_docs,
            'temp_folder': temp_folder
        }

    def process_folder(self, folder_path, graphname, temp_folder):
        """
        Process local folder with multiple file formats and extract text content.
        Uses async processing internally for parallel file handling.
        Creates one JSONL file per input file.
        
        Args:
            folder_path: Path to the folder containing files to process
            graphname: Name of the graph (for context)
            temp_folder: Path to save processed documents as JSONL files (one per input file)
        """
        logger.info(f"Processing local folder: {folder_path} for graph: {graphname}")
        return asyncio.run(self._process_folder_async(folder_path, graphname, temp_folder))


def extract_text_from_file_with_images_as_docs(file_path, graphname=None):
    """
    Extract text and images from a file, treating images as separate document entries.
    """
    file_path = Path(file_path)
    extension = file_path.suffix.lower()
    base_doc_id = str(file_path.stem)

    logger.debug(f"Extracting with images as docs: {file_path} (type: {extension})")

    if extension == '.pdf':
        return _extract_pdf_with_images_as_docs(file_path, base_doc_id, graphname)
    elif extension in ['.jpeg', '.jpg', '.png', '.gif']:
        return _extract_standalone_image_as_doc(file_path, base_doc_id, graphname)
    else:
        content = extract_text_from_file(file_path, graphname)
        doc_type = get_doc_type_from_extension(extension)
        return [{
            "doc_id": base_doc_id,
            "doc_type": doc_type,
            "content": content,
            "position": 0
        }]

def _sanitize_image_filenames(image_folder, markdown_content):
    """Rename image files that contain spaces (replace with underscores).

    pymupdf4llm can produce filenames with spaces.  Renaming them avoids
    downstream issues with path parsing and markdown rendering.

    Returns the updated markdown_content with paths adjusted to match the
    renamed files.
    """
    if not image_folder.exists():
        return markdown_content

    for img_file in image_folder.iterdir():
        if not img_file.is_file() or ' ' not in img_file.name:
            continue
        new_name = img_file.name.replace(' ', '_')
        new_path = img_file.with_name(new_name)
        img_file.rename(new_path)
        old_ref = str(img_file)
        new_ref = str(new_path)
        markdown_content = markdown_content.replace(old_ref, new_ref)

    return markdown_content


def _extract_pdf_with_images_as_docs(file_path, base_doc_id, graphname=None):
    """
    Extract PDF as ONE markdown document with inline image references using pymupdf4llm.
    Uses unique temporary folder per PDF to allow parallel processing.
    After processing, delete the extracted image folder.
    """
    # Use a unique ABSOLUTE temp folder per PDF.
    # A relative path would resolve to whatever the process CWD happens to be at
    # call time (varies across ThreadPoolExecutor threads in container deployments).
    # pymupdf4llm embeds os.path.join(image_path, filename) in the markdown, so an
    # absolute image_path produces absolute embedded paths that PIL can always open
    # regardless of CWD.
    image_output_folder = Path(tempfile.mkdtemp(prefix="tg_pdf_"))

    try:
        import pymupdf4llm
        from PIL import Image as PILImage
        from common.utils.image_data_extractor import (
            describe_image_with_llm,
            image_describe_workers,
            is_decorative,
            min_image_dim_px,
            should_extract_images,
        )

        _is_decorative = is_decorative

        # Ensure clean slate - remove folder if it exists from failed previous run
        if image_output_folder.exists():
            shutil.rmtree(image_output_folder, ignore_errors=True)

        # Convert PDF to markdown with extracted image files.
        # Use lock because pymupdf4llm's table extraction is not thread-safe
        # (https://github.com/pymupdf/PyMuPDF/issues/3241).
        #
        # page_chunks=True returns a list[dict] (one per page) carrying
        # per-page metadata. We re-join into a single markdown string with
        # `<!-- PAGE N -->` markers between pages so the structured chunker
        # (common/chunkers/structured.py) can attach page_no to each
        # emitted chunk. Markdown / character / semantic chunkers ignore
        # the comments — they're inert HTML comments to those chunkers.
        def _to_markdown_paged(strategy: str | None = None):
            kwargs = dict(
                write_images=True,
                image_path=str(image_output_folder),
                margins=0,
                image_size_limit=0.08,
                page_chunks=True,
            )
            if strategy:
                kwargs["table_strategy"] = strategy
            pages = pymupdf4llm.to_markdown(file_path, **kwargs)
            if not isinstance(pages, list):
                return pages or ""
            parts = []
            for p in pages:
                page_no = None
                meta = p.get("metadata") or {}
                # pymupdf4llm exposes the page index under ``page_number``
                # (1-based) in each chunk's metadata. ``page`` is the
                # filename-style label and not always populated.
                for key in ("page_number", "page"):
                    if key in meta:
                        try:
                            page_no = int(meta[key])
                            break
                        except (TypeError, ValueError):
                            page_no = None
                if page_no is not None:
                    parts.append(f"<!-- PAGE {page_no} -->")
                parts.append(p.get("text") or "")
            return "\n\n".join(parts)

        with _pymupdf4llm_lock:
            try:
                markdown_content = _to_markdown_paged()
            except Exception:
                # Retry with table_strategy="lines" if first attempt fails
                try:
                    markdown_content = _to_markdown_paged(strategy="lines")
                except Exception as e:
                    logger.error(f"pymupdf4llm failed for {file_path}: {e}")
                    # Cleanup folder if it was created
                    if image_output_folder.exists():
                        shutil.rmtree(image_output_folder, ignore_errors=True)
                    return [{
                        "doc_id": base_doc_id,
                        "doc_type": "markdown",
                        "content": f"[PDF extraction failed: {e}]",
                        "position": 0
                    }]

        if not markdown_content or not markdown_content.strip():
            logger.warning(
                f"No text layer found in PDF: {file_path}. "
                "The file may be a scanned image-only PDF — consider enabling OCR."
            )
            if image_output_folder.exists():
                shutil.rmtree(image_output_folder, ignore_errors=True)
            return [{
                "doc_id": base_doc_id,
                "doc_type": "markdown",
                "content": f"[Scanned PDF — no text layer extracted: {file_path.name}]",
                "position": 0
            }]

        # Clean up artefacts common in form PDFs (duplicate rows, ColN headers)
        markdown_content = _clean_pdf_markdown(markdown_content, source_hint=str(file_path))

        # Pages with broken CMaps (mojibake row labels, intact numbers) need a
        # page-screenshot multimodal pass — embedded images are often absent.
        markdown_content = _recover_mojibake_pages(
            file_path, markdown_content, graphname=graphname
        )

        # Rename image files that contain spaces to avoid path-parsing issues
        markdown_content = _sanitize_image_filenames(image_output_folder, markdown_content)

        # Extract image references from markdown
        image_refs = extract_images(markdown_content)

        if not image_refs:
            # cleanup folder anyway
            if image_output_folder.exists():
                shutil.rmtree(image_output_folder, ignore_errors=True)

            return [{
                "doc_id": base_doc_id,
                "doc_type": "markdown",
                "content": markdown_content,
                "position": 0
            }]
        # Phase 1 — describe + base64-encode every image in parallel.
        # Each worker is I/O-bound (one multimodal request + a disk read),
        # so a thread pool cuts wall-clock proportionally for image-heavy
        # PDFs. Markdown mutations stay in phase 2 because
        # insert_description_by_id / replace_path_with_tg_protocol mutate
        # the same shared string and must run in deterministic order.
        image_workers = image_describe_workers(graphname)
        extract_images_enabled = should_extract_images(graphname)
        min_dim = min_image_dim_px(graphname)

        def _describe_and_encode(img_ref: dict) -> dict:
            """Run on a worker thread. Returns one of:
              * ``{"ok": True, "img_ref", "description", "image_base64",
                  "width", "height"}``
              * ``{"ok": True, "img_ref", "skip": True}`` for decorative
                or too-small images that should be dropped from the JSONL
              * ``{"ok": False, "img_ref", "error"}``
            Never raises.
            """
            try:
                img_path = Path(img_ref["path"])
                with PILImage.open(img_path) as pil_image:
                    too_small = (
                        pil_image.width < min_dim or pil_image.height < min_dim
                    )
                    if not extract_images_enabled or too_small:
                        return {"ok": True, "skip": True, "img_ref": img_ref}
                    description = describe_image_with_llm(str(img_path))
                    if _is_decorative(description):
                        return {"ok": True, "skip": True, "img_ref": img_ref}
                    rgb_image = pil_image if pil_image.mode == "RGB" else pil_image.convert("RGB")
                    buffer = io.BytesIO()
                    rgb_image.save(buffer, format="JPEG", quality=95)
                    image_base64 = base64.b64encode(buffer.getvalue()).decode("utf-8")
                    return {
                        "ok": True,
                        "img_ref": img_ref,
                        "description": description,
                        "image_base64": image_base64,
                        "width": pil_image.width,
                        "height": pil_image.height,
                    }
            except Exception as img_error:  # noqa: BLE001 — keep going
                return {"ok": False, "img_ref": img_ref, "error": img_error}

        if image_refs:
            with ThreadPoolExecutor(
                max_workers=max(1, min(image_workers, len(image_refs)))
            ) as ex:
                # executor.map preserves input ordering, which is what
                # the markdown-mutation phase below relies on.
                described = list(ex.map(_describe_and_encode, image_refs))
        else:
            described = []

        # Phase 2 — apply markdown mutations and build image_entries
        # in deterministic order using the parallel results.
        image_entries: list[dict] = []
        image_counter = 0
        for d in described:
            img_ref = d["img_ref"]
            if not d.get("ok"):
                logger.warning(
                    f"Failed to process image {img_ref.get('path')}: {d.get('error')}"
                )
                failed_path = img_ref.get("path", "")
                if failed_path:
                    markdown_content = re.sub(
                        r'!\[.*?\]\(' + re.escape(failed_path) + r'\)',
                        "",
                        markdown_content,
                    )
                continue

            if d.get("skip"):
                skipped_path = img_ref.get("path", "")
                if skipped_path:
                    markdown_content = re.sub(
                        r'!\[.*?\]\(' + re.escape(skipped_path) + r'\)',
                        "",
                        markdown_content,
                    )
                continue

            image_id = img_ref["image_id"]
            markdown_content = insert_description_by_id(
                markdown_content, image_id, d["description"]
            )

            image_counter += 1
            image_doc_id = f"{base_doc_id}_image_{image_counter}".lower()
            markdown_content = replace_path_with_tg_protocol(
                markdown_content, image_id, image_doc_id
            )
            image_entries.append({
                "doc_id": image_doc_id,
                "doc_type": "image",
                "image_description": d["description"],
                "image_data": d["image_base64"],
                "image_format": "jpg",
                "parent_doc": base_doc_id,
                "page_number": 0,
                "width": d["width"],
                "height": d["height"],
                "position": image_counter,
            })

        # FINAL CLEANUP — delete folder after processing everything
        if image_output_folder.exists() and image_output_folder.is_dir():
            try:
                shutil.rmtree(image_output_folder)
                logger.debug(f"Deleted image folder: {image_output_folder}")
            except Exception as delete_err:
                logger.warning(f"Failed to delete folder {image_output_folder}: {delete_err}")

        # Build final result
        result = [{
            "doc_id": base_doc_id,
            "doc_type": "markdown",
            "content": markdown_content,
            "position": 0
        }]
        result.extend(image_entries)
        return result

    except ImportError as import_err:
        logger.error(f"Required library missing: {import_err}")
        # Cleanup on import error
        if image_output_folder.exists():
            shutil.rmtree(image_output_folder, ignore_errors=True)
        return [{
            "doc_id": base_doc_id,
            "doc_type": "markdown",
            "content": "[PDF extraction requires pymupdf4llm and PyMuPDF]",
            "position": 0
        }]
    except Exception as e:
        logger.error(f"Error extracting PDF: {e}")
        # Cleanup on any other error
        if image_output_folder.exists():
            shutil.rmtree(image_output_folder, ignore_errors=True)
        raise

def _extract_standalone_image_as_doc(file_path, base_doc_id, graphname=None):
    """
    Extract standalone image file as ONE markdown document with inline image reference.
    """
    try:
        from PIL import Image as PILImage
        from common.utils.image_data_extractor import (
            describe_image_with_llm,
            is_decorative,
            min_image_dim_px,
            should_extract_images,
        )

        pil_image = PILImage.open(file_path)
        min_dim = min_image_dim_px(graphname)
        if not should_extract_images(graphname) or (
            pil_image.width < min_dim or pil_image.height < min_dim
        ):
            logger.info(
                f"Skipping standalone image {file_path}: decorative or below "
                f"min dimension ({min_dim}px)"
            )
            return []
        description = describe_image_with_llm(str(Path(file_path).absolute()))
        if is_decorative(description):
            logger.info(
                f"Skipping standalone image {file_path}: LLM marked as decorative"
            )
            return []
        buffer = io.BytesIO()
        if pil_image.mode != 'RGB':
            pil_image = pil_image.convert('RGB')
        pil_image.save(buffer, format="JPEG", quality=95)
        image_base64 = base64.b64encode(buffer.getvalue()).decode('utf-8')

        image_id = f"{base_doc_id}_image_1".lower()
        content = f"![{description}](tg://{image_id})"
        return [
            {
                "doc_id": base_doc_id,
                "doc_type": "image",
                "content": content,
                "position": 0
            },
            {
                "doc_id": image_id,
                "doc_type": "image",
                "image_description": description,
                "image_data": image_base64,
                "image_format": "jpg",
                "parent_doc": base_doc_id,
                "page_number": 0,
                "width": pil_image.width,
                "height": pil_image.height,
                "position": 1
            }
        ]

    except Exception as e:
        logger.error(f"Error extracting image: {e}")
        return [{
            "doc_id": base_doc_id,
            "doc_type": "markdown",
            "content": f"[Image extraction failed: {str(e)}]",
            "position": 0
        }]


def extract_text_from_file(file_path, graphname=None):
    """
    Extract text content from a file based on its extension.
    """
    file_path = Path(file_path)
    extension = file_path.suffix.lower()

    logger.debug(f"Extracting text from {file_path} (type: {extension}) for graph: {graphname}")

    try:
        if extension in ['.txt', '.md']:
            with open(file_path, 'r', encoding='utf-8') as f:
                return f.read().strip()
        elif extension in ['.html', '.htm']:
            with open(file_path, 'r', encoding='utf-8') as f:
                return f.read().strip()
        elif extension == '.csv':
            raw = file_path.read_bytes()
            # utf-8-sig handles UTF-8 with BOM (common Excel CSV export)
            try:
                return raw.decode('utf-8-sig').strip()
            except UnicodeDecodeError:
                pass
            # Fall back to chardet detection
            import chardet
            detected = chardet.detect(raw)
            encoding = detected.get('encoding') if detected.get('confidence', 0) >= 0.5 else None
            # latin-1 as final fallback — never raises DecodeError
            return raw.decode(encoding or 'latin-1').strip()
        elif extension == '.json':
            with open(file_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            return json.dumps(data, indent=2, ensure_ascii=False)
        elif extension == '.docx':
            import docx
            doc = docx.Document(file_path)
            return "\n".join(p.text for p in doc.paragraphs if p.text.strip())
        elif extension in ['.xlsx', '.xls']:
            import pandas as pd
            engine = 'openpyxl' if extension == '.xlsx' else 'xlrd'
            try:
                xl = pd.ExcelFile(file_path, engine=engine)
            except Exception:
                xl = pd.ExcelFile(file_path)
            sheet_texts = []
            for sheet_name in xl.sheet_names:
                # Always read with header=None so no data row is silently
                # consumed as column names for headerless spreadsheets.
                df = xl.parse(sheet_name, header=None)
                if df.empty:
                    continue
                df = df.fillna('')
                first_row = df.iloc[0]
                first_row_values = [str(v).strip() for v in first_row]
                looks_like_header = (
                    len(df) > 1
                    and all(first_row_values)
                    and len(set(first_row_values)) == len(first_row_values)
                    and not any(v.isdigit() for v in first_row_values)
                )
                if looks_like_header:
                    df.columns = first_row_values
                    df = df.iloc[1:].reset_index(drop=True)
                else:
                    df.columns = [f"Column {i + 1}" for i in range(len(df.columns))]
                sheet_md = df.to_markdown(index=False)
                sheet_texts.append(f"## Sheet: {sheet_name}\n\n{sheet_md}")
            return "\n\n".join(sheet_texts) if sheet_texts else "[Excel file is empty or contains no data]"
        elif extension == '.xml':
            import xml.etree.ElementTree as ET
            tree = ET.parse(file_path)
            root = tree.getroot()

            def extract_text_from_element(element):
                text = element.text or ""
                for child in element:
                    text += " " + extract_text_from_element(child)
                if element.tail:
                    text += " " + element.tail
                return text.strip()

            content = extract_text_from_element(root)
            import re
            return re.sub(r'\s+', ' ', content).strip()
        else:
            return f"[Unsupported file type: {extension}]"

    except Exception as e:
        logger.error(f"Error extracting text from {file_path}: {e}")
        raise Exception(f"Text extraction failed: {e}")


def get_doc_type_from_extension(extension):
    """Map file extension to a chunker-compatible document type."""
    if not extension.startswith('.'):
        extension = '.' + extension
    extension = extension.lower()

    if extension in ['.html', '.htm']:
        return 'html'
    elif extension in ['.jpg', '.jpeg', '.png', '.gif', '.bmp', '.tiff', '.webp']:
        return 'image'
    else:
        return 'markdown'

def get_supported_extensions():
    """Get list of supported file extensions."""
    return {'.txt', '.md', '.html', '.htm', '.csv', '.json', '.pdf', '.docx', '.doc', '.xml', '.jpeg', '.jpg', '.png', '.gif', '.xlsx', '.xls', '.jsonl'}

def is_supported_file(file_path):
    """Check if a file is supported for text extraction."""
    extension = Path(file_path).suffix.lower()
    return extension in get_supported_extensions()