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

"""Deep Agent settings.

Read from ``graphrag_config.deep_agent`` (global, with per-graph overrides
through ``get_graphrag_config``)::

    "graphrag_config": {
      "agent_engine": "deep",            # "deep" (default) | "legacy"
      "deep_agent": {
        "memory_backend": "sqlite",      # "sqlite" (default) | "postgres" | "memory"
        "memory_path": "/code/deep_agent_data/memory.sqlite",
        "postgres_uri": "postgresql://user:pass@host:5432/db",
        "recursion_limit": 80,
        "long_term_memory": true,
        "subagents": true,
        "allow_installed_queries": true,
        "max_tool_output_chars": 60000
      }
    }

Environment variables ``DEEP_AGENT_MEMORY_BACKEND``, ``DEEP_AGENT_MEMORY_PATH``
and ``DEEP_AGENT_POSTGRES_URI`` override the persistence settings, which are
process-wide (one checkpointer and one store per service).
"""

from __future__ import annotations

import os
from dataclasses import dataclass

DEFAULT_MEMORY_PATH = os.path.join("deep_agent_data", "memory.sqlite")


@dataclass(frozen=True)
class DeepAgentSettings:
    recursion_limit: int = 80
    long_term_memory: bool = True
    subagents: bool = True
    allow_installed_queries: bool = True
    max_tool_output_chars: int = 60000


@dataclass(frozen=True)
class MemorySettings:
    backend: str = "sqlite"
    path: str = DEFAULT_MEMORY_PATH
    postgres_uri: str = ""


def _section(graphrag_cfg) -> dict:
    sec = (graphrag_cfg or {}).get("deep_agent") or {}
    return sec if isinstance(sec, dict) else {}


def _bool(value, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def _int(value, default: int, lo: int, hi: int) -> int:
    try:
        value = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(value, hi))


def agent_settings(graphrag_cfg) -> DeepAgentSettings:
    sec = _section(graphrag_cfg)
    d = DeepAgentSettings()
    return DeepAgentSettings(
        recursion_limit=_int(sec.get("recursion_limit"), d.recursion_limit, 10, 500),
        long_term_memory=_bool(sec.get("long_term_memory"), d.long_term_memory),
        subagents=_bool(sec.get("subagents"), d.subagents),
        allow_installed_queries=_bool(sec.get("allow_installed_queries"), d.allow_installed_queries),
        max_tool_output_chars=_int(sec.get("max_tool_output_chars"), d.max_tool_output_chars, 2000, 1_000_000),
    )


def memory_settings(graphrag_cfg) -> MemorySettings:
    sec = _section(graphrag_cfg)
    backend = (os.getenv("DEEP_AGENT_MEMORY_BACKEND") or sec.get("memory_backend") or "sqlite")
    return MemorySettings(
        backend=str(backend).strip().lower(),
        path=os.getenv("DEEP_AGENT_MEMORY_PATH") or sec.get("memory_path") or DEFAULT_MEMORY_PATH,
        postgres_uri=os.getenv("DEEP_AGENT_POSTGRES_URI") or sec.get("postgres_uri") or "",
    )


def use_deep_agent(graphrag_cfg) -> bool:
    """Whether Agent mode runs on the Deep Agent engine (default) or the
    legacy planner/react engine."""
    engine = str((graphrag_cfg or {}).get("agent_engine", "deep")).strip().lower()
    return engine != "legacy"
