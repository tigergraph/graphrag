"""Shared data_sources.json updates that keep every connector's sources."""

from __future__ import annotations

import json
import os
from pathlib import Path

from common.config import _config_file_lock


def replace_sources(path: Path, key: str, sources: list) -> None:
    """Replace one source list and leave every other key in the file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with _config_file_lock:
        document: dict = {}
        if path.exists():
            with path.open("r", encoding="utf-8") as stream:
                loaded = json.load(stream)
            if isinstance(loaded, dict):
                document = loaded
        document[key] = sources
        try:
            with tmp.open("w", encoding="utf-8") as stream:
                json.dump(document, stream, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp, path)
        finally:
            if tmp.exists():
                tmp.unlink()


def source_ids(path: Path, key: str) -> set[str]:
    if not path.exists():
        return set()
    with _config_file_lock:
        with path.open("r", encoding="utf-8") as stream:
            loaded = json.load(stream)
    if not isinstance(loaded, dict):
        return set()
    ids: set[str] = set()
    for item in loaded.get(key) or []:
        if isinstance(item, dict) and item.get("id"):
            ids.add(str(item["id"]))
    return ids
