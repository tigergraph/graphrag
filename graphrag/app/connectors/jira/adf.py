"""Convert Atlassian Document Format (ADF) to retrieval-friendly markdown."""

from __future__ import annotations

from typing import Any


def _escape_inline(value: str) -> str:
    return value.replace("\\", "\\\\").replace("*", "\\*").replace("_", "\\_")


def _with_marks(text: str, marks: list[dict[str, Any]]) -> str:
    for mark in marks:
        mark_type = mark.get("type")
        attrs = mark.get("attrs") or {}
        if mark_type == "code":
            escaped = text.replace("`", "\\`")
            text = f"`{escaped}`"
        elif mark_type == "strong":
            text = f"**{text}**"
        elif mark_type == "em":
            text = f"*{text}*"
        elif mark_type == "strike":
            text = f"~~{text}~~"
        elif mark_type == "link" and attrs.get("href"):
            text = f"[{text}]({attrs['href']})"
    return text


def _inline(node: dict[str, Any]) -> str:
    node_type = node.get("type")
    attrs = node.get("attrs") or {}
    if node_type == "text":
        text = _escape_inline(str(node.get("text") or ""))
        return _with_marks(text, node.get("marks") or [])
    if node_type == "hardBreak":
        return "  \n"
    if node_type == "mention":
        return f"@{attrs.get('text') or attrs.get('displayName') or 'user'}"
    if node_type == "emoji":
        return str(attrs.get("text") or attrs.get("shortName") or "")
    if node_type == "inlineCard":
        url = str(attrs.get("url") or "")
        return f"[{url}]({url})" if url else ""
    return "".join(_inline(child) for child in node.get("content") or [])


def _block(node: dict[str, Any], depth: int = 0) -> str:
    node_type = node.get("type")
    attrs = node.get("attrs") or {}
    children = node.get("content") or []

    if node_type == "doc":
        return "\n\n".join(
            value for child in children if (value := _block(child, depth)).strip()
        )
    if node_type == "paragraph":
        return "".join(_inline(child) for child in children).strip()
    if node_type == "heading":
        level = min(max(int(attrs.get("level") or 1), 1), 6)
        return f"{'#' * level} {''.join(_inline(c) for c in children).strip()}"
    if node_type == "blockquote":
        content = "\n".join(_block(child, depth) for child in children).strip()
        return "\n".join(f"> {line}" for line in content.splitlines())
    if node_type == "codeBlock":
        language = attrs.get("language") or ""
        content = "".join(_inline(child) for child in children)
        return f"```{language}\n{content}\n```"
    if node_type == "rule":
        return "---"
    if node_type in ("bulletList", "orderedList"):
        ordered = node_type == "orderedList"
        start = int(attrs.get("order") or 1)
        lines: list[str] = []
        for index, child in enumerate(children):
            value = _block(child, depth + 1).strip()
            if not value:
                continue
            prefix = f"{start + index}. " if ordered else "- "
            indentation = "  " * depth
            continuation = "\n".join(
                f"{indentation}  {line}" for line in value.splitlines()[1:]
            )
            first = f"{indentation}{prefix}{value.splitlines()[0]}"
            lines.append(f"{first}\n{continuation}".rstrip())
        return "\n".join(lines)
    if node_type == "listItem":
        return "\n".join(
            value for child in children if (value := _block(child, depth)).strip()
        )
    if node_type in ("table", "tableRow", "tableCell", "tableHeader"):
        # ADF tables can contain arbitrary blocks. Tabs preserve cell
        # boundaries for embedding without pretending to provide full GFM.
        separator = "\n" if node_type in ("table", "tableRow") else " "
        values = [_block(child, depth).strip() for child in children]
        values = [value for value in values if value]
        if node_type == "tableRow":
            separator = " | "
        return separator.join(values)
    if node_type in ("panel", "expand", "nestedExpand"):
        title = str(attrs.get("title") or "").strip()
        body = "\n\n".join(
            value for child in children if (value := _block(child, depth)).strip()
        )
        return f"**{title}**\n\n{body}".strip() if title else body
    if node_type == "mediaSingle":
        return "\n".join(_block(child, depth) for child in children).strip()
    if node_type == "media":
        name = attrs.get("alt") or attrs.get("id") or "attachment"
        return f"[Attachment: {name}]"
    return "".join(_inline(child) for child in children).strip()


def adf_to_markdown(value: Any) -> str:
    """Return markdown for an ADF document, or a safe string fallback."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if not isinstance(value, dict):
        return str(value).strip()
    return _block(value).strip()
