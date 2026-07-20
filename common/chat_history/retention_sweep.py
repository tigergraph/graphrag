# Copyright (c) 2024-2026 TigerGraph, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Periodic expiry of traces past their retention window.

Traces hold the user's prompts and the text of retrieved documents, so the
window is a privacy control and has to run whether or not anyone is chatting.

Replaces the file writer's sweep, which ran a full directory scan inside
*every* trace write — correct in the sense that it always ran, and expensive
in the sense that it ran on the response path. This runs on a timer instead,
using the service credential the embedding store already connects with, since
no user request is in flight when it fires.

Multi-worker note: each worker runs its own sweep, so with N workers the query
runs N times per interval. The deletes are idempotent (a trace already gone
matches nothing), so the cost is a few redundant queries per day rather than
incorrect behaviour. Left as-is rather than adding a lock: coordination for a
daily idempotent delete is not worth an external dependency.
"""

from __future__ import annotations

import logging
import threading

from common.chat_history.repository import TraceRetention

logger = logging.getLogger(__name__)

DEFAULT_RETENTION_DAYS = 30
DEFAULT_INTERVAL_HOURS = 24
_INITIAL_DELAY_S = 300


class RetentionSweeper:
    """Runs trace expiry on a timer across every graph."""

    def __init__(
        self,
        conn_factory,
        graphs_factory,
        retention_days: int = DEFAULT_RETENTION_DAYS,
        interval_hours: int = DEFAULT_INTERVAL_HOURS,
    ):
        self._conn_factory = conn_factory
        self._graphs_factory = graphs_factory
        self._retention_days = int(retention_days)
        self._interval_s = max(1, int(interval_hours)) * 3600
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def sweep_once(self) -> int:
        """Expire stale traces on every graph. Returns the count deleted."""
        deleted = 0
        try:
            graphs = self._graphs_factory() or []
        except Exception:
            logger.warning("Trace retention: could not list graphs", exc_info=True)
            return 0

        for graphname in graphs:
            try:
                conn = self._conn_factory(graphname)
                count = TraceRetention(conn).expire(self._retention_days)
                if count:
                    logger.info(
                        "Trace retention: expired %d trace(s) older than %d days on %s",
                        count, self._retention_days, graphname,
                    )
                deleted += count
            except Exception:
                logger.debug(
                    "Trace retention: sweep skipped for graph %s", graphname,
                    exc_info=True,
                )
        return deleted

    def _run(self) -> None:
        if self._stop.wait(_INITIAL_DELAY_S):
            return
        while not self._stop.is_set():
            try:
                self.sweep_once()
            except Exception:
                logger.warning("Trace retention sweep failed", exc_info=True)
            self._stop.wait(self._interval_s)

    def start(self) -> None:
        if self._retention_days <= 0:
            logger.info("Trace retention disabled (retention_days=%d)", self._retention_days)
            return
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="chat-trace-retention", daemon=True
        )
        self._thread.start()
        logger.info(
            "Trace retention started: %d-day window, sweeping every %dh",
            self._retention_days, self._interval_s // 3600,
        )

    def stop(self) -> None:
        self._stop.set()
