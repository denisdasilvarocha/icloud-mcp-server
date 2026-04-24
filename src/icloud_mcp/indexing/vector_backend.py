"""SQLite-backed vector search using sqlite-vec when available."""

from __future__ import annotations

from contextlib import suppress

import sqlite_vec

from icloud_mcp.db.connection import Database
from icloud_mcp.indexing.vector import VECTOR_DIMENSIONS, dense_embedding

VEC_TABLE_SQL = f"""
CREATE VIRTUAL TABLE IF NOT EXISTS search_vec_embeddings
USING vec0(
  chunk_id TEXT PRIMARY KEY,
  embedding FLOAT[{VECTOR_DIMENSIONS}]
)
"""


def ensure_vector_backend(db: Database) -> bool:
    """Load sqlite-vec and ensure the vector table exists."""

    with db._lock:
        try:
            db.connection.enable_load_extension(True)
            sqlite_vec.load(db.connection)
            db.connection.execute(VEC_TABLE_SQL)
            db.connection.commit()
        except Exception:
            return False
        finally:
            with suppress(Exception):
                db.connection.enable_load_extension(False)
    return True


def upsert_chunk_vector(db: Database, chunk_id: str, text: str) -> bool:
    """Store one chunk embedding in sqlite-vec."""

    if not ensure_vector_backend(db):
        return False
    vector = sqlite_vec.serialize_float32(dense_embedding(text))
    with db._lock:
        db.connection.execute("DELETE FROM search_vec_embeddings WHERE chunk_id = ?", (chunk_id,))
        db.connection.execute(
            "INSERT INTO search_vec_embeddings (chunk_id, embedding) VALUES (?, ?)",
            (chunk_id, vector),
        )
        db.connection.commit()
    return True


def delete_document_vectors(db: Database, document_id: str) -> None:
    """Delete sqlite-vec rows for a document if backend is available."""

    if not ensure_vector_backend(db):
        return
    with db._lock:
        db.connection.execute(
            """
            DELETE FROM search_vec_embeddings
            WHERE chunk_id IN (SELECT id FROM search_chunks WHERE document_id = ?)
            """,
            (document_id,),
        )
        db.connection.commit()


def query_similar_chunks(db: Database, query: str, limit: int) -> list[dict]:
    """Return nearest chunk ids using sqlite-vec."""

    if not ensure_vector_backend(db):
        return []
    vector = sqlite_vec.serialize_float32(dense_embedding(query))
    with db._lock:
        rows = db.connection.execute(
            """
            SELECT chunk_id, distance
            FROM search_vec_embeddings
            WHERE embedding MATCH ? AND k = ?
            ORDER BY distance
            """,
            (vector, limit),
        ).fetchall()
    return [{"chunk_id": row["chunk_id"], "distance": row["distance"]} for row in rows]
