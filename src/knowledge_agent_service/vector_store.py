from __future__ import annotations

import json
import math
import sqlite3
import struct
from pathlib import Path
from typing import Any


class VectorStore:
    """Persistent vector database with exact cosine search and stable source IDs."""

    def __init__(self, db_path: Path):
        self.db_path = db_path.expanduser().resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS vectors (
                    source_id TEXT PRIMARY KEY,
                    source_kind TEXT NOT NULL,
                    dimensions INTEGER NOT NULL,
                    embedding BLOB NOT NULL,
                    metadata_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_vectors_kind ON vectors(source_kind);
                """
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    @staticmethod
    def _pack(values: list[float]) -> bytes:
        return struct.pack(f"<{len(values)}f", *values)

    @staticmethod
    def _unpack(payload: bytes, dimensions: int) -> tuple[float, ...]:
        return struct.unpack(f"<{dimensions}f", payload)

    def upsert(self, source_id: str, source_kind: str, values: list[float], metadata: dict[str, Any], updated_at: str) -> None:
        if not source_id or not values or any(not math.isfinite(value) for value in values):
            raise ValueError("vector source ID and finite embedding values are required")
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO vectors(source_id,source_kind,dimensions,embedding,metadata_json,updated_at) VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(source_id) DO UPDATE SET source_kind=excluded.source_kind,dimensions=excluded.dimensions,embedding=excluded.embedding,metadata_json=excluded.metadata_json,updated_at=excluded.updated_at",
                (source_id, source_kind, len(values), self._pack(values), json.dumps(metadata, ensure_ascii=False, sort_keys=True), updated_at),
            )

    def search(self, query: list[float], *, limit: int = 10, source_kind: str = "") -> list[dict[str, Any]]:
        if not query:
            return []
        query_norm = math.sqrt(sum(value * value for value in query))
        if query_norm == 0:
            return []
        sql = "SELECT * FROM vectors" + (" WHERE source_kind=?" if source_kind else "")
        params = (source_kind,) if source_kind else ()
        results = []
        with self._connect() as connection:
            for row in connection.execute(sql, params):
                if row["dimensions"] != len(query):
                    continue
                values = self._unpack(row["embedding"], row["dimensions"])
                norm = math.sqrt(sum(value * value for value in values))
                score = 0.0 if norm == 0 else sum(left * right for left, right in zip(query, values)) / (query_norm * norm)
                if score > 0:
                    results.append({"source_id": row["source_id"], "source_kind": row["source_kind"], "score": score, "metadata": json.loads(row["metadata_json"])})
        return sorted(results, key=lambda item: item["score"], reverse=True)[: max(1, min(limit, 50))]

    def stats(self) -> dict[str, int]:
        with self._connect() as connection:
            total = connection.execute("SELECT count(*) FROM vectors").fetchone()[0]
            dimensions = connection.execute("SELECT COALESCE(max(dimensions),0) FROM vectors").fetchone()[0]
        return {"total": total, "dimensions": dimensions}

    def database_info(self) -> dict[str, Any]:
        return {
            "engine": "SQLite",
            "path": str(self.db_path),
            "size_bytes": self.db_path.stat().st_size if self.db_path.exists() else 0,
            "features": ["Exact cosine", "WAL"],
        }