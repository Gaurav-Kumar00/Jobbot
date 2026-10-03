"""HTML -> readable plain text for job descriptions (stdlib only)."""

from __future__ import annotations

import html
import re
from html.parser import HTMLParser

_BLOCK_TAGS = frozenset(
    {"p", "div", "br", "li", "ul", "ol", "tr", "table", "section", "article", "h1", "h2", "h3",
     "h4", "h5", "h6", "blockquote", "pre", "hr"}
)  # fmt: skip
_SKIP_TAGS = frozenset({"script", "style", "noscript", "head", "title"})
_WS = re.compile(r"[ \t\r\f\v ]+")
_BLANK_LINES = re.compile(r"\n{3,}")


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
        elif tag == "li":
            self.parts.append("\n• ")
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self.parts.append(data)


def html_to_text(markup: str | None) -> str:
    """Convert (possibly entity-escaped) HTML into tidy text with line structure kept."""
    if not markup:
        return ""
    # Some ATSes (e.g. Greenhouse) return HTML that is itself HTML-escaped.
    if "&lt;" in markup and "<" not in markup:
        markup = html.unescape(markup)
    parser = _TextExtractor()
    parser.feed(markup)
    parser.close()
    lines = (_WS.sub(" ", line).strip() for line in "".join(parser.parts).split("\n"))
    text = "\n".join(lines)
    text = re.sub(r"•\s*\n+", "• ", text)  # bullets whose text sat in a nested <p>
    return _BLANK_LINES.sub("\n\n", text).strip()


def truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0]
    return cut + " …"
