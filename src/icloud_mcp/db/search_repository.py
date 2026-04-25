"""Search and index repository interface."""

from __future__ import annotations

from icloud_mcp.db.query_cache import query_cache_get as query_cache_get
from icloud_mcp.db.query_cache import query_cache_set as query_cache_set
from icloud_mcp.db.repositories import cleanup_local_index as cleanup_local_index
from icloud_mcp.db.repositories import person_alias_terms as person_alias_terms
from icloud_mcp.db.repositories import search_documents as search_documents
from icloud_mcp.db.repositories import upsert_search_document as upsert_search_document
from icloud_mcp.db.search_index import SearchIndexQuery as SearchIndexQuery
from icloud_mcp.db.search_index import search_index as search_index

__all__ = [
    "cleanup_local_index",
    "person_alias_terms",
    "query_cache_get",
    "query_cache_set",
    "SearchIndexQuery",
    "search_documents",
    "search_index",
    "upsert_search_document",
]
