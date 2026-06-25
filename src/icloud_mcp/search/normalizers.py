"""Index text normalization."""

from __future__ import annotations

from html.parser import HTMLParser


def html_to_text(html: str) -> str:
    """Convert HTML mail bodies to plain text."""

    parser = _TextParser()
    parser.feed(html)
    return " ".join(" ".join(parser.parts).split())


class _TextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._skip = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style"}:
            self._skip = True

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"}:
            self._skip = False

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)
