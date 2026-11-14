"""Document files kept for a graph, and their state against the graph.

Files are kept under ``configs/graph_docs/<source>/<graph>/``. The sources
``uploads`` and ``downloads`` hold the files an admin uploads or downloads
from cloud storage; any other folder under ``configs/graph_docs/`` (for
example ``server``) is a source an admin fills on the server. A source folder
can have subfolders. Converted JSONL files are kept under
``configs/graph_docs/<source>/ingestion_temp/<graph>/``, in the same
subfolders. The configs folder is mounted in every deployment, so the files
stay when the container is recreated.

A file's name is its path under the graph's folder, such as ``q1.pdf`` or
``reports/2023/q1.pdf``. Its document id is the file name with the
extension, in lowercase (``q1.pdf``), the same value the loading job stores.
Files with the same file name in different subfolders get the same id, so
they show as a conflict until one is renamed or deleted. Each converted record carries a SHA-256 of
its content (``content_checksum``), which the loading job stores on the
Document. Comparing the two tells whether the graph holds the file's current
content.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Iterable, Optional

logger = logging.getLogger(__name__)

DOCS_ROOT = os.path.join("configs", "graph_docs")
UPLOADS = "uploads"
DOWNLOADS = "downloads"
SOURCES = (UPLOADS, DOWNLOADS)
CONVERTED_DIR = "ingestion_temp"
_SOURCE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
# Name prefixes of files and folders that are never listed: hidden files,
# editor and Office lock files, and schema-extraction samples.
_SKIP_PREFIXES = (".", "~", "$", "_schema_")

CHECKSUM_ATTR = "content_checksum"
OUTDATED_ATTR = "outdated"
SUPERSEDED_BY = "SUPERSEDED_BY"

# File states, in the order the Stored Files tab lists them.
NOT_INGESTED = "not_ingested"
INGESTED = "ingested"
CHANGED = "changed"
NOT_VERIFIED = "not_verified"
OLDER_VERSION = "older_version"
CONFLICT = "conflict"
STATES = (NOT_INGESTED, CHANGED, NOT_VERIFIED, OLDER_VERSION, INGESTED, CONFLICT)
# States that load only when the user overrides the document in the graph.
OVERRIDE_STATES = (CHANGED, NOT_VERIFIED, OLDER_VERSION)


# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------

def _check_source(source: str) -> str:
    if not isinstance(source, str) or not _SOURCE_NAME.match(source) or source == CONVERTED_DIR:
        raise ValueError(f"unknown document source: {source!r}")
    return source


def all_sources() -> list[str]:
    """``uploads``, ``downloads`` and every other source folder under
    ``configs/graph_docs/``."""
    extra = []
    if os.path.isdir(DOCS_ROOT):
        for name in sorted(os.listdir(DOCS_ROOT)):
            if (name not in SOURCES and _SOURCE_NAME.match(name) and name != CONVERTED_DIR
                    and os.path.isdir(os.path.join(DOCS_ROOT, name))):
                extra.append(name)
    return list(SOURCES) + extra


def source_dir(graphname: str, source: str) -> str:
    """Folder of the kept files of a graph from one source."""
    return os.path.join(DOCS_ROOT, _check_source(source), graphname)


def converted_dir(graphname: str, source: str) -> str:
    """Folder of the converted JSONL files of a graph from one source."""
    return os.path.join(DOCS_ROOT, _check_source(source), CONVERTED_DIR, graphname)


def safe_file_name(name: str) -> str:
    """The base name of ``name``; refuses names that are empty or hidden."""
    base = os.path.basename(str(name or "").replace("\\", "/"))
    if not base or base in (".", "..") or base.startswith("."):
        raise ValueError(f"invalid file name: {name!r}")
    return base


def safe_rel_path(name: str) -> str:
    """``name`` as a relative path with ``/`` separators, such as
    ``reports/2023/q1.pdf``; refuses absolute paths, ``..`` and hidden parts."""
    text = str(name or "").replace("\\", "/")
    parts = [p for p in text.split("/") if p not in ("", ".")]
    if text.startswith("/") or not parts or any(p == ".." or p.startswith(".") for p in parts):
        raise ValueError(f"invalid file name: {name!r}")
    return "/".join(parts)


def kept_file_path(graphname: str, source: str, name: str) -> str:
    folder = source_dir(graphname, source)
    path = os.path.join(folder, *safe_rel_path(name).split("/"))
    if not is_within(path, folder):
        raise ValueError(f"invalid file name: {name!r}")
    return path


def is_within(path: str, folder: str) -> bool:
    """True if ``path`` is ``folder`` or inside it, after resolving links."""
    path, folder = os.path.realpath(path), os.path.realpath(folder)
    return path == folder or path.startswith(folder + os.sep)


def is_graph_docs_folder(graphname: str, path: str) -> bool:
    """True if ``path`` is one of the graph's own kept-file folders."""
    return any(
        os.path.realpath(path) == os.path.realpath(source_dir(graphname, s))
        for s in all_sources()
    )


# --------------------------------------------------------------------------
# Ids and checksums
# --------------------------------------------------------------------------

def _base(file_name: str) -> str:
    return os.path.basename(str(file_name).replace("\\", "/"))


def doc_id(file_name: str) -> str:
    """Document id of a kept file: its file name with the extension, in
    lowercase (the loading job lowercases the id it stores). The subfolder
    is not part of the id."""
    return _base(file_name).lower()


def legacy_doc_id(file_name: str) -> str:
    """Document id that releases before 2.1.0 gave the file: its name without
    the extension, in lowercase."""
    return os.path.splitext(_base(file_name))[0].lower()


def converted_name(file_name: str) -> str:
    """Path of the converted JSONL file of a kept file, relative to the
    converted folder. A ``.jsonl`` source is already in the loading format
    and keeps its name."""
    name = str(file_name).replace("\\", "/")
    return name if name.lower().endswith(".jsonl") else f"{name}.jsonl"


def converted_path(graphname: str, source: str, name: str) -> str:
    rel = converted_name(safe_rel_path(name))
    return os.path.join(converted_dir(graphname, source), *rel.split("/"))


def content_checksum(content) -> str:
    """SHA-256 of a document's converted content."""
    return hashlib.sha256(str(content or "").encode("utf-8")).hexdigest()


def add_checksums(records: Iterable[dict]) -> list[dict]:
    """Set ``content_checksum`` on each converted record. A record without
    content (an image entry) gets an empty checksum, so every line has the
    field the loading job reads."""
    out = []
    for rec in records:
        rec = dict(rec)
        content = rec.get("content")
        rec[CHECKSUM_ATTR] = content_checksum(content) if content else ""
        out.append(rec)
    return out


def converted_documents(path: str) -> Optional[dict]:
    """``{doc_id: checksum}`` of the documents in a converted JSONL file, or
    ``None`` when the file does not exist. Image entries are left out."""
    if not os.path.isfile(path):
        return None
    docs: dict = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            content = rec.get("content")
            did = str(rec.get("doc_id") or "").lower()
            if not did or not content:
                continue
            docs[did] = rec.get(CHECKSUM_ATTR) or content_checksum(content)
    return docs


# --------------------------------------------------------------------------
# Kept files
# --------------------------------------------------------------------------

@dataclass
class KeptFile:
    name: str
    source: str
    size: int
    modified: float
    # ``{doc_id: checksum}`` from the converted file; ``None`` until converted.
    documents: Optional[dict] = None
    state: str = NOT_INGESTED
    conflict_with: list = field(default_factory=list)
    legacy_id: Optional[str] = None

    @property
    def doc_ids(self) -> list[str]:
        if self.documents:
            return sorted(self.documents)
        return [doc_id(self.name)]

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "source": self.source,
            "size": self.size,
            "modified": self.modified,
            "doc_ids": self.doc_ids,
            "converted": self.documents is not None,
            "state": self.state,
            "conflict_with": self.conflict_with,
            "legacy_id": self.legacy_id,
        }


def _walk_files(folder: str) -> list[str]:
    """Relative paths of the files under ``folder``, subfolders included,
    without hidden, temporary and schema-sample entries."""
    out = []
    for root, dirs, names in os.walk(folder):
        dirs[:] = sorted(d for d in dirs if not d.startswith(_SKIP_PREFIXES))
        for name in sorted(names):
            if name.startswith(_SKIP_PREFIXES):
                continue
            rel = os.path.relpath(os.path.join(root, name), folder).replace(os.sep, "/")
            out.append(rel)
    return out


def list_kept_files(graphname: str, sources: Optional[Iterable[str]] = None) -> list[KeptFile]:
    """The kept files of a graph, with the documents of their converted files."""
    files = []
    for source in (all_sources() if sources is None else sources):
        folder = source_dir(graphname, source)
        if not os.path.isdir(folder):
            continue
        for name in _walk_files(folder):
            st = os.stat(os.path.join(folder, *name.split("/")))
            files.append(KeptFile(
                name=name, source=source, size=st.st_size, modified=st.st_mtime,
                documents=converted_documents(converted_path(graphname, source, name)),
            ))
    return files


def id_conflicts(files: Iterable[KeptFile]) -> dict:
    """``{(source, name): [names of the other files]}`` for kept files that
    share a document id with another kept file."""
    by_id: dict = {}
    for f in files:
        for did in f.doc_ids:
            by_id.setdefault(did, []).append(f)
    out: dict = {}
    for group in by_id.values():
        if len(group) < 2:
            continue
        for f in group:
            others = [g.name for g in group if g is not f]
            out.setdefault((f.source, f.name), [])
            out[(f.source, f.name)].extend(o for o in others if o not in out[(f.source, f.name)])
    return out


def assign_states(files: list[KeptFile], graph_docs: dict, has_checksum: bool) -> list[KeptFile]:
    """Set the state of each kept file.

    ``graph_docs`` is ``{doc_id: {"content_checksum": str, "outdated": bool}}``
    for the Documents of the graph, outdated ones included. ``has_checksum``
    says whether the graph's Document type has the checksum attribute.
    """
    conflicts = id_conflicts(files)
    for f in files:
        f.conflict_with = conflicts.get((f.source, f.name), [])
        f.legacy_id = None
        if f.conflict_with:
            f.state = CONFLICT
            continue
        states = []
        for did in f.doc_ids:
            doc = graph_docs.get(did)
            if doc is None:
                legacy = legacy_doc_id(f.name)
                old = graph_docs.get(legacy) if f.documents is None or did == doc_id(f.name) else None
                if old is not None and legacy != did and not old.get(OUTDATED_ATTR):
                    f.legacy_id = legacy
                    states.append(OLDER_VERSION)
                else:
                    states.append(NOT_INGESTED)
                continue
            stored = doc.get(CHECKSUM_ATTR) or ""
            current = (f.documents or {}).get(did)
            if not has_checksum or not stored or not current:
                states.append(NOT_VERIFIED)
            elif stored == current:
                states.append(INGESTED)
            else:
                states.append(CHANGED)
        f.state = _file_state(states)
    return files


def _file_state(states: list[str]) -> str:
    """One state for a file whose converted file holds several documents."""
    for s in (CHANGED, NOT_VERIFIED, OLDER_VERSION):
        if s in states:
            return s
    if states and all(s == INGESTED for s in states):
        return INGESTED
    return NOT_INGESTED


def state_counts(files: Iterable[KeptFile]) -> dict:
    counts = {s: 0 for s in STATES}
    for f in files:
        counts[f.state] = counts.get(f.state, 0) + 1
    return counts


def upload_id_conflicts(graphname: str, names: Iterable[str]) -> dict:
    """``{name: [names of kept files]}`` for planned uploads whose document id
    is the id of a different kept file. A kept upload with the same name is not a
    conflict: the upload replaces it."""
    kept = list_kept_files(graphname)
    out: dict = {}
    for name in names:
        try:
            base = safe_file_name(name)
        except ValueError:
            continue
        did = doc_id(base)
        others = [
            f.name for f in kept
            if did in f.doc_ids and not (f.source == UPLOADS and f.name == base)
        ]
        if others:
            out[base] = others
    return out


# --------------------------------------------------------------------------
# Rename
# --------------------------------------------------------------------------

class RenameError(ValueError):
    """A rename the user asked for that cannot be done; the message is shown
    to the user."""


def _renamed_records(records: list[dict], old_name: str, new_name: str) -> list[dict]:
    """Converted records of ``old_name`` with the document ids of
    ``new_name``. Image ids, their parent id and the image references in the
    content follow the new id; the checksum is made again for the new
    content."""
    old_id, new_id = doc_id(old_name), doc_id(new_name)
    old_img, new_img = f"{old_id}_image_", f"{new_id}_image_"
    out = []
    for rec in records:
        rec = dict(rec)
        did = str(rec.get("doc_id") or "")
        if did.lower() == old_id:
            rec["doc_id"] = new_name
        elif did.lower().startswith(old_img):
            rec["doc_id"] = new_img + did[len(old_img):]
        if str(rec.get("parent_doc") or "").lower() == old_id:
            rec["parent_doc"] = new_name
        if isinstance(rec.get("content"), str):
            rec["content"] = rec["content"].replace(f"tg://{old_img}", f"tg://{new_img}")
        out.append(rec)
    return add_checksums(out)


def rename_kept_file(graphname: str, source: str, name: str, new_name: str) -> str:
    """Rename a kept file inside its folder, and its converted file with it.

    ``name`` is the file's path under the graph's folder; ``new_name`` is a
    file name, and the file stays in its subfolder. The extension stays the
    same. The new document id must not be the id of another kept file.
    Returns the new path.
    """
    old = safe_rel_path(name)
    folder, old_base = os.path.split(old)
    raw_new = str(new_name or "").strip()
    try:
        if "/" in raw_new.replace("\\", "/"):
            raise ValueError(raw_new)
        new_base = safe_file_name(raw_new)
    except ValueError:
        raise RenameError("Enter a valid file name.")
    new = f"{folder}/{new_base}" if folder else new_base
    if os.path.splitext(new_base)[1].lower() != os.path.splitext(old_base)[1].lower():
        raise RenameError("The new name must keep the file extension.")
    if old_base.lower().endswith(".jsonl"):
        raise RenameError("A JSONL file keeps the document ids inside it. Renaming it does not change them.")
    if doc_id(new) == doc_id(old):
        raise RenameError("The new name must differ by more than letter case.")
    src = kept_file_path(graphname, source, old)
    if not os.path.isfile(src):
        raise FileNotFoundError(old)
    dst = kept_file_path(graphname, source, new)
    if os.path.exists(dst):
        raise RenameError(f"A file named {new_base} is already stored in this folder.")
    others = [
        f.name for f in list_kept_files(graphname)
        if doc_id(new) in f.doc_ids and not (f.source == source and f.name == old)
    ]
    if others:
        raise RenameError(f"{new_base} has the same name as {', '.join(sorted(set(others)))} when letter case is ignored.")

    old_conv, new_conv = converted_path(graphname, source, old), converted_path(graphname, source, new)
    records = None
    if os.path.isfile(old_conv):
        with open(old_conv, encoding="utf-8") as f:
            records = [json.loads(line) for line in f if line.strip()]
    os.rename(src, dst)
    if records is not None:
        with open(new_conv, "w", encoding="utf-8") as f:
            for rec in _renamed_records(records, old_base, new_base):
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        os.remove(old_conv)
    return new
