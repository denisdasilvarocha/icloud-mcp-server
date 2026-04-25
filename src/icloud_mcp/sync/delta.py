"""Shared WebDAV delta-first sync helpers."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Generic, TypeVar

from icloud_mcp.db.connection import Database

CollectionT = TypeVar("CollectionT")
ItemT = TypeVar("ItemT")


@dataclass(frozen=True)
class DeltaSyncResult(Generic[CollectionT, ItemT]):
    """Result of a delta-first collection sync."""

    collections: list[CollectionT]
    items: list[ItemT]
    full_sync_collection_ids: set[str]
    deleted_hrefs: list[str]


def sync_delta_first(
    *,
    db: Database,
    collections: Sequence[CollectionT],
    existing_sql: str,
    sync_changes: Callable[[CollectionT, str], tuple[Any, list[ItemT]]],
    full_sync_items: Callable[[], Sequence[ItemT]],
    item_collection_id: Callable[[ItemT], str],
    deleted_href: Callable[[CollectionT, str], str],
    collection_with_sync_token: Callable[[CollectionT, str], CollectionT],
) -> DeltaSyncResult:
    """Use collection tokens first, falling back to full sync only when needed."""

    synced_collections: list[CollectionT] = []
    items: list[ItemT] = []
    deleted_hrefs: list[str] = []
    full_sync_collection_ids: set[str] = set()
    fallback_needed = False

    for collection in collections:
        existing = db.query_one(existing_sql, (collection.url,))
        if existing and existing.get("sync_token") and collection.sync_token:
            try:
                result, changed = sync_changes(collection, existing["sync_token"])
            except Exception:
                synced_collections.append(collection)
                fallback_needed = True
                full_sync_collection_ids.add(collection.id)
                continue
            items.extend(changed)
            deleted_hrefs.extend([deleted_href(collection, href) for href in result.deleted])
            if not result.sync_token:
                fallback_needed = True
                full_sync_collection_ids.add(collection.id)
                synced_collections.append(collection)
                continue
            synced_collections.append(collection_with_sync_token(collection, result.sync_token))
            continue
        synced_collections.append(collection)
        if not existing or not collection.ctag or existing.get("ctag") != collection.ctag:
            fallback_needed = True
            full_sync_collection_ids.add(collection.id)

    if fallback_needed:
        items.extend(item for item in full_sync_items() if item_collection_id(item) in full_sync_collection_ids)

    return DeltaSyncResult(
        collections=list(synced_collections),
        items=list(items),
        full_sync_collection_ids=full_sync_collection_ids,
        deleted_hrefs=deleted_hrefs,
    )
