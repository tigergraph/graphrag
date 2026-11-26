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

"""Keys of the stored agent memory and chat history.

Envelope encryption: a master key (global, or one for a graph) wraps one data
key for each target (a graph's agent memory, or the chat history). All keys
are configuration and live in ``configs/graph_configs/``; the data lives in
``configs/graph_data/`` and the chat-history service.

The global master key is managed by GraphRAG (generated on first start, kept
in ``graph_configs/master.key``) or mounted from a secret
(``GRAPHRAG_ENCRYPTION_KEY_FILE``), which GraphRAG only reads.

A target whose keys cannot be loaded is locked: nothing is read or written
for it, and no new key is generated over existing encrypted data. Every key
action goes to the audit log; nothing else about the keys is recorded.
"""

from __future__ import annotations

import logging
import os
import queue
import sqlite3
import threading
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

from data_encryption.keys import (DataKeyFile, DataKeys, DecryptError, KeyFileError, KeyRing,
                                  new_key, now, unwrap, wrap)

logger = logging.getLogger(__name__)

CHAT_TARGET = "@chat_history"          # never a graph name: "@" is not valid in one
ENV_KEY_FILE = "GRAPHRAG_ENCRYPTION_KEY_FILE"
MASTER_FILE = "master.key"
DATA_KEY_FILE = "data_key.json"
CHAT_DATA_KEY_FILE = "chat_history.data_key.json"
MEMORY_FILE = "agent_memory.sqlite"
SYSTEM = "system"


class Locked(Exception):
    """The keys of a target cannot be loaded."""

    def __init__(self, target: str, reason: str, needed: Optional[List[str]] = None,
                 scope: str = "global"):
        self.target = target
        self.reason = reason            # master_key_missing | wrong_key | data_key_missing
        self.needed = needed or []
        self.scope = scope
        super().__init__(f"{target}: {reason}")

    def to_json(self) -> dict:
        return {"reason": self.reason, "needed_key_ids": self.needed, "scope": self.scope}


class Rejected(Exception):
    """An admin action is not possible; the message is for the admin."""


class NeedsPreviousKey(Rejected):
    """The mounted key is new and the data keys cannot be opened without the
    key that wraps them now."""

    def __init__(self, needed: List[str]):
        self.needed = needed
        super().__init__("Upload the previous global key to finish the rotation.")


@dataclass
class Job:
    state: str = "idle"                 # idle | queued | running | done | failed
    done: int = 0
    error: str = ""

    def to_json(self) -> dict:
        return {"state": self.state, "done": self.done, "error": self.error}


@dataclass
class _Paths:
    root: str

    @property
    def graph_configs(self) -> str:
        return os.path.join(self.root, "graph_configs")

    @property
    def graph_data(self) -> str:
        return os.path.join(self.root, "graph_data")

    def global_ring(self) -> str:
        return os.environ.get(ENV_KEY_FILE) or os.path.join(self.graph_configs, MASTER_FILE)

    def graph_ring(self, graph: str) -> str:
        return os.path.join(self.graph_configs, graph, MASTER_FILE)

    def data_keys(self, target: str) -> str:
        if target == CHAT_TARGET:
            return os.path.join(self.graph_configs, CHAT_DATA_KEY_FILE)
        return os.path.join(self.graph_configs, target, DATA_KEY_FILE)

    def memory(self, graph: str) -> str:
        return os.path.join(self.graph_data, graph, MEMORY_FILE)


def _real(path: str) -> str:
    # The image links /code/configs to the mounted volume; open the real path.
    return os.path.realpath(path)


def _name(scope: str) -> str:
    return "graph key" if scope == "graph" else "global key"


def _audit(action: str, actor: str, graph: Optional[str], result: str, **fields) -> None:
    entry = {"event": "encryption_key", "action": action, "user": actor or SYSTEM,
             "graph": graph or "", "result": result, "at": now(), **fields}
    try:
        from common.logs.logwriter import LogWriter
        LogWriter.audit_log(entry, mask_pii=False)
    except Exception as exc:                # never block a key action on logging
        logger.warning(f"encryption: audit log failed: {exc}")
    logger.info(f"encryption audit: {entry}")


class EncryptionManager:
    def __init__(self, root: str = "configs"):
        self.paths = _Paths(root)
        self._lock = threading.RLock()
        self._keys: Dict[str, DataKeys] = {}
        self._jobs: Dict[str, Job] = {}
        self._queue: "queue.Queue[str]" = queue.Queue()
        self._worker: Optional[threading.Thread] = None
        # target kind -> function(target, keys, progress) that re-encrypts the
        # target's stored values with keys.active. Registered by the owners of
        # the data (agent memory, chat history).
        self.sweepers: Dict[str, Callable] = {}
        # Called before a graph's memory is reset (closes open connections).
        self.on_reset: List[Callable[[str], None]] = []

    # ------------------------------------------------------------------ rings

    @property
    def mounted(self) -> bool:
        return bool(os.environ.get(ENV_KEY_FILE))

    def _ring_path(self, scope: str, graph: str) -> str:
        return self.paths.global_ring() if scope == "global" else self.paths.graph_ring(graph)

    def _load_ring(self, scope: str, graph: str = "") -> Optional[KeyRing]:
        try:
            return KeyRing.load(_real(self._ring_path(scope, graph)))
        except (KeyFileError, OSError, ValueError) as exc:
            logger.error(f"encryption: cannot read the {scope} key: {exc}")
            return None

    def _save_ring(self, scope: str, graph: str, ring: KeyRing) -> None:
        if scope == "global" and self.mounted:
            raise Rejected("The global key is a mounted secret.")
        ring.save(_real(self._ring_path(scope, graph)))

    # ------------------------------------------------------------ data keys

    def _graph_targets(self) -> List[str]:
        out = set()
        for base, marker in ((self.paths.graph_configs, DATA_KEY_FILE),
                             (self.paths.graph_data, MEMORY_FILE)):
            root = _real(base)
            for name in os.listdir(root) if os.path.isdir(root) else []:
                if os.path.exists(os.path.join(root, name, marker)):
                    out.add(name)
        return sorted(out)

    def _targets(self) -> List[str]:
        return self._graph_targets() + [CHAT_TARGET]

    def _load_dk(self, target: str) -> Optional[DataKeyFile]:
        return DataKeyFile.load(_real(self.paths.data_keys(target)))

    def _save_dk(self, target: str, dk: DataKeyFile) -> None:
        dk.save(_real(self.paths.data_keys(target)))

    def _files_of_scope(self, scope: str, graph: Optional[str] = None) -> Dict[str, DataKeyFile]:
        targets = [graph] if scope == "graph" else self._targets()
        out = {}
        for t in targets:
            dk = self._load_dk(t)
            if dk is not None and dk.master_scope == scope:
                out[t] = dk
        return out

    def _memory_query(self, graph: str, sql: str) -> bool:
        path = _real(self.paths.memory(graph))
        if not os.path.exists(path):
            return False
        try:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            try:
                return conn.execute(sql).fetchone() is not None
            finally:
                conn.close()
        except sqlite3.Error:
            return False

    def _has_encrypted_memory(self, graph: str) -> bool:
        return self._memory_query(
            graph, "SELECT 1 FROM checkpoints WHERE type LIKE '%+gre1' LIMIT 1")

    def _has_plain_memory(self, graph: str) -> bool:
        return any(self._memory_query(graph, sql) for sql in (
            "SELECT 1 FROM checkpoints WHERE type NOT LIKE '%+gre1' LIMIT 1",
            "SELECT 1 FROM writes WHERE value IS NOT NULL AND type NOT LIKE '%+gre1' LIMIT 1",
            "SELECT 1 FROM store WHERE instr(value, '__graphrag_enc__') = 0 LIMIT 1",
        ))

    # ------------------------------------------------------------------ keys

    def _may_generate_global(self) -> bool:
        if self.mounted or self._files_of_scope("global"):
            return False
        return not any(self._has_encrypted_memory(g) for g in self._graph_targets())

    def ensure_global_key(self) -> Optional[KeyRing]:
        """The global key; generated only when no data needs an existing one."""
        with self._lock:
            ring = self._load_ring("global")
            if ring is None and self._may_generate_global():
                ring = KeyRing.generate()
                self._save_ring("global", "", ring)
                _audit("generate", SYSTEM, None, "ok", key_id=ring.active)
            return ring

    def keys(self, target: str) -> DataKeys:
        """The data keys of a target. Raises Locked."""
        keys = self._keys.get(target)
        if keys is not None:
            return keys
        with self._lock:
            keys = self._keys.get(target)
            if keys is not None:
                return keys
            keys, needs_sweep = self._open_keys(target)
            self._keys[target] = keys
        if needs_sweep:
            self.schedule(target)
        return keys

    def _open_keys(self, target: str):
        """(keys, whether stored values must be encrypted again)."""
        dk = self._load_dk(target)
        if dk is None:
            if target != CHAT_TARGET and self._has_encrypted_memory(target):
                raise Locked(target, "data_key_missing")
            ring = self.ensure_global_key()
            if ring is None:
                raise Locked(target, "master_key_missing")
            dek = new_key()
            entry = wrap(ring.active_key, ring.active, dek, target)
            self._save_dk(target, DataKeyFile("global", entry.id, [entry]))
            _audit("create_data_key", SYSTEM, None if target == CHAT_TARGET else target, "ok",
                   key_id=entry.id)
            has_data = target == CHAT_TARGET or os.path.exists(_real(self.paths.memory(target)))
            return DataKeys({entry.id: dek}, entry.id), has_data
        ring = self._load_ring(dk.master_scope, target)
        if ring is None:
            raise Locked(target, "master_key_missing", dk.master_ids(), dk.master_scope)
        plain = {}
        for entry in dk.keys:
            master = ring.versions.get(entry.master_id)
            try:
                if master is None:
                    raise DecryptError("no master key")
                plain[entry.id] = unwrap(master, entry, target)
            except DecryptError:
                raise Locked(target, "wrong_key", dk.master_ids(), dk.master_scope)
        if dk.active not in plain:
            raise Locked(target, "data_key_missing", dk.master_ids(), dk.master_scope)
        # More than one data key: a data key rotation is not finished. Chat
        # history is checked once in each process; memory files are checked
        # here (a quick query).
        needs = len(dk.keys) > 1 or target == CHAT_TARGET or self._has_plain_memory(target)
        return DataKeys(plain, dk.active), needs

    def locked(self, target: str) -> Optional[Locked]:
        try:
            self.keys(target)
            return None
        except Locked as exc:
            return exc

    def _forget(self, *targets: str) -> None:
        with self._lock:
            for t in targets:
                self._keys.pop(t, None)

    def _plain_data_keys(self, files: Dict[str, DataKeyFile], *rings: Optional[KeyRing]):
        """The data keys of ``files`` in clear, from ``rings`` or from the
        keys open in this process. Returns (keys, master ids not found)."""
        deks, missing = {}, set()
        for t, dk in files.items():
            cached = self._keys.get(t)
            for e in dk.keys:
                master = next((r.versions[e.master_id] for r in rings
                               if r is not None and e.master_id in r.versions), None)
                try:
                    if master is not None:
                        deks[(t, e.id)] = unwrap(master, e, t)
                        continue
                except DecryptError:
                    pass
                if cached is not None and e.id in cached.keys:
                    deks[(t, e.id)] = cached.keys[e.id]
                else:
                    missing.add(e.master_id)
        return deks, sorted(missing)

    def _rewrap(self, files: Dict[str, DataKeyFile], deks: dict, ring: KeyRing, scope: str) -> None:
        for t, dk in files.items():
            dk.keys = [e if e.master_id == ring.active else
                       wrap(ring.active_key, ring.active, deks[(t, e.id)], t) for e in dk.keys]
            dk.master_scope = scope
            self._save_dk(t, dk)
        self._forget(*files.keys())

    def _prune_ring(self, scope: str, graph: str, ring: KeyRing) -> None:
        used = set()
        for dk in self._files_of_scope(scope, graph or None).values():
            used.update(dk.master_ids())
        stale = [k for k in ring.versions if k != ring.active and k not in used]
        if stale:
            for k in stale:
                ring.remove(k)
            self._save_ring(scope, graph, ring)

    def _resume_rotation(self, scope: str, graph: str = "") -> None:
        """Finish a rotation of a managed key that stopped part way: the key
        file has the new version, some data keys still use the old one."""
        with self._lock:
            ring = self._load_ring(scope, graph)
            if ring is None:
                return
            files = self._files_of_scope(scope, graph or None)
            deks, missing = self._plain_data_keys(files, ring)
            if missing:
                return                      # locked; the admin must upload the key
            if any(e.master_id != ring.active for dk in files.values() for e in dk.keys):
                self._rewrap(files, deks, ring, scope)
                _audit("rotate_master_resumed", SYSTEM, graph or None, "ok", key_id=ring.active)
            self._prune_ring(scope, graph, ring)

    # --------------------------------------------------------- admin actions

    def _run(self, action: str, actor: str, graph: Optional[str], fn, **fields):
        try:
            result = fn()
        except NeedsPreviousKey as exc:
            _audit(action, actor, graph, "needs_previous_key", needed_key_ids=exc.needed, **fields)
            raise
        except (Rejected, Locked) as exc:
            _audit(action, actor, graph, "rejected", detail=str(exc), **fields)
            raise
        return result

    def rotate_master(self, graph: Optional[str] = None, key: Optional[str] = None,
                      previous_key: Optional[str] = None, actor: str = SYSTEM) -> dict:
        """Rotate a master key.

        A managed key gets a new version (``key``, or a generated one) and
        every data key is wrapped with it. A mounted key is changed in the
        secret: GraphRAG wraps the data keys with the key that is mounted
        now, and needs ``previous_key`` only if it cannot open them itself.
        """
        def do():
            with self._lock:
                if graph:
                    dk = self._load_dk(graph)
                    if dk is None or dk.master_scope != "graph":
                        raise Rejected("This graph uses the global key.")
                if not graph and self.mounted:
                    return self._rotate_mounted(previous_key, actor)
                scope = "graph" if graph else "global"
                current = self._load_ring(scope, graph or "") if graph else self.ensure_global_key()
                if current is None:
                    raise Rejected(f"The {_name(scope)} is missing. Upload it first.")
                try:
                    new = KeyRing.parse(key).active_key if key else new_key()
                except KeyFileError as exc:
                    raise Rejected(f"The {_name(scope)} is not valid: {exc}")
                rotated = KeyRing(dict(current.versions), dict(current.created), current.active)
                if rotated.add(new) in current.versions:
                    raise Rejected(f"This {_name(scope)} is already in use.")
                files = self._files_of_scope(scope, graph)
                deks, missing = self._plain_data_keys(files, current)
                if missing:
                    raise Rejected(f"Some memory is locked. Upload its {_name(scope)} first.")
                # The key file keeps the old version until every data key is
                # wrapped with the new one, so a stop at any step is safe.
                self._save_ring(scope, graph or "", rotated)
                self._rewrap(files, deks, rotated, scope)
                self._prune_ring(scope, graph or "", rotated)
                _audit("rotate_master", actor, graph, "ok", key_id=rotated.active,
                       old_key_id=current.active, own_key=bool(key))
                return {"result": "rotated", **self.status(graph)}
        return self._run("rotate_master", actor, graph, do)

    def _rotate_mounted(self, previous_key: Optional[str], actor: str) -> dict:
        mounted = self._load_ring("global")
        if mounted is None:
            raise Rejected("The mounted global key cannot be read.")
        files = self._files_of_scope("global")
        if all(e.master_id == mounted.active for dk in files.values() for e in dk.keys):
            return {"result": "unchanged", **self.status()}
        previous = None
        if previous_key:
            try:
                previous = KeyRing.parse(previous_key)
            except KeyFileError as exc:
                raise Rejected(f"The global key is not valid: {exc}")
        deks, missing = self._plain_data_keys(files, mounted, previous)
        if missing:
            if previous is not None:
                raise Rejected("This global key does not match the data keys of the stored memory.")
            raise NeedsPreviousKey(missing)
        old = sorted({e.master_id for dk in files.values() for e in dk.keys} - {mounted.active})
        self._rewrap(files, deks, mounted, "global")
        _audit("rotate_master", actor, None, "ok", key_id=mounted.active, old_key_id=",".join(old),
               mounted=True, previous_key_uploaded=previous is not None)
        return {"result": "rotated", **self.status()}

    def upload_key(self, content: str, graph: Optional[str] = None, actor: str = SYSTEM) -> dict:
        """Restore the key of locked data. With a mounted key, this is the
        previous key, used only to finish a rotation and never stored."""
        def do():
            with self._lock:
                scope = "graph" if graph else "global"
                if graph:
                    dk = self._load_dk(graph)
                    if dk is None or dk.master_scope != "graph":
                        scope, files = "global", self._files_of_scope("global")
                    else:
                        files = {graph: dk}
                else:
                    files = self._files_of_scope("global")
                if scope == "global" and self.mounted:
                    return self._rotate_mounted(content, actor)
                if not any(self.locked(t) for t in files):
                    raise Rejected(f"Nothing is locked. To change the {_name(scope)}, rotate it.")
                try:
                    ring = KeyRing.parse(content)
                except KeyFileError as exc:
                    raise Rejected(f"The {_name(scope)} is not valid: {exc}")
                ring_graph = graph if scope == "graph" else ""
                current = self._load_ring(scope, ring_graph)
                deks, missing = self._plain_data_keys(files, ring, current)
                if missing:
                    raise Rejected(f"This {_name(scope)} does not match the data keys of the stored memory.")
                merged = KeyRing(dict(ring.versions), dict(ring.created), ring.active)
                if current is not None:
                    for k, v in current.versions.items():
                        merged.versions.setdefault(k, v)
                        merged.created.setdefault(k, current.created.get(k, ""))
                self._save_ring(scope, ring_graph, merged)
                self._rewrap(files, deks, merged, scope)
                self._prune_ring(scope, ring_graph, merged)
                _audit("upload", actor, graph, "ok", key_id=merged.active)
                return self.status(graph)
        result = self._run("upload", actor, graph, do)
        self._resume_sweeps()
        return result

    def use_graph_key(self, graph: str, key: Optional[str] = None, actor: str = SYSTEM) -> dict:
        """Give a graph its own key (``key``, or a generated one)."""
        def do():
            with self._lock:
                try:
                    ring = KeyRing.parse(key) if key else KeyRing.generate()
                except KeyFileError as exc:
                    raise Rejected(f"The graph key is not valid: {exc}")
                dk = self._load_dk(graph)
                if dk is not None and dk.master_scope == "graph":
                    raise Rejected("This graph already uses a graph key. Use Rotate graph key.")
                if dk is None:
                    if self._has_encrypted_memory(graph):
                        raise Rejected("The memory of this graph is locked.")
                    self._save_ring("graph", graph, ring)
                    e = wrap(ring.active_key, ring.active, new_key(), graph)
                    self._save_dk(graph, DataKeyFile("graph", e.id, [e]))
                else:
                    files = {graph: dk}
                    deks, missing = self._plain_data_keys(files, self._load_ring("global"))
                    if missing:
                        raise Rejected("The memory of this graph is locked.")
                    # Save the new key first; the data key stays wrapped with
                    # the global key until it is saved with the new one.
                    self._save_ring("graph", graph, ring)
                    self._rewrap(files, deks, ring, "graph")
                _audit("use_graph_key", actor, graph, "ok", key_id=ring.active, own_key=bool(key))
                return self.status(graph)
        return self._run("use_graph_key", actor, graph, do)

    def use_global_key(self, graph: str, actor: str = SYSTEM) -> dict:
        def do():
            with self._lock:
                dk = self._load_dk(graph)
                if dk is None or dk.master_scope == "global":
                    return self.status(graph)
                ring = self.ensure_global_key()
                if ring is None:
                    raise Rejected("The global key is not available.")
                files = {graph: dk}
                deks, missing = self._plain_data_keys(files, self._load_ring("graph", graph))
                if missing:
                    raise Rejected("The graph key does not match the data key of this graph.")
                self._rewrap(files, deks, ring, "global")
                try:
                    os.unlink(_real(self.paths.graph_ring(graph)))
                except FileNotFoundError:
                    pass
                _audit("use_global_key", actor, graph, "ok", key_id=ring.active)
                return self.status(graph)
        return self._run("use_global_key", actor, graph, do)

    def rotate_data_key(self, target: str, actor: str = SYSTEM) -> dict:
        graph = None if target == CHAT_TARGET else target

        def do():
            with self._lock:
                keys = self.keys(target)            # raises Locked
                dk = self._load_dk(target)
                ring = self._load_ring(dk.master_scope, target)
                dek = new_key()
                entry = wrap(ring.active_key, ring.active, dek, target)
                dk.keys.append(entry)
                dk.active = entry.id
                self._save_dk(target, dk)
                self._keys[target] = DataKeys({**keys.keys, entry.id: dek}, entry.id)
                _audit("rotate_data_key", actor, graph, "ok", target=target, key_id=entry.id,
                       old_key_id=keys.active)
        self._run("rotate_data_key", actor, graph, do)
        self.schedule(target)
        return self.status(graph)

    def reset(self, target: str, actor: str = SYSTEM) -> dict:
        """Delete the locked data of a target and start with a new data key."""
        graph = None if target == CHAT_TARGET else target

        def do():
            with self._lock:
                if target != CHAT_TARGET:
                    raise Rejected("Use Delete memory for a graph.")
                if self.locked(target) is None:
                    raise Rejected("Only locked data can be reset.")
                for hook in self.on_reset:
                    hook(target)
                if target == CHAT_TARGET:
                    sweeper = self.sweepers.get("reset_chat")
                    if sweeper is None:
                        raise Rejected("Chat history cannot be reset.")
                    sweeper(target)
                else:
                    base = _real(self.paths.memory(target))
                    for suffix in ("", "-wal", "-shm"):
                        try:
                            os.unlink(base + suffix)
                        except FileNotFoundError:
                            pass
                try:
                    os.unlink(_real(self.paths.data_keys(target)))
                except FileNotFoundError:
                    pass
                self._forget(target)
                self._jobs.pop(target, None)
                _audit("reset", actor, graph, "ok", target=target)
        self._run("reset", actor, graph, do)
        return self.status(graph)

    def delete_memory(self, graph: str, live_graphs, actor: str = SYSTEM) -> None:
        """Delete the stored conversations and memories of a graph, for all
        users. The keys are kept for a graph that exists in TigerGraph; a
        removed graph loses its keys too, and a locked graph gets a new data
        key, so each starts fresh."""
        def do():
            with self._lock:
                removed = graph not in set(live_graphs)
                locked = self.locked(graph) is not None
                for hook in self.on_reset:
                    hook(graph)
                base = _real(self.paths.memory(graph))
                paths = [base + suffix for suffix in ("", "-wal", "-shm")]
                if removed or locked:
                    paths += [_real(self.paths.data_keys(graph)), _real(self.paths.graph_ring(graph))]
                for path in paths:
                    try:
                        os.unlink(path)
                    except FileNotFoundError:
                        pass
                self._forget(graph)
                self._jobs.pop(graph, None)
                _audit("delete_memory", actor, graph, "ok", graph_removed=removed,
                       keys_deleted=removed or locked)
        self._run("delete_memory", actor, graph, do)

    def download(self, graph: Optional[str] = None, actor: str = SYSTEM) -> dict:
        def do():
            if graph is None and self.mounted:
                raise Rejected("The global key is a mounted secret.")
            ring = self._load_ring("graph" if graph else "global", graph or "")
            if ring is None:
                raise Rejected("The key is missing.")
            _audit("download", actor, graph, "ok", key_id=ring.active)
            return ring.to_json()
        return self._run("download", actor, graph, do)

    # ------------------------------------------------------------- reporting

    def _ring_info(self, scope: str, graph: Optional[str]) -> dict:
        ring = self._load_ring(scope, graph or "")
        mounted = scope == "global" and self.mounted
        info = {"present": ring is not None, "source": "mounted" if mounted else "managed"}
        if ring is None:
            return info
        in_use = sorted({k for dk in self._files_of_scope(scope, graph).values()
                         for k in dk.master_ids()})
        info.update({
            "active_key_id": ring.active,
            "created_at": ring.created.get(ring.active, ""),
            "key_ids": list(ring.versions),
            "in_use_key_ids": in_use,
            # A mounted key that the data keys do not use yet: Rotate key
            # wraps them with it.
            "rotation_pending": mounted and any(k != ring.active for k in in_use),
        })
        return info

    def _target_info(self, target: str, live: Optional[set] = None) -> dict:
        dk = self._load_dk(target)
        lock = self.locked(target)
        job = self._jobs.get(target, Job())
        info = {
            "target": "chat_history" if target == CHAT_TARGET else target,
            "state": "locked" if lock else ("encrypting" if job.state in ("queued", "running")
                                            else ("failed" if job.state == "failed" else "ok")),
            "scope": dk.master_scope if dk else "global",
            "job": job.to_json(),
            # The graph no longer exists in TigerGraph (None: not checked).
            "removed": None if live is None or target == CHAT_TARGET else target not in live,
        }
        if lock:
            info["locked"] = lock.to_json()
        return info

    def status(self, graph: Optional[str] = None, live_graphs=None) -> dict:
        live = set(live_graphs) if live_graphs is not None else None
        if graph:
            dk = self._load_dk(graph)
            scope = dk.master_scope if dk else "global"
            return {
                "graph": graph,
                "scope": scope,
                "key": self._ring_info("graph", graph) if scope == "graph" else None,
                "global_key": self._ring_info("global", None),
                "target": self._target_info(graph, live),
            }
        return {"key": self._ring_info("global", None),
                "targets": [self._target_info(t, live) for t in self._targets()]}

    # ------------------------------------------------------------------ jobs

    def schedule(self, target: str) -> None:
        with self._lock:
            job = self._jobs.get(target)
            if job and job.state in ("queued", "running"):
                return
            self._jobs[target] = Job(state="queued")
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(target=self._run_jobs, name="encryption-jobs",
                                                daemon=True)
                self._worker.start()
        self._queue.put(target)

    def _run_jobs(self) -> None:
        while True:
            try:
                target = self._queue.get(timeout=30)
            except queue.Empty:
                return
            self.run_sweep(target)

    def run_sweep(self, target: str) -> None:
        """Encrypt every stored value of a target with its active data key,
        then drop the data keys no value uses."""
        job = self._jobs.setdefault(target, Job())
        job.state, job.done, job.error = "running", 0, ""
        try:
            keys = self.keys(target)
            sweeper = self.sweepers.get("chat" if target == CHAT_TARGET else "memory")
            if sweeper is None:
                raise RuntimeError("no sweeper registered")

            def progress(n: int) -> None:
                job.done = n

            sweeper(target, keys, progress)
            again = False
            with self._lock:
                dk = self._load_dk(target)
                if dk is not None and dk.active == keys.active:
                    if len(dk.keys) > 1:
                        dk.keys = [e for e in dk.keys if e.id == dk.active]
                        self._save_dk(target, dk)
                        self._forget(target)
                else:
                    again = dk is not None      # the data key changed during the job
            job.state = "done"
            if again:
                self.schedule(target)
        except Locked as exc:
            job.state, job.error = "failed", f"locked: {exc.reason}"
        except Exception as exc:
            logger.exception(f"encryption: encrypting {target} failed")
            job.state, job.error = "failed", str(exc)

    def _resume_sweeps(self) -> None:
        for t in self._targets():
            try:
                self.keys(t)                # opening a target schedules its job if needed
            except Locked:
                continue

    def startup(self) -> None:
        """Generate the global key if allowed, finish an interrupted rotation
        of a managed key, and encrypt values that are not encrypted yet."""
        try:
            self.ensure_global_key()
            if not self.mounted:
                self._resume_rotation("global")
            for g in self._graph_targets():
                dk = self._load_dk(g)
                if dk is not None and dk.master_scope == "graph":
                    self._resume_rotation("graph", g)
            self._resume_sweeps()
            for t in self._targets():
                lock = self.locked(t)
                if lock:
                    logger.error(f"encryption: {t} is locked ({lock.reason}); an admin must "
                                 f"provide the key {', '.join(lock.needed)}")
        except Exception:
            logger.exception("encryption: startup failed")


_manager: Optional[EncryptionManager] = None
_manager_lock = threading.Lock()


def get_manager() -> EncryptionManager:
    global _manager
    if _manager is None:
        with _manager_lock:
            if _manager is None:
                _manager = EncryptionManager()
    return _manager


def set_manager_for_tests(manager: Optional[EncryptionManager]) -> None:
    global _manager
    _manager = manager
