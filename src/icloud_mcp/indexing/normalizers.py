"""Index text normalization."""

from __future__ import annotations

from bs4 import BeautifulSoup


def html_to_text(html: str) -> str:
    """Convert HTML mail bodies to plain text."""

    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    return " ".join(soup.get_text(" ").split())
