"""One data_sources.json per graph.

Each connector replaces only its own key. Jira uses ``sources``. Zendesk uses
``zendesk_sources``. Source ids cannot collide across those keys.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from common.config import _config_file_lock


def load_document(path: Path) -> dict:
    if not path.exists():
        return {}
    with _config_file_lock:
        with path.open("r", encoding="utf-8") as stream:
            payload = json.load(stream)
    if not isinstance(payload, dict):
        raise ValueError("data_sources.json must be a JSON object")
    return payload


def replace_key(path: Path, key: str, value) -> None:
    """Write one connector key and leave every other key unchanged."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with _config_file_lock:
        document: dict = {}
        if path.exists():
            with path.open("r", encoding="utf-8") as stream:
                loaded = json.load(stream)
            if not isinstance(loaded, dict):
                raise ValueError("data_sources.json must be a JSON object")
            document = loaded
        document[key] = value
        try:
            with tmp.open("w", encoding="utf-8") as stream:
                json.dump(document, stream, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp, path)
        finally:
            if tmp.exists():
                tmp.unlink()


def ensure_unique_source_id(document: dict, source_id: str, owner_key: str) -> None:
    """Raise when source_id is already used by a different connector key."""
    for key, entries in document.items():
        if key == owner_key or not isinstance(entries, list):
            continue
        for entry in entries:
            if isinstance(entry, dict) and entry.get("id") == source_id:
                raise ValueError(
                    f"data-source id {source_id!r} is already used by {key}"
                )
