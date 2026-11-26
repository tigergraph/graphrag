"""Convert Confluence storage HTML to markdown."""

from __future__ import annotations

from html.parser import HTMLParser


class _StorageMarkdown(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._href: str | None = None
        self._ignore = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {name: value or "" for name, value in attrs}
        if tag in {"script", "style"}:
            self._ignore += 1
            return
        if tag == "br":
            self.parts.append("\n")
        elif tag in {"p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "li", "tr"}:
            self.parts.append("\n")
        elif tag == "a":
            self._href = attributes.get("href") or ""
        elif tag == "li":
            self.parts.append("- ")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"} and self._ignore:
            self._ignore -= 1
            return
        if tag == "a" and self._href:
            self.parts.append(f" ({self._href})")
            self._href = None
        elif tag in {"p", "div", "li", "h1", "h2", "h3", "h4", "h5", "h6"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._ignore or not data:
            return
        self.parts.append(data)


def storage_to_markdown(value: str | None) -> str:
    """Turn a storage-format body into markdown-ish text."""
    if not value:
        return ""
    parser = _StorageMarkdown()
    parser.feed(value)
    parser.close()
    lines = [" ".join(line.split()) for line in "".join(parser.parts).splitlines()]
    return "\n".join(line for line in lines if line).strip()
