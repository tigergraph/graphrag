# Copyright (c) 2024-2026 TigerGraph, Inc.
#
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0).
# See the LICENSE file or <https://www.gnu.org/licenses/agpl-3.0.html>
# for the full license text.

"""Translate an MCP ``CallToolResult`` into the agentic tool-result dict.

MCP tool results are a list of typed content blocks (text, image,
resource). The agentic engine consumes a uniform ``{ok, summary, context,
citations}`` dict. This module reduces the content list to one such dict:
- concatenated text payloads (parsed as JSON when possible) become
  ``context.result``
- the synthesizer reads ``context.result`` (and ``summary``) directly
- non-text blocks are summarized inline; the planner can ask the tool
  again with narrower args if needed
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


def _content_to_text(block: Any) -> str:
    # mcp.types.TextContent / ImageContent / EmbeddedResource have
    # different shapes; only ``text`` blocks contribute to the result
    # body. Everything else is reported via a short marker so the planner
    # at least knows something non-text came back.
    kind = getattr(block, "type", None)
    if kind == "text":
        return getattr(block, "text", "") or ""
    if kind == "image":
        mime = getattr(block, "mimeType", "image/*")
        return f"[image:{mime}]"
    if kind == "resource":
        uri = getattr(getattr(block, "resource", None), "uri", "?")
        return f"[resource:{uri}]"
    # Fallback for unknown block kinds.
    return str(block)


def normalize_call_tool_result(result: Any, qualified_name: str) -> dict:
    """Reduce an MCP ``CallToolResult`` to the agentic tool-result dict.

    Sets ``ok=False`` when the result's ``isError`` flag is set, otherwise
    ``ok=True``. Concatenates text blocks and tries to JSON-parse the
    combined body; on parse failure the raw text is kept under
    ``context.result``.
    """
    is_error = bool(getattr(result, "isError", False))
    content = getattr(result, "content", None) or []
    text_parts = [_content_to_text(b) for b in content]
    combined = "\n".join(p for p in text_parts if p)

    parsed: Any = combined
    if combined:
        body = combined.strip()
        if body.startswith("```"):
            # Some servers wrap JSON in a fenced block.
            body = body.split("```", 2)[1]
            if body.startswith("json"):
                body = body[4:]
            body = body.strip()
        try:
            parsed = json.loads(body)
        except Exception:
            parsed = combined  # keep raw text

    summary = f"{qualified_name}: {'failed' if is_error else 'ok'}"
    return {
        "ok": not is_error,
        "summary": summary,
        "context": {"function_call": qualified_name, "result": parsed},
        "citations": [],
    }
