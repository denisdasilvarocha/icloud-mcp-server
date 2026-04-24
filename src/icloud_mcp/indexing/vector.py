"""Small local vector search using deterministic hashed embeddings."""

from __future__ import annotations

import math
from collections import Counter
from hashlib import blake2b
from typing import Any

from icloud_mcp.util import tokenize

VECTOR_DIMENSIONS = 64
VECTOR_MODEL = "sqlite-vec-local-hashed-v1"

SYNONYMS = {
    "meeting": ["appointment", "sync", "catchup", "call"],
    "appointment": ["meeting", "event"],
    "deadline": ["due", "contract", "timeline"],
    "email": ["mail", "message"],
}


def expanded_tokens(text: str) -> list[str]:
    """Tokenize and add a few deterministic domain synonyms."""

    tokens = tokenize(text)
    expanded = list(tokens)
    for token in tokens:
        expanded.extend(SYNONYMS.get(token, []))
    return expanded


def cosine_score(query: str, document: str) -> float:
    """Compute cosine score over expanded token counters."""

    query_vector = Counter(expanded_tokens(query))
    document_vector = Counter(expanded_tokens(document))
    if not query_vector or not document_vector:
        return 0.0
    dot = sum(weight * document_vector.get(token, 0) for token, weight in query_vector.items())
    query_norm = math.sqrt(sum(weight * weight for weight in query_vector.values()))
    document_norm = math.sqrt(sum(weight * weight for weight in document_vector.values()))
    if query_norm == 0 or document_norm == 0:
        return 0.0
    return dot / (query_norm * document_norm)


def embedding_vector(text: str) -> dict[str, int]:
    """Return deterministic sparse embedding values for durable local storage."""

    return dict(Counter(expanded_tokens(text)))


def dense_embedding(text: str, dimensions: int = VECTOR_DIMENSIONS) -> list[float]:
    """Return fixed-width dense vector for sqlite-vec storage."""

    values = [0.0] * dimensions
    for token, count in Counter(expanded_tokens(text)).items():
        digest = blake2b(token.encode("utf-8"), digest_size=4).digest()
        bucket = int.from_bytes(digest[:2], "big") % dimensions
        sign = -1.0 if digest[2] % 2 else 1.0
        values[bucket] += sign * float(count)
    norm = math.sqrt(sum(value * value for value in values))
    if norm == 0:
        return values
    return [value / norm for value in values]


def cosine_score_vectors(query_vector: dict[str, Any], document_vector: dict[str, Any]) -> float:
    """Compute cosine score over stored sparse vectors."""

    if not query_vector or not document_vector:
        return 0.0
    dot = sum(float(weight) * float(document_vector.get(token, 0)) for token, weight in query_vector.items())
    query_norm = math.sqrt(sum(float(weight) * float(weight) for weight in query_vector.values()))
    document_norm = math.sqrt(sum(float(weight) * float(weight) for weight in document_vector.values()))
    if query_norm == 0 or document_norm == 0:
        return 0.0
    return dot / (query_norm * document_norm)
