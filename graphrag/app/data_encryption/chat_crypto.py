# Copyright (c) 2024-2026 TigerGraph, Inc.
#
# This program may be redistributed and/or modified under the terms of the GNU
# Affero General Public License as published by the Free Software Foundation,
# either version 3 of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE. See the GNU Affero General Public License for more
# details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.

"""Encrypted chat history.

GraphRAG encrypts the message content and the feedback comment before it
sends a message to the chat-history service, and decrypts them when it reads
them back. The chat-history service stores only cipher text and has no key.
"""

from __future__ import annotations

import base64
import logging
from typing import Any, Callable, Optional

import httpx

from data_encryption.keys import (DataKeys, decrypt_text, encrypt_text, is_encrypted_text,
                                  text_blob, value_key_id)
from data_encryption.key_manager import CHAT_TARGET

logger = logging.getLogger(__name__)

FIELDS = ("content", "comment")


def encrypt_message(manager, message: dict) -> dict:
    """A copy of ``message`` with its text fields encrypted. Raises Locked."""
    keys = manager.keys(CHAT_TARGET)
    out = dict(message)
    for f in FIELDS:
        v = out.get(f)
        if isinstance(v, str) and v and not is_encrypted_text(v):
            out[f] = encrypt_text(keys, v)
    return out


def decrypt_messages(manager, data: Any) -> Any:
    """``data`` (a message, a list of them, or a response that holds them)
    with the text fields decrypted in place. Raises Locked."""
    keys: Optional[DataKeys] = None

    def walk(obj):
        nonlocal keys
        if isinstance(obj, list):
            for x in obj:
                walk(x)
        elif isinstance(obj, dict):
            for k, v in obj.items():
                if k in FIELDS and is_encrypted_text(v):
                    keys = keys or manager.keys(CHAT_TARGET)
                    obj[k] = decrypt_text(keys, v)
                elif isinstance(v, (list, dict)):
                    walk(v)
    walk(data)
    return data


# --------------------------------------------------------------------------
# Admin calls to the chat-history service
# --------------------------------------------------------------------------

def _service():
    from common.config import db_config, graphrag_config
    base = graphrag_config.get("chat_history_api")
    if not base:
        raise RuntimeError("chat_history_api is not configured")
    user, pwd = db_config.get("username", ""), db_config.get("password", "")
    auth = base64.b64encode(f"{user}:{pwd}".encode()).decode()
    return base.rstrip("/"), {"Authorization": f"Basic {auth}"}


def _pages(client, base, headers, page: int = 500):
    after = 0
    while True:
        res = client.get(f"{base}/messages", params={"after": after, "limit": page},
                         headers=headers)
        res.raise_for_status()
        rows = res.json() or []
        if not rows:
            return
        yield rows
        after = rows[-1]["id"]
        if len(rows) < page:
            return


def sweep_chat(target: str, keys: DataKeys, progress: Callable[[int], None]) -> int:
    """Encrypt every stored message with ``keys.active``."""
    base, headers = _service()
    done = 0
    with httpx.Client(timeout=60) as client:
        for rows in _pages(client, base, headers):
            updates = []
            for m in rows:
                change = {}
                for f in FIELDS:
                    v = m.get(f) or ""
                    if not v:
                        continue
                    if is_encrypted_text(v):
                        if value_key_id(text_blob(v)) == keys.active:
                            continue
                        v = decrypt_text(keys, v)
                    change[f] = encrypt_text(keys, v)
                if change:
                    updates.append({"message_id": m["message_id"], **{
                        f: change.get(f, m.get(f) or "") for f in FIELDS}})
            if updates:
                res = client.post(f"{base}/messages", json=updates, headers=headers)
                res.raise_for_status()
                done += len(updates)
                progress(done)
    return done


def reset_chat(target: str) -> int:
    """Delete the messages that are encrypted (their key is lost)."""
    base, headers = _service()
    deleted = 0
    with httpx.Client(timeout=60) as client:
        ids = [m["message_id"] for rows in _pages(client, base, headers) for m in rows
               if any(is_encrypted_text(m.get(f)) for f in FIELDS)]
        for i in range(0, len(ids), 500):
            res = client.post(f"{base}/messages/delete", json={"message_ids": ids[i:i + 500]},
                              headers=headers)
            res.raise_for_status()
            deleted += len(ids[i:i + 500])
    logger.warning(f"encryption: deleted {deleted} chat messages whose key is lost")
    return deleted
