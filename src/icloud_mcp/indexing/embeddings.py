"""Embedding worker for local hashed search status."""

from __future__ import annotations

from dataclasses import dataclass

from icloud_mcp.db.connection import Database
from icloud_mcp.indexing.vector import VECTOR_DIMENSIONS, VECTOR_MODEL, embedding_vector
from icloud_mcp.indexing.vector_backend import ensure_vector_backend, upsert_chunk_vector
from icloud_mcp.sync.checkpoints import update_checkpoint
from icloud_mcp.util import compact_json, utc_now


@dataclass
class EmbeddingWorker:
    """Marks pending chunks ready for local hashed vector search."""

    db: Database

    name = "embedding_worker"
    model = VECTOR_MODEL

    def run_once(self) -> dict:
        """Mark pending chunks as embedded by the local deterministic model."""

        backend_available = ensure_vector_backend(self.db)
        rows = self.db.query("SELECT id, text FROM search_chunks WHERE embedding_status = 'pending'")
        for row in rows:
            if backend_available:
                upsert_chunk_vector(self.db, row["id"], row["text"])
            self.db.execute(
                """
                INSERT INTO search_embeddings (chunk_id, embedding_model, vector_json, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(chunk_id) DO UPDATE SET
                  embedding_model = excluded.embedding_model,
                  vector_json = excluded.vector_json,
                  updated_at = excluded.updated_at
                """,
                (row["id"], self.model, compact_json(embedding_vector(row["text"])), utc_now()),
            )
            self.db.execute(
                """
                UPDATE search_chunks
                SET embedding_model = ?, embedding_status = 'ready'
                WHERE id = ?
                """,
                (self.model, row["id"]),
            )
        self.db.execute(
            """
            INSERT INTO vector_backend_state (id, backend, dimensions, available, updated_at)
            VALUES (1, 'sqlite-vec', ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
              backend = excluded.backend,
              dimensions = excluded.dimensions,
              available = excluded.available,
              updated_at = excluded.updated_at
            """,
            (VECTOR_DIMENSIONS, 1 if backend_available else 0, utc_now()),
        )
        result = {
            "status": "ok",
            "embedded_chunks": len(rows),
            "model": self.model,
            "vector_backend": "sqlite-vec" if backend_available else "json-fallback",
        }
        update_checkpoint(self.db, self.name, "ok", result)
        return result
