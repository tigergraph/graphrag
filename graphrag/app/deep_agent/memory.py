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

"""Short-term and long-term memory for the Deep Agent.

* STM — a LangGraph checkpointer. Each conversation is one thread whose id is
  built from the graph, the user and the conversation id, so one user's
  thread can never be resumed by another.
* LTM — a LangGraph store. The agent's ``/memories/`` directory is routed to
  it (see ``agent.py``) under the namespace ``("deep_agent", graph, user,
  "memories")``, so what the agent remembers for a user on a graph persists
  across conversations and restarts and is private to that user.

Both are kept per graph, in one SQLite file per graph
(``langgraph-checkpoint-sqlite``) in the graph's data folder, encrypted with
the graph's data key (``data_encryption``). ``memory`` keeps everything
in-process, for tests.
"""

from __future__ import annotations

import logging
import os
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from deep_agent.settings import MemorySettings

logger = logging.getLogger(__name__)

AGENTS_MD = "/AGENTS.md"            # path of the auto-loaded memory file, inside /memories/
MEMORIES_ROUTE = "/memories/"


@dataclass
class _Backends:
    checkpointer: Any
    store: Any
    kind: str
    closers: tuple = ()


_lock = threading.Lock()
# Open backends by graph name.
_backends: dict = {}
_test_backends: Optional[_Backends] = None


# --------------------------------------------------------------------------
# Ids and namespaces
# --------------------------------------------------------------------------

_UNSAFE = re.compile(r"[^A-Za-z0-9_.@+\-]")


def _safe(part: str) -> str:
    """A namespace/thread component: store namespaces reject '.'-free wildcards
    and some punctuation, so anything unusual is replaced."""
    part = _UNSAFE.sub("_", str(part or "").strip())
    return part or "_"


def thread_id(graphname: str, user: str, conversation_id: str) -> str:
    """Checkpointer thread id for one conversation of one user on one graph."""
    return f"{_safe(graphname)}:{_safe(user)}:{_safe(conversation_id)}"


def thread_prefix(graphname: str, user: str) -> str:
    return f"{_safe(graphname)}:{_safe(user)}:"


def memory_namespace(graphname: str, user: str) -> tuple:
    """Store namespace of a user's long-term memories on a graph."""
    return ("deep_agent", _safe(graphname), _safe(user), "memories")


# --------------------------------------------------------------------------
# Backends
# --------------------------------------------------------------------------

# Types the Deep Agent keeps in its state besides LangChain messages; the
# checkpoint serializer only revives allowlisted types.
_STATE_TYPES = [("deep_agent.agent", "DeepAgentAnswer")]


def serializer():
    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
    return JsonPlusSerializer(allowed_msgpack_modules=_STATE_TYPES)


def _memory_backends() -> _Backends:
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.store.memory import InMemoryStore
    return _Backends(InMemorySaver(serde=serializer()), InMemoryStore(), "memory")


def _sqlite_backends(path: str, graphname: str) -> _Backends:
    import sqlite3
    from langgraph.checkpoint.sqlite import SqliteSaver
    from langgraph.store.sqlite import SqliteStore

    import data_encryption
    from data_encryption.memory_crypto import EncryptedStore, checkpoint_serializer

    enc = data_encryption.manager()
    enc.keys(graphname)                 # raises Locked before the file is opened

    # SQLite cannot open a path through a symlink whose target climbs above the
    # root directory, such as the image's /code/configs -> ../../configs link to
    # the mounted volume; open the real path.
    path = os.path.realpath(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    # One connection per backend, shared across worker threads. Both classes
    # serialize access with their own lock.
    cp_conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
    cp_conn.execute("PRAGMA journal_mode=WAL")
    checkpointer = SqliteSaver(cp_conn, serde=checkpoint_serializer(enc, graphname, serializer()))
    checkpointer.setup()
    st_conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
    inner = SqliteStore(st_conn)
    inner.setup()
    store = EncryptedStore(inner, enc, graphname)
    return _Backends(checkpointer, store, "sqlite", (cp_conn.close, st_conn.close))


def _open(settings: MemorySettings, graphname: str) -> _Backends:
    if settings.backend == "memory":
        return _memory_backends()
    return _sqlite_backends(settings.path, graphname)


def init_backends(settings: MemorySettings, graphname: str) -> _Backends:
    """Open (once) and return the checkpointer and store of a graph.

    A persistence backend that fails to start (missing package, read-only
    volume) falls back to in-process memory with a
    warning, so chat keeps working; memories then last until restart.
    A graph whose encryption key cannot be loaded raises ``Locked`` and is
    not cached, so it opens as soon as an admin provides the key.
    """
    from data_encryption import Locked
    if _test_backends is not None:
        return _test_backends
    backends = _backends.get(graphname)
    if backends is not None:
        return backends
    with _lock:
        backends = _backends.get(graphname)
        if backends is not None:
            return backends
        try:
            backends = _open(settings, graphname)
            logger.info(f"deep_agent: memory backend={backends.kind} for {graphname}")
        except Locked:
            raise
        except Exception as exc:
            logger.warning(
                f"deep_agent: memory backend {settings.backend!r} unavailable for "
                f"{graphname} ({exc}); using in-process memory — conversations and "
                "memories will not survive a restart"
            )
            backends = _memory_backends()
        _backends[graphname] = backends
        return backends


def get_backends(graphname: str) -> _Backends:
    """The checkpointer and store of a graph, opened on first use."""
    if _test_backends is not None:
        return _test_backends
    backends = _backends.get(graphname)
    if backends is not None:
        return backends
    from common.config import get_graphrag_config
    from deep_agent.settings import memory_settings
    return init_backends(memory_settings(get_graphrag_config(graphname), graphname), graphname)


def close_backends() -> None:
    """Release database connections (application shutdown)."""
    with _lock:
        for backends in _backends.values():
            for close in backends.closers:
                try:
                    close()
                except Exception as exc:
                    logger.warning(f"deep_agent: closing memory backend failed: {exc}")
        _backends.clear()


def close_graph_backends(graphname: str) -> None:
    """Close the memory of one graph (before its file is deleted)."""
    with _lock:
        backends = _backends.pop(graphname, None)
        if backends is not None:
            _readers.pop(id(backends.checkpointer), None)
            for close in backends.closers:
                try:
                    close()
                except Exception as exc:
                    logger.warning(f"deep_agent: closing memory of {graphname} failed: {exc}")


def reset_backends_for_tests(backends: Optional[_Backends] = None) -> None:
    """Use ``backends`` for every graph (``None`` restores normal lookup)."""
    global _test_backends
    _test_backends = backends


# --------------------------------------------------------------------------
# STM: threads
# --------------------------------------------------------------------------

def _config(tid: str) -> dict:
    return {"configurable": {"thread_id": tid}}


_readers: dict = {}


def _thread_reader(checkpointer):
    """A no-op graph with the Deep Agent's state schema on ``checkpointer``.

    The Deep Agent stores ``messages`` as a delta channel (each checkpoint
    holds only new messages), so a thread is read back through LangGraph
    rather than from raw checkpoint blobs.
    """
    reader = _readers.get(id(checkpointer))
    if reader is None:
        from deepagents import DeepAgentState
        from langgraph.graph import END, START, StateGraph
        g = StateGraph(DeepAgentState)
        g.add_node("noop", lambda _s: {})
        g.add_edge(START, "noop")
        g.add_edge("noop", END)
        reader = g.compile(checkpointer=checkpointer)
        _readers[id(checkpointer)] = reader
    return reader


def thread_messages(graphname: str, tid: str) -> list:
    """The messages of a thread (empty when the thread does not exist)."""
    checkpointer = get_backends(graphname).checkpointer
    if checkpointer.get_tuple(_config(tid)) is None:
        return []
    state = _thread_reader(checkpointer).get_state(_config(tid))
    return list((state.values or {}).get("messages") or [])


def _latest_checkpoints(checkpointer, prefix: str, limit: int) -> list:
    """``(thread_id, checkpoint)`` of the newest checkpoint of each thread
    whose id starts with ``prefix``, newest thread first.

    Thread ids are the first column of the checkpoint key, so SQLite reads
    only the user's rows; a full ``list()`` would read and decode every
    checkpoint of every user on the graph while holding the saver's lock.
    """
    if hasattr(checkpointer, "cursor") and hasattr(checkpointer, "conn"):  # SqliteSaver
        # ``prefix`` ends with ":"; ";" is the next character, so the range
        # holds exactly the ids that start with ``prefix``.
        hi = prefix[:-1] + ";"
        with checkpointer.cursor(transaction=False) as cur:
            cur.execute(
                "SELECT c.thread_id, c.type, c.checkpoint FROM checkpoints c "
                "JOIN (SELECT thread_id, MAX(checkpoint_id) AS cid FROM checkpoints "
                "      WHERE thread_id >= ? AND thread_id < ? AND checkpoint_ns = '' "
                "      GROUP BY thread_id) m "
                "ON c.thread_id = m.thread_id AND c.checkpoint_ns = '' AND c.checkpoint_id = m.cid "
                "ORDER BY c.checkpoint_id DESC LIMIT ?",
                (prefix, hi, limit),
            )
            rows = cur.fetchall()
        return [(tid, checkpointer.serde.loads_typed((typ, blob))) for tid, typ, blob in rows]
    storage = getattr(checkpointer, "storage", None)
    if isinstance(storage, dict):  # InMemorySaver
        latest = []
        for tid in [t for t in list(storage) if t.startswith(prefix)]:
            tup = checkpointer.get_tuple(_config(tid))
            if tup is not None:
                latest.append((tid, tup.checkpoint or {}))
        latest.sort(key=lambda x: x[1].get("id", ""), reverse=True)
        return latest[:limit]
    # Other savers: scan.
    seen, latest = set(), []
    for tup in checkpointer.list(None):
        tid = (tup.config or {}).get("configurable", {}).get("thread_id", "")
        if tid.startswith(prefix) and tid not in seen:
            seen.add(tid)
            latest.append((tid, tup.checkpoint or {}))
            if len(latest) >= limit:
                break
    return latest


def list_threads(graphname: str, user: str, limit: int = 100) -> list:
    """Thread ids (latest checkpoint per thread) of a user on a graph,
    most recently updated first."""
    prefix = thread_prefix(graphname, user)
    return [{
        "thread_id": tid,
        "conversation_id": tid[len(prefix):],
        "updated_at": (cp or {}).get("ts"),
    } for tid, cp in _latest_checkpoints(get_backends(graphname).checkpointer, prefix, limit)]


def delete_thread(graphname: str, tid: str) -> None:
    get_backends(graphname).checkpointer.delete_thread(tid)


# --------------------------------------------------------------------------
# LTM: memory files (same record format StoreBackend writes)
# --------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _key(path: str) -> str:
    path = "/" + str(path or "").strip().lstrip("/")
    if path.startswith(MEMORIES_ROUTE):
        path = "/" + path[len(MEMORIES_ROUTE):]
    if ".." in path.split("/") or path == "/":
        raise ValueError(f"invalid memory path: {path!r}")
    return path


def list_memories(graphname: str, user: str) -> list:
    items = get_backends(graphname).store.search(memory_namespace(graphname, user), limit=1000)
    return [{
        "path": MEMORIES_ROUTE.rstrip("/") + it.key,
        "size": len(str((it.value or {}).get("content", ""))),
        "modified_at": (it.value or {}).get("modified_at"),
    } for it in items]


def read_memory(graphname: str, user: str, path: str) -> Optional[str]:
    item = get_backends(graphname).store.get(memory_namespace(graphname, user), _key(path))
    if item is None:
        return None
    content = (item.value or {}).get("content", "")
    return "\n".join(content) if isinstance(content, list) else str(content)


def write_memory(graphname: str, user: str, path: str, content: str) -> str:
    key = _key(path)
    ns = memory_namespace(graphname, user)
    store = get_backends(graphname).store
    existing = store.get(ns, key)
    now = _now()
    created = ((existing.value or {}).get("created_at") if existing else None) or now
    store.put(ns, key, {"content": content, "encoding": "utf-8",
                        "created_at": created, "modified_at": now})
    return MEMORIES_ROUTE.rstrip("/") + key


def delete_memory(graphname: str, user: str, path: str) -> bool:
    key = _key(path)
    ns = memory_namespace(graphname, user)
    store = get_backends(graphname).store
    if store.get(ns, key) is None:
        return False
    store.delete(ns, key)
    return True
