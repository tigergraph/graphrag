# Copyright (c) 2026 TigerGraph, Inc.
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

"""Time, model calls, tokens and cost of one ingest, rebuild or summary run.

A run sets a collector in a context variable. Asyncio tasks that the run
creates, and threads started with ``asyncio.to_thread``, see the same
collector; code that hands work to an executor passes it on with
``contextvars.copy_context``. Each model call adds its usage with ``record``.

At the end the run writes one summary line to the log and saves the summary
in ``configs/graph_data/<graph>/run_usage.json``, one entry per kind of run,
so the UI can show the last run after the services restart.
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
import threading
import time
from contextlib import contextmanager
from typing import Iterator, Optional

logger = logging.getLogger(__name__)

_DATA_ROOT = os.path.join("configs", "graph_data")
_FILE = "run_usage.json"
_file_lock = threading.Lock()

# Kinds of runs, as saved and shown.
INGEST = "ingest"          # ingest from Stored Files (converts files not converted yet)
CONVERSION = "conversion"  # file conversion when files are added
REBUILD = "rebuild"
CHUNK_SUMMARIES = "chunk_summaries"


class RunUsage:
    def __init__(self, kind: str, graphname: str):
        self.kind, self.graphname = kind, graphname
        self.started = time.time()
        self._t0 = time.perf_counter()
        self.seconds: Optional[float] = None
        self.phases: dict[str, float] = {}
        self.calls: dict[str, dict] = {}
        self.status = "running"
        self._lock = threading.Lock()

    def record(self, kind: str, input_tokens: int = 0, output_tokens: int = 0,
               cost: float = 0.0, items: int = 0) -> None:
        """Add one model call of ``kind`` (for example ``entity_extraction``).
        ``items`` counts texts for embedding calls."""
        with self._lock:
            c = self.calls.setdefault(kind, {"calls": 0, "input_tokens": 0, "output_tokens": 0,
                                             "cost": 0.0, "items": 0})
            c["calls"] += 1
            c["input_tokens"] += int(input_tokens or 0)
            c["output_tokens"] += int(output_tokens or 0)
            c["cost"] += float(cost or 0.0)
            c["items"] += int(items or 0)

    @contextmanager
    def phase(self, name: str) -> Iterator[None]:
        t0 = time.perf_counter()
        try:
            yield
        finally:
            with self._lock:
                self.phases[name] = self.phases.get(name, 0.0) + time.perf_counter() - t0

    def add_phase(self, name: str, seconds: float) -> None:
        with self._lock:
            self.phases[name] = self.phases.get(name, 0.0) + float(seconds)

    def totals(self) -> dict:
        with self._lock:
            calls = list(self.calls.values())
        return {
            "calls": sum(c["calls"] for c in calls),
            "input_tokens": sum(c["input_tokens"] for c in calls),
            "output_tokens": sum(c["output_tokens"] for c in calls),
            "cost": round(sum(c["cost"] for c in calls), 6),
        }

    def summary(self) -> dict:
        seconds = self.seconds if self.seconds is not None else time.perf_counter() - self._t0
        with self._lock:
            calls = {k: {**v, "cost": round(v["cost"], 6)} for k, v in sorted(self.calls.items())}
            phases = {k: round(v, 1) for k, v in self.phases.items()}
        return {
            "kind": self.kind,
            "graphname": self.graphname,
            "status": self.status,
            "started_at": self.started,
            "seconds": round(seconds, 1),
            "phases": phases,
            "by_kind": calls,
            "totals": self.totals(),
        }

    def log_line(self) -> str:
        s = self.summary()
        t = s["totals"]
        phases = ", ".join(f"{k} {v:.0f}s" for k, v in s["phases"].items())
        parts = [
            f"{k} {v['calls']} calls"
            + (f" {v['input_tokens']}/{v['output_tokens']} tokens" if v["input_tokens"] or v["output_tokens"] else "")
            + (f" {v['items']} texts" if v["items"] else "")
            for k, v in s["by_kind"].items()
        ]
        return (
            f"{run_label(s['kind'])}: {s['graphname']} {s['status']} in {s['seconds']:.1f}s"
            + (f" ({phases})" if phases else "")
            + f"; {t['calls']} model calls, {t['input_tokens']} input / {t['output_tokens']} output tokens,"
            + f" cost {t['cost']:.4f}"
            + (f"; {'; '.join(parts)}" if parts else "")
        )


# Log label of each kind of run. Every label ends in "Usage", so
# ``grep 'Usage:'`` finds all summary lines and none of the per-call lines.
_LABELS = {
    INGEST: "Ingest Usage",
    CONVERSION: "Conversion Usage",
    REBUILD: "Rebuild Usage",
    "regenerate_chunk_summaries": "Chunk Summaries Usage",
    "regenerate_summaries": "Community Summaries Usage",
    "regenerate_embeddings": "Embeddings Usage",
    "move_heading_paths": "Section Paths Usage",
}


def run_label(kind: str) -> str:
    return _LABELS.get(kind) or f"{kind.replace('_', ' ').title()} Usage"


_current: contextvars.ContextVar = contextvars.ContextVar("graphrag_run_usage", default=None)


def current_run() -> Optional[RunUsage]:
    return _current.get()


def record(kind: str, input_tokens: int = 0, output_tokens: int = 0,
           cost: float = 0.0, items: int = 0) -> None:
    """Add a model call to the current run, if a run is being tracked."""
    run = _current.get()
    if run is not None:
        run.record(kind, input_tokens, output_tokens, cost, items)


@contextmanager
def phase(name: str) -> Iterator[None]:
    """Time a phase of the current run; does nothing outside a run."""
    run = _current.get()
    if run is None:
        yield
        return
    with run.phase(name):
        yield


def add_phase(name: str, seconds: float) -> None:
    """Add a phase that the caller timed itself to the current run."""
    run = _current.get()
    if run is not None:
        run.add_phase(name, seconds)


@contextmanager
def track_run(kind: str, graphname: str, save: bool = True) -> Iterator[RunUsage]:
    """Track one run: on exit, log its summary and save it for the graph.
    A run inside another tracked run adds to the outer one instead."""
    outer = _current.get()
    if outer is not None:
        yield outer
        return
    run = RunUsage(kind, graphname)
    token = _current.set(run)
    try:
        yield run
        run.status = "completed"
    except BaseException:
        run.status = "failed"
        raise
    finally:
        _current.reset(token)
        run.seconds = time.perf_counter() - run._t0
        logger.info(run.log_line())
        if save:
            try:
                save_summary(run.summary())
            except Exception as e:  # never fail the run over its usage record
                logger.warning(f"run usage: could not save for {graphname}: {e}")


def _path(graphname: str) -> str:
    return os.path.join(_DATA_ROOT, graphname, _FILE)


def save_summary(summary: dict) -> None:
    path = _path(summary["graphname"])
    with _file_lock:
        data = load_summaries(summary["graphname"])
        data[summary["kind"]] = summary
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)


def load_summaries(graphname: str) -> dict:
    """``{kind: summary}`` of the last run of each kind for the graph."""
    path = _path(graphname)
    if not os.path.exists(path):
        return {}
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}
