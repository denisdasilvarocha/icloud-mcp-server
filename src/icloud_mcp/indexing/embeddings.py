"""Embedding worker for local hashed search status."""

from __future__ import annotations

from dataclasses import dataclass

from icloud_mcp.db.connection import Database
from icloud_mcp.sync.checkpoints import update_checkpoint


@dataclass
class EmbeddingWorker:
    """Marks pending chunks ready for local hashed vector search."""

    db: Database

    name = "embedding_worker"
    model = "local-hashed-bow-v1"

    def run_once(self) -> dict:
        """Mark pending chunks as embedded by the local deterministic model."""

        rows = self.db.query("SELECT id FROM search_chunks WHERE embedding_status = 'pending'")
        for row in rows:
            self.db.execute(
                """
                UPDATE search_chunks
                SET embedding_model = ?, embedding_status = 'ready'
                WHERE id = ?
                """,
                (self.model, row["id"]),
            )
        result = {"status": "ok", "embedded_chunks": len(rows), "model": self.model}
        update_checkpoint(self.db, self.name, "ok", result)
        return result
