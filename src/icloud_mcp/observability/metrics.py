"""Minimal metrics value objects."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TimingMetric:
    """Simple timing metric shape for future middleware."""

    name: str
    duration_ms: float
