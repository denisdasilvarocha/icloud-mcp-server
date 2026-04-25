"""Text chunking."""

from __future__ import annotations


def chunk_text(text: str, max_chars: int = 4000) -> list[str]:
    """Split text into deterministic character chunks."""

    normalized = text.strip()
    if not normalized:
        return []
    return [normalized[index : index + max_chars] for index in range(0, len(normalized), max_chars)]
