"""Cache maintenance repository interface."""

from __future__ import annotations

from icloud_mcp.db.cache_state import bump_index_generation
from icloud_mcp.db.connection import Database
from icloud_mcp.indexing.vector_backend import delete_document_vectors
from icloud_mcp.util import utc_now


def cleanup_local_index(db: Database) -> dict[str, int]:
    """Remove stale local search/index rows left by prior duplicate syncs."""

    now = utc_now()
    tombstoned_mail = db.execute(
        """
        UPDATE mail_messages
        SET deleted_at = ?
        WHERE deleted_at IS NULL
          AND mailbox_id NOT IN (SELECT id FROM mailboxes)
        """,
        (now,),
    ).rowcount
    tombstoned_contacts = db.execute(
        """
        UPDATE contacts
        SET deleted_at = ?
        WHERE deleted_at IS NULL
          AND addressbook_id NOT IN (SELECT id FROM addressbooks)
        """,
        (now,),
    ).rowcount
    tombstoned_calendar = db.execute(
        """
        UPDATE calendar_objects
        SET deleted_at = ?
        WHERE deleted_at IS NULL
          AND calendar_id NOT IN (SELECT id FROM calendar_collections)
        """,
        (now,),
    ).rowcount
    stale_documents = db.query(
        """
        SELECT d.id
        FROM search_documents d
        WHERE d.deleted_at IS NOT NULL
           OR (d.domain = 'mail' AND NOT EXISTS (
                SELECT 1 FROM mail_messages m WHERE m.id = d.object_id AND m.deleted_at IS NULL
              ))
           OR (d.domain = 'mail_invite' AND NOT EXISTS (
                SELECT 1 FROM mail_messages m WHERE m.id = d.object_id AND m.deleted_at IS NULL
              ))
           OR (d.domain = 'contact' AND NOT EXISTS (
                SELECT 1 FROM contacts c WHERE c.id = d.object_id AND c.deleted_at IS NULL
              ))
           OR (d.domain = 'calendar' AND NOT EXISTS (
                SELECT 1 FROM calendar_objects o WHERE o.id = d.object_id AND o.deleted_at IS NULL
              ))
        """
    )
    document_ids = [row["id"] for row in stale_documents]
    for document_id in document_ids:
        delete_document_vectors(db, document_id)

    removed_chunks = 0
    removed_fts = 0
    removed_documents = 0
    if document_ids:
        placeholders = ",".join("?" for _ in document_ids)
        removed_fts += db.execute(
            f"DELETE FROM search_fts WHERE document_id IN ({placeholders})",
            tuple(document_ids),
        ).rowcount
        removed_chunks += db.execute(
            f"DELETE FROM search_chunks WHERE document_id IN ({placeholders})",
            tuple(document_ids),
        ).rowcount
        removed_documents += db.execute(
            f"DELETE FROM search_documents WHERE id IN ({placeholders})",
            tuple(document_ids),
        ).rowcount

    removed_fts += db.execute(
        """
        DELETE FROM search_fts
        WHERE document_id NOT IN (SELECT id FROM search_documents)
        """
    ).rowcount
    removed_chunks += db.execute(
        """
        DELETE FROM search_chunks
        WHERE document_id NOT IN (SELECT id FROM search_documents)
        """
    ).rowcount
    removed_embeddings = db.execute(
        """
        DELETE FROM search_embeddings
        WHERE chunk_id NOT IN (SELECT id FROM search_chunks)
        """
    ).rowcount
    removed_aliases = db.execute(
        """
        DELETE FROM person_aliases
        WHERE contact_id NOT IN (SELECT id FROM contacts WHERE deleted_at IS NULL)
        """
    ).rowcount
    removed_contact_fts = db.execute(
        """
        DELETE FROM contact_trigram_fts
        WHERE contact_id NOT IN (SELECT id FROM contacts WHERE deleted_at IS NULL)
        """
    ).rowcount
    removed_occurrences = db.execute(
        """
        DELETE FROM calendar_occurrences
        WHERE event_id NOT IN (SELECT id FROM calendar_objects WHERE deleted_at IS NULL)
        """
    ).rowcount

    if any(
        value > 0
        for value in [
            removed_documents,
            removed_chunks,
            removed_fts,
            removed_embeddings,
            removed_aliases,
            removed_contact_fts,
            removed_occurrences,
            tombstoned_mail,
            tombstoned_contacts,
            tombstoned_calendar,
        ]
    ):
        bump_index_generation(db)

    return {
        "tombstoned_mail": max(0, tombstoned_mail),
        "tombstoned_contacts": max(0, tombstoned_contacts),
        "tombstoned_calendar": max(0, tombstoned_calendar),
        "removed_documents": max(0, removed_documents),
        "removed_chunks": max(0, removed_chunks),
        "removed_fts": max(0, removed_fts),
        "removed_embeddings": max(0, removed_embeddings),
        "removed_aliases": max(0, removed_aliases),
        "removed_contact_fts": max(0, removed_contact_fts),
        "removed_occurrences": max(0, removed_occurrences),
    }


MIN_SEMANTIC_SCORE = 0.2
MIN_SQLITE_VEC_SCORE = 0.1
