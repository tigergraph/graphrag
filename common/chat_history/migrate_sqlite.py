"""One-time, resumable SQLite-to-TigerGraph history migration."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import shutil
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from .repository import (
    HistoryConflictError,
    _default_connection,
    _payload,
)
from .settings import HistorySettings

logger = logging.getLogger(__name__)


def _epoch(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip()
    if not text:
        return 0
    try:
        return int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp())
    except (TypeError, ValueError):
        return 0


def _hash(value: Any) -> str:
    encoded = json.dumps(
        value, default=str, sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _load_rows(db: sqlite3.Connection, table: str) -> list[dict[str, Any]]:
    try:
        rows = db.execute(f"SELECT * FROM {table}").fetchall()
    except sqlite3.OperationalError as exc:
        raise RuntimeError(f"SQLite table {table!r} is missing") from exc
    return [dict(row) for row in rows]


def migrate_sqlite(
    db_path: str | Path,
    *,
    default_graph: str,
    dry_run: bool = False,
    backup: bool = True,
    connection: Any | None = None,
    settings: HistorySettings | None = None,
) -> dict[str, int | str | bool]:
    path = Path(db_path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    settings = settings or HistorySettings.from_env()

    backup_path = path.with_suffix(path.suffix + f".backup-{int(time.time())}")
    if backup and not dry_run:
        shutil.copy2(path, backup_path)

    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        conversations = [
            row
            for row in _load_rows(db, "conversations")
            if not row.get("deleted_at")
        ]
        messages = [
            row
            for row in _load_rows(db, "messages")
            if not row.get("deleted_at")
        ]

    owners = {
        str(row.get("conversation_id")): str(row.get("user_id") or "")
        for row in conversations
    }
    titles = {
        str(row.get("conversation_id")): str(row.get("name") or "")
        for row in conversations
    }
    messages.sort(
        key=lambda row: (
            str(row.get("conversation_id") or ""),
            _epoch(row.get("created_at")),
            int(row.get("id") or 0),
        )
    )

    if dry_run:
        return {
            "dry_run": True,
            "conversations": len(conversations),
            "messages": len(messages),
            "backup": "",
        }

    conn = connection or _default_connection(settings)
    imported = 0
    replayed = 0
    per_conversation_sequence: dict[str, int] = {}
    for row in messages:
        conversation_id = str(row.get("conversation_id") or "")
        owner = owners.get(conversation_id)
        if not owner:
            logger.warning(
                "Skipping message %s without a conversation owner",
                row.get("message_id"),
            )
            continue
        sequence = per_conversation_sequence.get(conversation_id, 0)
        per_conversation_sequence[conversation_id] = sequence + 1
        message_id = str(row.get("message_id") or "")
        created_at = _epoch(row.get("created_at")) or int(time.time())
        updated_at = _epoch(row.get("updated_at")) or created_at
        message_payload = {
            "conversation_id": conversation_id,
            "message_id": message_id,
            "role": row.get("role"),
            "content": row.get("content"),
            "model": row.get("model_name") or row.get("model"),
        }
        raw = conn.runInstalledQuery(
            "Chat_Import_Legacy_Message",
            params={
                "principal_id": owner,
                "graph_name": default_graph,
                "conversation_id": conversation_id,
                "conversation_title": titles.get(conversation_id, ""),
                "conversation_hash": _hash(
                    {
                        "principal_id": owner,
                        "graph_name": default_graph,
                        "conversation_id": conversation_id,
                    }
                ),
                "message_id": message_id,
                "parent_message_id": str(row.get("parent_id") or ""),
                "role_name": str(row.get("role") or ""),
                "model_name": str(
                    row.get("model_name") or row.get("model") or ""
                ),
                "content": str(row.get("content") or ""),
                "response_time": float(row.get("response_time") or 0.0),
                "feedback": int(row.get("feedback") or 0),
                "comment": str(row.get("comment") or ""),
                "sequence_no": sequence,
                "created_at": created_at,
                "updated_at": updated_at,
                "payload_hash": _hash(message_payload),
            },
        )
        status = str(_payload(raw).get("status") or "")
        if status == "conflict":
            raise HistoryConflictError(
                f"Conversation id collision for {conversation_id}"
            )
        if status == "created":
            imported += 1
        elif status == "replayed":
            replayed += 1
        else:
            raise RuntimeError(
                f"Unexpected migration status {status!r} for {message_id}"
            )

    return {
        "dry_run": False,
        "conversations": len(conversations),
        "messages": len(messages),
        "imported": imported,
        "replayed": replayed,
        "backup": str(backup_path) if backup else "",
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Migrate the legacy Go/GORM SQLite chat database"
    )
    parser.add_argument("--db-path", required=True)
    parser.add_argument("--default-graph", required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-backup", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=os.getenv("LOGLEVEL", "INFO"))
    print(
        migrate_sqlite(
            args.db_path,
            default_graph=args.default_graph,
            dry_run=args.dry_run,
            backup=not args.no_backup,
        )
    )


if __name__ == "__main__":
    main()
