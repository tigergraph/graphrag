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

"""Guardrails for agentic tool calls.

The agentic engine lets the LLM set retrieval parameters (`top_k`,
`num_hops`, `community_level`) per call and widen them on thin results.
These helpers clamp each to a sane ceiling so a planned (or hallucinated)
argument can't issue a pathological query, while leaving the
``graphrag_config`` value as the default when the agent omits it.

Only read-side tools are ever registered for the chat agent (see
``tool_registry``); there is no write/mutating tool surface here.
"""

# Hard ceilings on agent-settable retrieval parameters. The agent may
# widen toward these on thin results; it cannot exceed them.
MAX_TOP_K = 50
MAX_NUM_HOPS = 4
MAX_COMMUNITY_LEVEL = 5


def _clamp_int(value, default: int, lo: int, hi: int) -> int:
    """Return ``value`` coerced to int and clamped to [lo, hi]; fall back
    to ``default`` when ``value`` is None or not int-coercible.
    """
    if value is None:
        value = default
    try:
        value = int(value)
    except (TypeError, ValueError):
        return max(lo, min(default, hi))
    return max(lo, min(value, hi))


def clamp_top_k(value, default: int) -> int:
    return _clamp_int(value, default, 1, MAX_TOP_K)


def clamp_num_hops(value, default: int) -> int:
    return _clamp_int(value, default, 1, MAX_NUM_HOPS)


def clamp_community_level(value, default: int) -> int:
    return _clamp_int(value, default, 1, MAX_COMMUNITY_LEVEL)


def clamp_similarity_threshold(value, default: float) -> float:
    """Clamp a cosine-similarity threshold to [0.0, 1.0]."""
    if value is None:
        return default
    try:
        value = float(value)
    except (TypeError, ValueError):
        return default
    return max(0.0, min(value, 1.0))


# --- external MCP allowlist --------------------------------------------------

def is_tool_allowed(allowed_patterns, tool_name: str) -> bool:
    """Return True when ``tool_name`` matches any glob in ``allowed_patterns``.

    Filters the tool list an external MCP server publishes down to the
    set the admin opted into via ``McpServerSpec.allowed_tools``. Default
    ``["*"]`` admits every tool; narrower patterns (e.g. ``["get_*",
    "list_*"]``) cap the surface to read-only verbs even when the server
    publishes mutating tools.
    """
    import fnmatch
    if not allowed_patterns:
        return False
    return any(fnmatch.fnmatch(tool_name, pat) for pat in allowed_patterns)
