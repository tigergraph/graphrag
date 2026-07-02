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
