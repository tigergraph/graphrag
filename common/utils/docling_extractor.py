"""Document extraction with Docling.

Docling detects each element of a document — headings with their levels,
paragraphs, lists, tables, pictures, formulas, captions, page headers and
footers — and its page. This module writes that structure as Markdown in the
same shape as the default PDF extractor, so the structured chunker reads both
the same way:

- A ``<!-- PAGE n -->`` marker starts each page when the source has pages.
- Headings use ``#``–``######`` by their detected level.
- Tables are Markdown tables; each caption stays with its table or figure.
- Charts and other pictures become ``![description](tg://id)`` links to the
  image documents described by the multimodal model.
- Formulas become LaTeX, transcribed by the multimodal model.
- Page headers, page footers, page numbers, logos and icons are left out.
"""
import base64
import contextvars
import io
import logging
import os
import re
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

logger = logging.getLogger(__name__)

# Formats Docling parses when the document parser is "docling".
DOCLING_EXTENSIONS = {".pdf", ".docx", ".pptx", ".xlsx", ".html", ".htm", ".csv", ".xml"}
# PowerPoint has no other extractor, so Docling always reads it.
DOCLING_ONLY_EXTENSIONS = {".pptx"}

# Picture classes that carry no content for retrieval.
_SKIP_PICTURE_CLASSES = {"logo", "icon", "qr_code", "bar_code", "signature", "stamp"}

_converter = None
_converter_lock = threading.Lock()
# Docling's models are not documented as safe for concurrent use.
_convert_lock = threading.Lock()


def document_parser(graphname=None) -> str:
    """The ``document_parser`` setting of a graph: "default" or "docling"."""
    try:
        from common.config import get_graphrag_config
        cfg = get_graphrag_config(graphname) or {}
    except Exception:
        cfg = {}
    value = str(cfg.get("document_parser") or "default").strip().lower()
    return value if value in ("default", "docling") else "default"


def use_docling(extension: str, graphname=None) -> bool:
    """Whether Docling extracts a file with this extension."""
    extension = extension.lower()
    if extension in DOCLING_ONLY_EXTENSIONS:
        return True
    return extension in DOCLING_EXTENSIONS and document_parser(graphname) == "docling"


def docling_available() -> bool:
    try:
        import docling  # noqa: F401
        return True
    except ImportError:
        return False


def _get_converter():
    global _converter
    with _converter_lock:
        if _converter is not None:
            return _converter
        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.pipeline_options import PdfPipelineOptions
        from docling.document_converter import DocumentConverter, PdfFormatOption

        opts = PdfPipelineOptions()
        opts.do_table_structure = True
        opts.heading_hierarchy_options.enabled = True
        opts.do_picture_classification = True
        opts.generate_picture_images = True
        # Page images let formulas be cropped for transcription.
        opts.generate_page_images = True
        opts.images_scale = 2.0
        opts.ocr_options.replace_broken_text_layer = True
        artifacts = os.environ.get("DOCLING_ARTIFACTS_PATH")
        if artifacts:
            opts.artifacts_path = artifacts
        _converter = DocumentConverter(
            format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=opts)}
        )
        return _converter


def convert(file_path):
    """Parse a file into a DoclingDocument."""
    conv = _get_converter()
    with _convert_lock:
        result = conv.convert(Path(file_path))
    return result.document


# --- Markdown writer --------------------------------------------------------

def _picture_class(pic) -> str:
    meta = getattr(pic, "meta", None)
    cls = getattr(meta, "classification", None) if meta else None
    preds = getattr(cls, "predictions", None) if cls else None
    return preds[0].class_name if preds else ""


_IMAGE_SLOT = "<!-- docling-image -->"
_HEAD_RE = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")
_PAGE_RE = re.compile(r"^<!-- PAGE \d+ -->$")
_BULLETS = ("●", "•", "・", "※", "○", "◆")
# Text found on this many pages is a running header or footer, not content.
_RUNNING_MIN_PAGES = 3
_RUNNING_MAX_CHARS = 120
# Some PDF text layers map glyphs to control characters.
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def _norm(text: str) -> str:
    return re.sub(r"\d+", "#", re.sub(r"\s+", " ", text)).strip()


def _body_like(text: str) -> bool:
    """A heading that reads as a sentence or a bullet is body text."""
    t = text.strip()
    return t.endswith("。") or "。" in t[:-1] or t.startswith(_BULLETS)


def _clean_markdown(blocks: list) -> list:
    """Drop running headers and footers and headings that start no section;
    turn body-like headings into paragraphs. ``blocks`` holds
    ``(page, text)`` pairs."""
    # Running text sits among the first or last two blocks of a page.
    edge = set()
    by_page = {}
    for i, (page, _) in enumerate(blocks):
        by_page.setdefault(page, []).append(i)
    for idx in by_page.values():
        edge.update(idx[:2] + idx[-2:])

    def key(i):
        text = blocks[i][1]
        if i not in edge or len(text) > _RUNNING_MAX_CHARS or text.startswith(("|", "![", "<!--")):
            return None
        return _norm(_HEAD_RE.sub(r"\2", text)) or None

    pages = {}
    for i, (page, _) in enumerate(blocks):
        k = key(i)
        if k:
            pages.setdefault(k, set()).add(page)
    running = {k for k, v in pages.items() if len(v) >= _RUNNING_MIN_PAGES}

    out = []
    for i, (page, text) in enumerate(blocks):
        if key(i) in running or (i in edge and re.fullmatch(r"[-–]?\s*\d{1,4}\s*[-–]?", text)):
            continue
        m = _HEAD_RE.match(text)
        if m and _body_like(m.group(2)):
            text = m.group(2)
        out.append((page, text))

    # A heading followed by a heading of the same or a higher level, or by
    # nothing, starts no section.
    keep = []
    for i, (page, text) in enumerate(out):
        m = _HEAD_RE.match(text)
        if m:
            nxt = next((t for _, t in out[i + 1:]), None)
            n = _HEAD_RE.match(nxt) if nxt else None
            if nxt is None or (n and len(n.group(1)) <= len(m.group(1))):
                continue
        keep.append((page, text))
    return keep


def document_to_markdown(doc, figures: dict, formulas: dict) -> str:
    """Write a DoclingDocument as Markdown in the same shape as the default
    PDF extractor: ``<!-- PAGE n -->`` markers and ``![alt](tg://id)``
    images.

    ``figures`` maps a picture's reference to ``{"src", "description"}``, or
    to ``None`` to leave the picture out. ``formulas`` maps a formula's
    reference to its LaTeX.
    """
    from docling_core.types.doc.base import ImageRefMode
    from docling_core.types.doc.common.content_layer import ContentLayer
    from docling_core.types.doc.document import FormulaItem, PictureItem
    from common.utils.text_extractors import _sanitize_alt_text

    for item in doc.texts:
        if str(getattr(item.label, "value", item.label)) in ("page_header", "page_footer"):
            item.content_layer = ContentLayer.FURNITURE
    for pic in doc.pictures:
        fig = figures.get(pic.self_ref)
        if fig is None and not pic.caption_text(doc):
            pic.content_layer = ContentLayer.FURNITURE
    for item, _ in doc.iterate_items():
        if isinstance(item, FormulaItem) and formulas.get(item.self_ref):
            item.text = formulas[item.self_ref]

    page_nos = sorted(doc.pages) if doc.pages else [None]
    blocks = []
    for page in page_nos:
        md = doc.export_to_markdown(
            page_no=page, image_mode=ImageRefMode.PLACEHOLDER,
            image_placeholder=_IMAGE_SLOT, include_annotations=False,
            include_picture_classification=False, compact_tables=True,
            escape_html=False, escape_underscores=False,
        )
        pics = [it for it, _ in doc.iterate_items(page_no=page)
                if isinstance(it, PictureItem)]
        slots = md.count(_IMAGE_SLOT)
        if slots != len(pics):
            logger.warning(f"Docling page {page}: {slots} image slots for {len(pics)} pictures")
        for pic in pics:
            fig = figures.get(pic.self_ref) or {}
            img = (f"![{_sanitize_alt_text(fig['description'])}]({fig['src']})"
                   if fig.get("src") else "")
            md = md.replace(_IMAGE_SLOT, img, 1)
        md = _CONTROL_CHARS.sub(" ", md.replace(_IMAGE_SLOT, ""))
        for part in re.split(r"\n\s*\n", md):
            part = part.strip()
            if part:
                blocks.append((page, part))

    out, cur = [], object()
    for page, text in _clean_markdown(blocks):
        if page is not None and page != cur:
            out.append(f"<!-- PAGE {page} -->")
            cur = page
        out.append(text)
    return "\n\n".join(out)


# --- extraction -------------------------------------------------------------


def extract_with_docling(file_path, base_doc_id, graphname=None) -> list:
    """Extract one file with Docling.

    Returns the same entries as the other extractors: the document itself
    (``doc_type`` "markdown"), then one entry per described image.
    """
    from docling_core.types.doc.document import FormulaItem
    from common.utils.image_data_extractor import (
        describe_image_with_llm, image_describe_workers, is_decorative,
        min_image_dim_px, should_extract_images, transcribe_formula_with_llm,
    )

    doc = convert(file_path)
    extract_images = should_extract_images(graphname)
    min_dim = min_image_dim_px(graphname)

    work = []  # (kind, ref, PIL image)
    for pic in doc.pictures:
        if _picture_class(pic) in _SKIP_PICTURE_CLASSES or not extract_images:
            continue
        img = pic.get_image(doc)
        if img is None or img.width < min_dim or img.height < min_dim:
            continue
        work.append(("picture", pic.self_ref, img))
    for item, _ in doc.iterate_items():
        if isinstance(item, FormulaItem):
            img = item.get_image(doc)
            if img is not None:
                work.append(("formula", item.self_ref, img))

    tmp = Path(tempfile.mkdtemp(prefix="tg_docling_"))

    def _run(job):
        kind, ref, img = job
        path = tmp / f"{ref.strip('#/').replace('/', '_')}.png"
        img.convert("RGB").save(path)
        try:
            if kind == "formula":
                return kind, ref, img, transcribe_formula_with_llm(str(path))
            return kind, ref, img, describe_image_with_llm(str(path))
        except Exception as exc:  # noqa: BLE001 — one failure must not stop the file
            logger.warning(f"Docling {kind} {ref} in {Path(file_path).name}: {exc}")
            return kind, ref, img, ""

    try:
        results = []
        if work:
            workers = max(1, min(image_describe_workers(graphname), len(work)))
            with ThreadPoolExecutor(max_workers=workers) as ex:
                contexts = [contextvars.copy_context() for _ in work]
                results = list(ex.map(lambda p: p[0].run(_run, p[1]), zip(contexts, work)))
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)

    figures, formulas, images = {}, {}, []
    for kind, ref, img, text in results:
        if kind == "formula":
            if text:
                formulas[ref] = text
            continue
        if not text or is_decorative(text) or text.startswith("Image: "):
            figures[ref] = None
            continue
        image_doc_id = f"{base_doc_id}_image_{len(images) + 1}".lower()
        buf = io.BytesIO()
        img.convert("RGB").save(buf, format="JPEG", quality=95)
        figures[ref] = {"src": f"tg://{image_doc_id}", "description": text}
        images.append({
            "doc_id": image_doc_id,
            "doc_type": "image",
            "image_description": text,
            "image_data": base64.b64encode(buf.getvalue()).decode("utf-8"),
            "image_format": "jpg",
            "parent_doc": base_doc_id,
            "page_number": 0,
            "width": img.width,
            "height": img.height,
            "position": len(images) + 1,
        })

    content = document_to_markdown(doc, figures, formulas)
    return [{"doc_id": base_doc_id, "doc_type": "markdown", "content": content, "position": 0}] + images
