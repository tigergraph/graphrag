"""Document extraction with Docling.

Docling detects each element of a document — headings with their levels,
paragraphs, lists, tables, pictures, formulas, captions, page headers and
footers — and its page. This module writes that structure as HTML for the
structured chunker:

- ``<body data-parser="docling">`` marks the format, so the chunker renders
  each chunk as Markdown.
- Every block carries ``data-page`` when the source has pages.
- Headings use ``<h1>``–``<h6>`` by their detected level.
- Tables keep merged cells; each caption stays with its table or figure.
- Charts and other pictures become ``<figure>`` elements with the image
  reference and the full description from the multimodal model.
- Formulas become LaTeX, transcribed by the multimodal model.
- Page headers, page footers, logos and icons are left out.
"""
import base64
import contextvars
import html
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
_SKIP_TEXT_LABELS = {"page_header", "page_footer"}

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


# --- HTML writer ------------------------------------------------------------


# Some PDF text layers map glyphs to control characters.
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def _esc(text: str) -> str:
    return html.escape(_CONTROL_CHARS.sub(" ", text or ""), quote=True)


def _page(item):
    prov = getattr(item, "prov", None) or []
    return prov[0].page_no if prov else None


def _attrs(item, **extra) -> str:
    parts = []
    page = _page(item)
    if page is not None:
        parts.append(f'data-page="{page}"')
    for k, v in extra.items():
        if v:
            parts.append(f'{k.replace("_", "-")}="{_esc(str(v))}"')
    return (" " + " ".join(parts)) if parts else ""


def _picture_class(pic) -> str:
    meta = getattr(pic, "meta", None)
    cls = getattr(meta, "classification", None) if meta else None
    preds = getattr(cls, "predictions", None) if cls else None
    return preds[0].class_name if preds else ""


def _label(item) -> str:
    label = getattr(item, "label", "")
    return str(getattr(label, "value", label))


class _Writer:
    """Collect the HTML blocks of one document in reading order."""

    def __init__(self):
        self.blocks: list[str] = []
        self._list_parent = None
        self._list_tag = None

    def close_list(self):
        if self._list_tag:
            self.blocks.append(f"</{self._list_tag}>")
        self._list_parent = None
        self._list_tag = None

    def list_item(self, item):
        parent = item.parent.cref if item.parent else None
        tag = "ol" if getattr(item, "enumerated", False) else "ul"
        if parent != self._list_parent or tag != self._list_tag:
            self.close_list()
            self.blocks.append(f"<{tag}{_attrs(item)}>")
            self._list_parent, self._list_tag = parent, tag
        self.blocks.append(f"<li>{_esc(item.text)}</li>")

    def add(self, block: str):
        self.close_list()
        self.blocks.append(block)


def document_to_html(doc, figures: dict, formulas: dict) -> str:
    """Write a DoclingDocument as HTML for the structured chunker.

    ``figures`` maps a picture's reference to ``{"src", "description"}``, or
    to ``None`` to leave the picture out. ``formulas`` maps a formula's
    reference to its LaTeX.
    """
    from docling_core.types.doc.document import (
        CodeItem, FormulaItem, ListItem, PictureItem, SectionHeaderItem,
        TableItem, TitleItem,
    )

    captions = set()
    for item in list(doc.tables) + list(doc.pictures):
        for ref in getattr(item, "captions", []) or []:
            captions.add(ref.cref)

    w = _Writer()
    for item, _level in doc.iterate_items(traverse_pictures=False):
        label = _label(item)
        if label in _SKIP_TEXT_LABELS or item.self_ref in captions:
            continue
        if isinstance(item, ListItem):
            if item.text.strip():
                w.list_item(item)
            continue
        if isinstance(item, TitleItem):
            w.add(f"<h1{_attrs(item)}>{_esc(item.text)}</h1>")
        elif isinstance(item, SectionHeaderItem):
            level = min(max(int(getattr(item, "level", 1) or 1), 1), 6)
            w.add(f"<h{level}{_attrs(item)}>{_esc(item.text)}</h{level}>")
        elif isinstance(item, TableItem):
            table = item.export_to_html(doc=doc, add_caption=False)
            if not table.strip():
                continue
            caption = item.caption_text(doc)
            # Some docling-core versions add the caption themselves.
            if caption and "<caption" not in table:
                table = table.replace("<table>", f"<table><caption>{_esc(caption)}</caption>", 1)
            w.add(table.replace("<table", f"<table{_attrs(item)}", 1))
        elif isinstance(item, PictureItem):
            fig = figures.get(item.self_ref)
            caption = item.caption_text(doc)
            if fig is None and not caption:
                continue
            parts = [f"<figure{_attrs(item, data_kind=_picture_class(item))}>"]
            if fig and fig.get("src"):
                parts.append(f'<img src="{_esc(fig["src"])}" alt="{_esc(caption)}">')
            if caption:
                parts.append(f"<figcaption>{_esc(caption)}</figcaption>")
            if fig and fig.get("description"):
                parts.append(f'<div class="figure-description">{_esc(fig["description"])}</div>')
            parts.append("</figure>")
            w.add("".join(parts))
        elif isinstance(item, FormulaItem):
            latex = formulas.get(item.self_ref) or item.text
            if latex.strip():
                w.add(f'<div class="formula"{_attrs(item)}>$${_esc(latex)}$$</div>')
        elif isinstance(item, CodeItem):
            w.add(f"<pre{_attrs(item)}>{_esc(item.text)}</pre>")
        else:
            text = getattr(item, "text", "") or ""
            if not text.strip():
                continue
            cls = ' class="footnote"' if label == "footnote" else ""
            w.add(f"<p{cls}{_attrs(item)}>{_esc(text)}</p>")
    w.close_list()
    return '<html><body data-parser="docling">\n' + "\n".join(w.blocks) + "\n</body></html>"


# --- extraction -------------------------------------------------------------


def extract_with_docling(file_path, base_doc_id, graphname=None) -> list:
    """Extract one file with Docling.

    Returns the same entries as the other extractors: the document itself
    (``doc_type`` "html"), then one entry per described image.
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

    content = document_to_html(doc, figures, formulas)
    return [{"doc_id": base_doc_id, "doc_type": "html", "content": content, "position": 0}] + images
