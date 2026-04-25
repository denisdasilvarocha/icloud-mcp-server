"""Explicit sync adapter capability seams."""

from __future__ import annotations

from datetime import date
from typing import Any, Protocol, TypeGuard, runtime_checkable

from icloud_mcp.calendar.adapter import SyncedCalendar, SyncedCalendarEvent
from icloud_mcp.calendar.adapter import WebDAVSyncResult as CalendarSyncResult
from icloud_mcp.contacts.adapter import SyncedAddressBook, SyncedContact
from icloud_mcp.contacts.adapter import WebDAVSyncResult as ContactSyncResult
from icloud_mcp.mail.adapter import IMAPSyncDelta


@runtime_checkable
class IncrementalMailAdapter(Protocol):
    """Mail adapter that can sync by mailbox state deltas."""

    def sync_incremental(
        self,
        *,
        apple_id: str,
        app_password: str,
        mailbox_states: dict[str, dict[str, Any]],
        days: int,
        limit_per_mailbox: int,
    ) -> IMAPSyncDelta: ...


@runtime_checkable
class CalendarDeltaAdapter(Protocol):
    """Calendar adapter that can discover collections and sync WebDAV deltas."""

    def discover(self, *, apple_id: str, app_password: str) -> list[SyncedCalendar]: ...

    def sync_event_changes(
        self,
        *,
        apple_id: str,
        app_password: str,
        calendar_id: str,
        calendar_url: str,
        sync_token: str,
    ) -> tuple[CalendarSyncResult, list[SyncedCalendarEvent]]: ...

    def sync_events(
        self,
        *,
        apple_id: str,
        app_password: str,
        start: date,
        end: date,
    ) -> tuple[list[SyncedCalendar], list[SyncedCalendarEvent]]: ...


@runtime_checkable
class ContactsDeltaAdapter(Protocol):
    """Contacts adapter that can discover addressbooks and sync WebDAV deltas."""

    def discover_addressbooks(self, *, apple_id: str, app_password: str) -> list[SyncedAddressBook]: ...

    def sync_contact_changes(
        self,
        *,
        apple_id: str,
        app_password: str,
        addressbook: SyncedAddressBook,
        sync_token: str,
    ) -> tuple[ContactSyncResult, list[SyncedContact]]: ...

    def sync_contacts(self, *, apple_id: str, app_password: str) -> tuple[list[SyncedAddressBook], list[SyncedContact]]:
        ...


def supports_mail_incremental(adapter: object) -> TypeGuard[IncrementalMailAdapter]:
    """Return whether adapter provides incremental Mail sync."""

    return isinstance(adapter, IncrementalMailAdapter)


def supports_calendar_delta(adapter: object) -> TypeGuard[CalendarDeltaAdapter]:
    """Return whether adapter provides Calendar WebDAV delta sync."""

    return isinstance(adapter, CalendarDeltaAdapter)


def supports_contacts_delta(adapter: object) -> TypeGuard[ContactsDeltaAdapter]:
    """Return whether adapter provides Contacts WebDAV delta sync."""

    return isinstance(adapter, ContactsDeltaAdapter)
