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

"""Encrypted agent memory: the checkpoint serializer, the store wrapper and
the job that encrypts the existing rows of a graph's memory file.

Rows written before encryption stay readable; the job encrypts them.
"""

from __future__ import annotations

import base64
import json
import sqlite3
from typing import Any, Callable, Iterable, List

from langgraph.checkpoint.serde.base import CipherProtocol
from langgraph.checkpoint.serde.encrypted import EncryptedSerializer
from langgraph.store.base import BaseStore, Item, PutOp

from data_encryption.keys import DataKeys, value_key_id

CIPHER = "gre1"
STORE_MARK = "__graphrag_enc__"


class _GraphCipher(CipherProtocol):
    def __init__(self, manager, target: str):
        self.manager = manager
        self.target = target

    def encrypt(self, plaintext: bytes) -> tuple[str, bytes]:
        return CIPHER, self.manager.keys(self.target).encrypt(plaintext)

    def decrypt(self, ciphername: str, ciphertext: bytes) -> bytes:
        if ciphername != CIPHER:
            raise ValueError(f"unsupported cipher {ciphername!r}")
        return self.manager.keys(self.target).decrypt(ciphertext)


def checkpoint_serializer(manager, target: str, base) -> EncryptedSerializer:
    """Encrypts checkpoints and checkpoint writes with the graph's data key."""
    return EncryptedSerializer(_GraphCipher(manager, target), base)


def _seal_value(keys: DataKeys, value: dict) -> dict:
    blob = keys.encrypt(json.dumps(value).encode("utf-8"))
    return {STORE_MARK: base64.b64encode(blob).decode()}


def _open_value(keys: DataKeys, value: Any) -> Any:
    if isinstance(value, dict) and STORE_MARK in value:
        blob = base64.b64decode(value[STORE_MARK])
        return json.loads(keys.decrypt(blob).decode("utf-8"))
    return value


class EncryptedStore(BaseStore):
    """A store whose item values are encrypted with the graph's data key.

    Namespaces and keys (memory paths) stay in clear text, so lookups by
    graph, user and path keep working.
    """

    def __init__(self, inner: BaseStore, manager, target: str):
        self.inner = inner
        self.manager = manager
        self.target = target

    def _ops(self, ops: Iterable) -> List:
        out = []
        for op in ops:
            if isinstance(op, PutOp) and op.value is not None:
                keys = self.manager.keys(self.target)
                op = op._replace(value=_seal_value(keys, op.value), index=False)
            out.append(op)
        return out

    def _results(self, results: List) -> List:
        keys = None
        for r in results:
            items = r if isinstance(r, list) else [r]
            for item in items:
                if isinstance(item, Item):
                    if isinstance(item.value, dict) and STORE_MARK in item.value:
                        keys = keys or self.manager.keys(self.target)
                        item.value = _open_value(keys, item.value)
        return results

    def batch(self, ops):
        return self._results(self.inner.batch(self._ops(ops)))

    async def abatch(self, ops):
        return self._results(await self.inner.abatch(self._ops(ops)))


# --------------------------------------------------------------------------
# Encrypting existing rows
# --------------------------------------------------------------------------

def _tables(conn) -> set:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def sweep_memory_file(path: str, keys: DataKeys, progress: Callable[[int], None],
                      batch: int = 200) -> int:
    """Encrypt every row of a memory file with ``keys.active``.

    Clear-text rows are encrypted, and rows of another data key are
    encrypted again. A row is updated only if it did not change since it was
    read, so writes of a running conversation are never lost.
    """
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    done = 0
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        tables = _tables(conn)
        for table, col in (("checkpoints", "checkpoint"), ("writes", "value")):
            if table not in tables:
                continue
            last = 0
            while True:
                rows = conn.execute(
                    f"SELECT rowid, type, {col} FROM {table} WHERE rowid > ? "
                    f"ORDER BY rowid LIMIT ?", (last, batch)).fetchall()
                if not rows:
                    break
                last = rows[-1][0]
                updates = []
                for rowid, typ, val in rows:
                    if val is None or typ is None:
                        continue
                    if typ.endswith("+" + CIPHER):
                        if value_key_id(val) == keys.active:
                            continue
                        plain, new_typ = keys.decrypt(val), typ
                    else:
                        plain, new_typ = bytes(val), f"{typ}+{CIPHER}"
                    updates.append((new_typ, keys.encrypt(plain), rowid, typ, val))
                if updates:
                    conn.execute("BEGIN IMMEDIATE")
                    conn.executemany(
                        f"UPDATE {table} SET type = ?, {col} = ? "
                        f"WHERE rowid = ? AND type = ? AND {col} = ?", updates)
                    conn.execute("COMMIT")
                    done += len(updates)
                    progress(done)
        if "store" in tables:
            last = 0
            while True:
                rows = conn.execute(
                    "SELECT rowid, value FROM store WHERE rowid > ? ORDER BY rowid LIMIT ?",
                    (last, batch)).fetchall()
                if not rows:
                    break
                last = rows[-1][0]
                updates = []
                for rowid, val in rows:
                    value = json.loads(val) if val else None
                    if value is None:
                        continue
                    if isinstance(value, dict) and STORE_MARK in value:
                        blob = base64.b64decode(value[STORE_MARK])
                        if value_key_id(blob) == keys.active:
                            continue
                        value = json.loads(keys.decrypt(blob).decode("utf-8"))
                    updates.append((json.dumps(_seal_value(keys, value)), rowid, val))
                if updates:
                    conn.execute("BEGIN IMMEDIATE")
                    conn.executemany("UPDATE store SET value = ? WHERE rowid = ? AND value = ?",
                                     updates)
                    conn.execute("COMMIT")
                    done += len(updates)
                    progress(done)
    finally:
        conn.close()
    return done
