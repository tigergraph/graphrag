"""Heading path and summary of a document chunk, kept apart from its text.

A graph that has the ``heading_path`` and ``summary`` attributes on
DocumentChunk stores the chunk body in ``Content.text``, the section
breadcrumb in ``heading_path`` and the summary from entity extraction in
``summary``. Entity extraction reads the body only. The embedding reads the
heading path, the summary and the body. The retrieval queries show the heading
path before the body.

A graph without the attributes keeps the older form: the breadcrumb is the
first paragraph of the chunk text, and the summary is not stored. The
Migration Assistant adds the attributes and moves the breadcrumb out of the
text.
"""

from typing import Optional

HEADING_PATH_ATTR = "heading_path"
SUMMARY_ATTR = "summary"

SCHEMA_FILE = "common/gsql/supportai/SupportAI_Schema_Chunk_Context.gsql"
SCHEMA_JOB = "add_chunk_context"

_SEPARATOR = "\n\n"


def has_chunk_context_attrs(vertex_type: Optional[dict]) -> bool:
    """True if a schema vertex-type entry (from getSchema) carries both
    attributes."""
    names = {a.get("AttributeName") for a in (vertex_type or {}).get("Attributes") or []}
    return HEADING_PATH_ATTR in names and SUMMARY_ATTR in names


def schema_has_chunk_context(schema: dict) -> bool:
    """True if the DocumentChunk type in a getSchema result has both
    attributes."""
    chunk = next(
        (v for v in (schema or {}).get("VertexTypes", []) if v.get("Name") == "DocumentChunk"),
        None,
    )
    return has_chunk_context_attrs(chunk)


def schema_change_gsql(graphname: str) -> str:
    """GSQL that adds the two attributes to DocumentChunk. The job is dropped
    after it runs, so the same statement works again on another graph."""
    with open(SCHEMA_FILE, "r") as f:
        job = f.read()
    return (
        f"USE GRAPH {graphname}\n{job}\n"
        f"RUN SCHEMA_CHANGE JOB {SCHEMA_JOB}\nDROP JOB {SCHEMA_JOB}"
    )


def split_heading_path(text: str, heading_path: Optional[str]) -> tuple[str, str]:
    """``(heading_path, body)`` of a chunk text that starts with its
    breadcrumb, as the structured chunker writes it. If the text does not
    start with ``heading_path`` and a blank line, returns ``("", text)``."""
    text = text or ""
    if heading_path and text.startswith(heading_path + _SEPARATOR):
        return heading_path, text[len(heading_path) + len(_SEPARATOR):]
    return "", text


def chunk_parts(chunk) -> tuple[str, str]:
    """``(heading_path, body)`` of a chunker output. A structured chunk
    carries its breadcrumb in ``under_heading``; other chunks have none."""
    return split_heading_path(str(chunk), getattr(chunk, "under_heading", None))


def embedding_text(heading_path: str, summary: str, body: str) -> str:
    """Embedding input of a chunk: heading path, summary and body, each
    present part separated by a blank line."""
    return _SEPARATOR.join(p for p in (heading_path, summary, body) if p)


def format_chunk_summary(summary) -> str:
    """The stored and embedded form of a chunk summary (``topic``,
    ``section``, ``entities``): one tagged line per present part."""
    if not isinstance(summary, dict):
        return ""
    topic = str(summary.get("topic", "")).strip()
    section = str(summary.get("section", "")).strip()
    ents = summary.get("entities") or []
    ents_s = ", ".join(str(x).strip() for x in ents if str(x).strip()) if isinstance(ents, list) else ""
    parts = []
    if topic:
        parts.append(f"TOPIC: {topic}")
    if section:
        parts.append(f"SECTION: {section}")
    if ents_s:
        parts.append(f"ENTITIES: {ents_s}")
    return "\n".join(parts)
