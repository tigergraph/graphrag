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
      "agent_engine": "default",         # "default" | "deepagent" (both Deep Agent) | "legacy"
      "deep_agent": {
        "memory_backend": "sqlite",      # "sqlite" (default) | "memory" (until restart)
        "recursion_limit": 80,
        "long_term_memory": true,
        "subagents": true,
        "allow_installed_queries": true,
        "max_tool_output_chars": 60000
      }
    }

Memory is kept per graph. Each graph has its own SQLite file,
``configs/graph_data/<graph>/agent_memory.sqlite``. ``graph_data`` holds the
data GraphRAG keeps for a graph, apart from its settings in
``graph_configs``. ``configs/`` is the volume every deployment already
mounts, so memory survives container recreation without another volume.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

MEMORY_FILE = "agent_memory.sqlite"


def memory_path(graphname: str) -> str:
    """The SQLite memory file of a graph."""
    return os.path.join("configs", "graph_data", graphname, MEMORY_FILE)


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
    path: str = ""


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


def memory_settings(graphrag_cfg, graphname: str) -> MemorySettings:
    """Memory settings of one graph (``graphrag_cfg`` is that graph's
    configuration)."""
    sec = _section(graphrag_cfg)
    return MemorySettings(
        backend=str(sec.get("memory_backend") or "sqlite").strip().lower(),
        path=memory_path(graphname),
    )


AGENT_ENGINE_DEFAULT = "default"
AGENT_ENGINE_DEEPAGENT = "deepagent"
AGENT_ENGINE_LEGACY = "legacy"
# The engine that "default" (and an unset or unknown value) selects.
DEFAULT_AGENT_ENGINE = AGENT_ENGINE_DEEPAGENT


def use_deep_agent(graphrag_cfg) -> bool:
    """Whether Agent mode runs on the Deep Agent engine. ``agent_engine`` is
    ``"default"``, ``"deepagent"`` or ``"legacy"``. ``"default"`` selects the
    default engine, which is the Deep Agent; ``"deepagent"`` selects the Deep
    Agent by name; ``"legacy"`` selects the previous planner/react engine. An
    unset or unknown value means ``"default"``."""
    engine = str((graphrag_cfg or {}).get("agent_engine") or "").strip().lower()
    if engine not in (AGENT_ENGINE_DEEPAGENT, AGENT_ENGINE_LEGACY):
        engine = DEFAULT_AGENT_ENGINE
    return engine == AGENT_ENGINE_DEEPAGENT
