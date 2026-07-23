# Copyright (c) 2024-2026 TigerGraph, Inc.
#
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0).
# See the LICENSE file or <https://www.gnu.org/licenses/agpl-3.0.html>
# for the full license text.

"""Sync ↔ async bridge for the external MCP client manager.

The agentic executor is sync — it calls registered tool functions
directly. The MCP SDK is async, and the manager's connections (long-lived
stdio subprocesses and HTTP sessions) keep ``asyncio.Lock`` instances and
streams bound to whichever event loop opened them. So per-call
``asyncio.run`` is wrong: each call would create a new loop, and the next
call would find locks bound to a dead one.

This module owns a dedicated background event loop thread. ``run_sync``
schedules a coroutine onto it and blocks for the result; ``run_async``
returns an awaitable for callers inside FastAPI's main loop. Every MCP
operation goes through this loop, so the manager's async state is
consistent across calls and across threads.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any, Awaitable, Coroutine, Optional

logger = logging.getLogger(__name__)

_loop: Optional[asyncio.AbstractEventLoop] = None
_thread: Optional[threading.Thread] = None
_start_lock = threading.Lock()


def _ensure_loop() -> asyncio.AbstractEventLoop:
    global _loop, _thread
    if _loop is not None and _loop.is_running():
        return _loop
    with _start_lock:
        if _loop is not None and _loop.is_running():
            return _loop
        loop = asyncio.new_event_loop()

        def _runner() -> None:
            asyncio.set_event_loop(loop)
            try:
                loop.run_forever()
            finally:
                loop.close()

        t = threading.Thread(target=_runner, name="mcp-addons", daemon=True)
        t.start()
        _loop = loop
        _thread = t
        return _loop


def run_sync(coro: Coroutine[Any, Any, Any], timeout: Optional[float] = None) -> Any:
    """Run ``coro`` on the dedicated MCP loop and block for its result.

    Used from the sync agent executor. ``timeout`` is in seconds (None =
    wait forever — the loop is daemonized so it dies with the process).
    """
    loop = _ensure_loop()
    fut = asyncio.run_coroutine_threadsafe(coro, loop)
    return fut.result(timeout=timeout)


def run_async(coro: Coroutine[Any, Any, Any]) -> Awaitable[Any]:
    """Run ``coro`` on the dedicated MCP loop and return an awaitable.

    Use from FastAPI's main event loop (e.g. WebSocket handlers) so the
    main loop doesn't block on MCP I/O.
    """
    loop = _ensure_loop()
    fut = asyncio.run_coroutine_threadsafe(coro, loop)
    return asyncio.wrap_future(fut)


def stop_loop() -> None:
    """Stop the dedicated loop. Call on application shutdown."""
    global _loop, _thread
    if _loop is None:
        return
    loop = _loop
    if loop.is_running():
        loop.call_soon_threadsafe(loop.stop)
    if _thread is not None:
        _thread.join(timeout=5)
    _loop = None
    _thread = None
