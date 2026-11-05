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

Both are process-wide singletons. On-prem persistence uses SQLite by default
(``langgraph-checkpoint-sqlite``); Postgres (``langgraph-checkpoint-postgres``)
is used when configured; ``memory`` keeps everything in-process, for tests.
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
_backends: Optional[_Backends] = None


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


def _sqlite_backends(path: str) -> _Backends:
    import sqlite3
    from langgraph.checkpoint.sqlite import SqliteSaver
    from langgraph.store.sqlite import SqliteStore

    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    # One connection per backend, shared across worker threads. Both classes
    # serialize access with their own lock.
    cp_conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
    cp_conn.execute("PRAGMA journal_mode=WAL")
    checkpointer = SqliteSaver(cp_conn, serde=serializer())
    checkpointer.setup()
    st_conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
    store = SqliteStore(st_conn)
    store.setup()
    return _Backends(checkpointer, store, "sqlite", (cp_conn.close, st_conn.close))


def _postgres_backends(uri: str) -> _Backends:
    from langgraph.checkpoint.postgres import PostgresSaver
    from langgraph.store.postgres import PostgresStore
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool

    kwargs = {"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row}
    pool = ConnectionPool(conninfo=uri, max_size=10, kwargs=kwargs, open=True)
    checkpointer = PostgresSaver(pool, serde=serializer())
    checkpointer.setup()
    store = PostgresStore(pool)
    store.setup()
    return _Backends(checkpointer, store, "postgres", (pool.close,))


def init_backends(settings: MemorySettings) -> _Backends:
    """Create (once) and return the checkpointer and store.

    A persistence backend that fails to start (missing package, unreachable
    database, read-only volume) falls back to in-process memory with a
    warning, so chat keeps working; memories then last until restart.
    """
    global _backends
    if _backends is not None:
        return _backends
    with _lock:
        if _backends is not None:
            return _backends
        kind = settings.backend
        try:
            if kind == "postgres":
                if not settings.postgres_uri:
                    raise ValueError("deep_agent.postgres_uri is not set")
                _backends = _postgres_backends(settings.postgres_uri)
            elif kind == "memory":
                _backends = _memory_backends()
            else:
                _backends = _sqlite_backends(settings.path)
            logger.info(f"deep_agent: memory backend={_backends.kind}")
        except Exception as exc:
            logger.warning(
                f"deep_agent: memory backend {kind!r} unavailable ({exc}); "
                "using in-process memory — conversations and memories will not "
                "survive a restart"
            )
            _backends = _memory_backends()
        return _backends


def get_backends() -> _Backends:
    if _backends is None:
        from common.config import get_graphrag_config
        from deep_agent.settings import memory_settings
        return init_backends(memory_settings(get_graphrag_config()))
    return _backends


def close_backends() -> None:
    """Release database connections (application shutdown)."""
    global _backends
    with _lock:
        if _backends is None:
            return
        for close in _backends.closers:
            try:
                close()
            except Exception as exc:
                logger.warning(f"deep_agent: closing memory backend failed: {exc}")
        _backends = None


def reset_backends_for_tests(backends: Optional[_Backends] = None) -> None:
    global _backends
    _backends = backends


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


def thread_messages(tid: str) -> list:
    """The messages of a thread (empty when the thread does not exist)."""
    checkpointer = get_backends().checkpointer
    if checkpointer.get_tuple(_config(tid)) is None:
        return []
    state = _thread_reader(checkpointer).get_state(_config(tid))
    return list((state.values or {}).get("messages") or [])


def list_threads(graphname: str, user: str, limit: int = 100) -> list:
    """Thread ids (latest checkpoint per thread) of a user on a graph."""
    prefix = thread_prefix(graphname, user)
    seen, out = set(), []
    for tup in get_backends().checkpointer.list(None):
        tid = (tup.config or {}).get("configurable", {}).get("thread_id", "")
        if not tid.startswith(prefix) or tid in seen:
            continue
        seen.add(tid)
        out.append({
            "thread_id": tid,
            "conversation_id": tid[len(prefix):],
            "updated_at": (tup.checkpoint or {}).get("ts"),
        })
        if len(out) >= limit:
            break
    return out


def delete_thread(tid: str) -> None:
    get_backends().checkpointer.delete_thread(tid)


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
    items = get_backends().store.search(memory_namespace(graphname, user), limit=1000)
    return [{
        "path": MEMORIES_ROUTE.rstrip("/") + it.key,
        "size": len(str((it.value or {}).get("content", ""))),
        "modified_at": (it.value or {}).get("modified_at"),
    } for it in items]


def read_memory(graphname: str, user: str, path: str) -> Optional[str]:
    item = get_backends().store.get(memory_namespace(graphname, user), _key(path))
    if item is None:
        return None
    content = (item.value or {}).get("content", "")
    return "\n".join(content) if isinstance(content, list) else str(content)


def write_memory(graphname: str, user: str, path: str, content: str) -> str:
    key = _key(path)
    ns = memory_namespace(graphname, user)
    store = get_backends().store
    existing = store.get(ns, key)
    now = _now()
    created = ((existing.value or {}).get("created_at") if existing else None) or now
    store.put(ns, key, {"content": content, "encoding": "utf-8",
                        "created_at": created, "modified_at": now})
    return MEMORIES_ROUTE.rstrip("/") + key


def delete_memory(graphname: str, user: str, path: str) -> bool:
    key = _key(path)
    ns = memory_namespace(graphname, user)
    store = get_backends().store
    if store.get(ns, key) is None:
        return False
    store.delete(ns, key)
    return True
