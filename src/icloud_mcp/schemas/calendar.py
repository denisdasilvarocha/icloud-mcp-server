"""Calendar schemas."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class CreateEventInput(BaseModel):
    """Calendar create_event input."""

    title: str = Field(min_length=1, max_length=200)
    start: str
    end: str
    timezone: str
    calendar_id: str | None = None
    location: str | None = None
    description: str | None = None
    attendees: list[dict[str, str]] = Field(default_factory=list)
    recurrence: dict[str, Any] | None = None
    alarms: list[dict[str, Any]] = Field(default_factory=list)
    request_id: str | None = None


class UpdateEventInput(BaseModel):
    """Calendar update_event input."""

    event_id: str
    patch: dict[str, Any]
    etag: str | None = None
    scope: str = "series"


class CalendarWriteResult(BaseModel):
    """Calendar write result."""

    status: str
    event_id: str
    etag: str | None
