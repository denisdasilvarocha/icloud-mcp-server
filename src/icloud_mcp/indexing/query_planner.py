"""Search query planning."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from icloud_mcp.util import normalize_text, tokenize


@dataclass(frozen=True)
class QueryPlan:
    """Compact query plan."""

    raw: str
    normalized: str
    tokens: list[str]
    intent: str
    domains: list[str]
    start: str | None = None
    end: str | None = None
    people: list[str] | None = None


def plan_query(query: str, *, now: datetime | None = None) -> QueryPlan:
    """Infer a simple deterministic query plan."""

    tokens = tokenize(query)
    token_set = set(tokens)
    intent = _intent(token_set)
    start, end = _relative_window(token_set, now or datetime.now(tz=UTC))
    domains = _domains_for_intent(intent, token_set)
    people = _people_from_query(query)
    return QueryPlan(
        raw=query,
        normalized=normalize_text(" ".join(tokens)),
        tokens=tokens,
        intent=intent,
        domains=domains,
        start=start,
        end=end,
        people=people,
    )


def _intent(tokens: set[str]) -> str:
    if "meetings" in tokens:
        tokens = {*tokens, "meeting"}
    if "events" in tokens:
        tokens = {*tokens, "event"}
    if {"who", "contact", "phone", "email"} & tokens and not ({"message", "mail", "from"} & tokens):
        return "person_lookup"
    if {"meeting", "event", "appointment", "calendar"} & tokens and {"time", "when", "tomorrow", "next"} & tokens:
        return "calendar_time_lookup"
    if {"meeting", "event", "appointment", "calendar"} & tokens:
        return "event_listing"
    if {"mail", "email", "message", "from", "sender"} & tokens:
        return "mail_search"
    return "general_search"


def _domains_for_intent(intent: str, tokens: set[str]) -> list[str]:
    if intent in {"calendar_time_lookup", "event_listing"}:
        return ["calendar"]
    if intent == "mail_search":
        return ["mail", "mail_invite"]
    if intent == "person_lookup":
        return ["contact"]
    if "invite" in tokens:
        return ["calendar", "mail", "mail_invite"]
    return ["mail", "calendar", "contact", "mail_invite"]


def _relative_window(tokens: set[str], now: datetime) -> tuple[str | None, str | None]:
    today = now.astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    if "tomorrow" in tokens:
        start = today + timedelta(days=1)
        return start.isoformat(), (start + timedelta(days=1)).isoformat()
    if "today" in tokens:
        return today.isoformat(), (today + timedelta(days=1)).isoformat()
    if "yesterday" in tokens:
        start = today - timedelta(days=1)
        return start.isoformat(), today.isoformat()
    if "week" in tokens and "last" in tokens:
        start = today - timedelta(days=today.weekday() + 7)
        return start.isoformat(), (start + timedelta(days=7)).isoformat()
    if "week" in tokens and "next" in tokens:
        start = today + timedelta(days=7 - today.weekday())
        return start.isoformat(), (start + timedelta(days=7)).isoformat()
    if "next" in tokens:
        return now.isoformat(), (now + timedelta(days=31)).isoformat()
    return None, None


def _people_from_query(query: str) -> list[str]:
    markers = (" with ", " from ", " by ", " for ")
    delimiters = {"about", "regarding", "re", "on"}
    lowered = f" {query} "
    people: list[str] = []
    for marker in markers:
        index = lowered.casefold().find(marker)
        if index == -1:
            continue
        phrase = lowered[index + len(marker) :].strip(" ?.,!")
        words = []
        for word in phrase.split():
            cleaned = word.strip(" ?.,!;:")
            if cleaned.casefold() in delimiters:
                break
            if cleaned[:1].isalpha():
                words.append(cleaned)
        if words:
            people.append(" ".join(words[:2]))
    return list(dict.fromkeys(people))
