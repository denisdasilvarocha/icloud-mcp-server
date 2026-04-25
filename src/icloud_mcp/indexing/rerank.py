"""Reranking helpers."""

from __future__ import annotations


def reciprocal_rank_score(rank: int, k: int = 60) -> float:
    """Compute reciprocal-rank contribution."""

    return 1.0 / (k + rank)
