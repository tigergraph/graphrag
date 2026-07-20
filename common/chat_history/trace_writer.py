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

from __future__ import annotations

import logging
import queue
import threading
from typing import Callable, Optional

logger = logging.getLogger(__name__)

DEFAULT_MAX_QUEUE = 512


class TraceWriter:
    """Serializes trace writes onto a worker thread."""

    def __init__(self, max_queue: int = DEFAULT_MAX_QUEUE):
        self._queue: queue.Queue = queue.Queue(maxsize=max_queue)
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._stopping = threading.Event()
        self._submitted = 0
        self._written = 0
        self._dropped = 0
        self._failed = 0

    def _ensure_worker(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stopping.clear()
            self._thread = threading.Thread(
                target=self._drain, name="chat-trace-writer", daemon=True
            )
            self._thread.start()

    def _drain(self) -> None:
        while not self._stopping.is_set():
            try:
                job = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue
            try:
                job()
                self._written += 1
            except Exception:
                self._failed += 1
                logger.warning("Trace write failed", exc_info=True)
            finally:
                self._queue.task_done()

    def submit(self, job: Callable[[], None]) -> bool:
        """Queue *job*. Returns False if it was dropped.

        Never raises and never blocks: the caller is on the response path.
        """
        self._ensure_worker()
        self._submitted += 1
        try:
            self._queue.put_nowait(job)
            return True
        except queue.Full:
            self._dropped += 1
            logger.warning(
                "Trace queue full (%d); dropping trace. dropped_total=%d",
                self._queue.maxsize,
                self._dropped,
            )
            return False

    def flush(self, timeout: float = 5.0) -> bool:
        """Block until the queue drains. For tests and shutdown."""
        deadline = threading.Event()
        waiter = threading.Thread(target=lambda: (self._queue.join(), deadline.set()))
        waiter.daemon = True
        waiter.start()
        return deadline.wait(timeout)

    def stats(self) -> dict:
        return {
            "submitted": self._submitted,
            "written": self._written,
            "dropped": self._dropped,
            "failed": self._failed,
            "queued": self._queue.qsize(),
        }

trace_writer = TraceWriter()
