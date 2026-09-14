from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


MEMORY_TYPES = {"session", "decision", "lesson", "issue", "workflow", "fact", "concept", "person", "project", "tool"}
MEMORY_STATUSES = {"draft", "active", "contested", "superseded", "deprecated", "rejected"}
MEMORY_KINDS = {"raw", "candidate", "memory"}


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _fts_query(query: str) -> str:
    terms = re.findall(r"[\w./:-]+", query, flags=re.UNICODE)
    return " OR ".join(f'"{term.replace(chr(34), chr(34) * 2)}"' for term in terms)


class MemoryStore:
    def __init__(self, db_path: Path, event_log_path: Path):
        self.db_path = db_path.expanduser().resolve()
        self.event_log_path = event_log_path.expanduser().resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS memory_units (
                    id TEXT PRIMARY KEY,
                    fingerprint TEXT NOT NULL UNIQUE,
                    type TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    resolution TEXT NOT NULL,
                    evidence_json TEXT NOT NULL,
                    context_json TEXT NOT NULL,
                    tags_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    memory_kind TEXT NOT NULL,
                    source_refs_json TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    workspace_id TEXT NOT NULL,
                    repo_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_memory_scope
                    ON memory_units(workspace_id, repo_id, session_id, status, memory_kind);
                CREATE VIRTUAL TABLE IF NOT EXISTS memory_units_fts USING fts5(
                    memory_id UNINDEXED, summary, resolution, tags, source_refs
                );
                CREATE TABLE IF NOT EXISTS memory_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT NOT NULL,
                    memory_id TEXT NOT NULL,
                    detail_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )

    def upsert(self, unit: dict[str, Any]) -> dict[str, Any]:
        memory_type = str(unit.get("type") or "lesson").strip()
        status = str(unit.get("status") or "draft").strip()
        memory_kind = str(unit.get("memory_kind") or "candidate").strip()
        if memory_type not in MEMORY_TYPES:
            raise ValueError(f"unsupported memory type: {memory_type}")
        if status not in MEMORY_STATUSES:
            raise ValueError(f"unsupported memory status: {status}")
        if memory_kind not in MEMORY_KINDS:
            raise ValueError(f"unsupported memory kind: {memory_kind}")
        summary = str(unit.get("summary") or "").strip()
        if not summary:
            raise ValueError("memory summary is required")
        resolution = str(unit.get("resolution") or "").strip()
        scope = {key: str(unit.get(key) or "").strip() for key in ("session_id", "workspace_id", "repo_id")}
        fingerprint_payload = {"type": memory_type, "summary": summary, "resolution": resolution, **scope}
        explicit_id = str(unit.get("id") or "").strip()
        if explicit_id:
            fingerprint_payload["id"] = explicit_id
        fingerprint = hashlib.sha256(_json(fingerprint_payload).encode()).hexdigest()
        memory_id = explicit_id or f"memory:{fingerprint[:24]}"
        now = utc_now()
        record = {
            "id": memory_id,
            "fingerprint": fingerprint,
            "type": memory_type,
            "summary": summary,
            "resolution": resolution,
            "evidence_json": _json(unit.get("evidence") if isinstance(unit.get("evidence"), list) else []),
            "context_json": _json(unit.get("context") if isinstance(unit.get("context"), dict) else {}),
            "tags_json": _json(unit.get("tags") if isinstance(unit.get("tags"), list) else []),
            "status": status,
            "memory_kind": memory_kind,
            "source_refs_json": _json(unit.get("source_refs") if isinstance(unit.get("source_refs"), list) else []),
            **scope,
            "created_at": str(unit.get("created_at") or now),
            "updated_at": str(unit.get("updated_at") or now),
        }
        with self._connect() as connection:
            existing = connection.execute(
                "SELECT id FROM memory_units WHERE id=?" if explicit_id else "SELECT id FROM memory_units WHERE fingerprint=?",
                (memory_id if explicit_id else fingerprint,),
            ).fetchone()
            if existing:
                record["id"] = existing["id"]
            columns = ",".join(record)
            values = ",".join(f":{key}" for key in record)
            updates = ",".join(f"{key}=excluded.{key}" for key in record if key not in {"id", "created_at"})
            connection.execute(
                f"INSERT INTO memory_units ({columns}) VALUES ({values}) ON CONFLICT(id) DO UPDATE SET {updates}",
                record,
            )
            connection.execute("DELETE FROM memory_units_fts WHERE memory_id=?", (record["id"],))
            connection.execute(
                "INSERT INTO memory_units_fts(memory_id,summary,resolution,tags,source_refs) VALUES(?,?,?,?,?)",
                (record["id"], summary, resolution, " ".join(json.loads(record["tags_json"])), " ".join(json.loads(record["source_refs_json"]))),
            )
            connection.execute(
                "INSERT INTO memory_events(event_type,memory_id,detail_json,created_at) VALUES(?,?,?,?)",
                ("upsert", record["id"], _json({"created": existing is None, "status": status, "memory_kind": memory_kind}), now),
            )
        self._append_event({"type": "upsert", "memory_id": record["id"], "created": existing is None, "timestamp": now})
        return {"id": record["id"], "created": existing is None}

    def search(self, query: str, *, workspace_id: str = "", repo_id: str = "", session_id: str = "", limit: int = 10) -> list[dict[str, Any]]:
        match = _fts_query(query)
        if not match:
            return []
        clauses = ["memory_units_fts MATCH ?", "m.status='active'", "m.memory_kind='memory'", "m.evidence_json<>'[]'"]
        params: list[Any] = [match]
        for column, value in (("workspace_id", workspace_id), ("repo_id", repo_id), ("session_id", session_id)):
            if value:
                clauses.append(f"(m.{column}=? OR m.{column}='')")
                params.append(value)
            elif column == "session_id":
                clauses.append("m.session_id='' ")
        params.append(max(1, min(limit, 50)))
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT m.*,bm25(memory_units_fts) score FROM memory_units_fts JOIN memory_units m ON m.id=memory_units_fts.memory_id WHERE {' AND '.join(clauses)} ORDER BY score,m.updated_at DESC LIMIT ?",
                params,
            ).fetchall()
        return [self._row(row) for row in rows]

    def get_many(self, memory_ids: list[str]) -> list[dict[str, Any]]:
        if not memory_ids:
            return []
        placeholders = ",".join("?" for _ in memory_ids)
        with self._connect() as connection:
            rows = connection.execute(f"SELECT * FROM memory_units WHERE id IN ({placeholders})", memory_ids).fetchall()
        by_id = {row["id"]: self._row(row) for row in rows}
        return [by_id[item] for item in memory_ids if item in by_id]

    def get_unit(self, memory_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM memory_units WHERE id=?", (memory_id,)).fetchone()
        return self._row(row) if row else None

    def list_skill_candidates(self, limit: int = 500) -> list[dict[str, Any]]:
        """Rows that could be skills by lifecycle; capability/evidence are checked by the caller."""
        bounded = max(1, min(int(limit), 1000))
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM memory_units WHERE type='workflow' AND status='active' AND memory_kind='memory' ORDER BY updated_at DESC LIMIT ?",
                (bounded,),
            ).fetchall()
        return [self._row(row) for row in rows]

    def stats(self) -> dict[str, int]:
        with self._connect() as connection:
            total = connection.execute("SELECT count(*) FROM memory_units").fetchone()[0]
            active = connection.execute("SELECT count(*) FROM memory_units WHERE status='active' AND memory_kind='memory'").fetchone()[0]
            candidates = connection.execute("SELECT count(*) FROM memory_units WHERE memory_kind='candidate'").fetchone()[0]
            raw = connection.execute("SELECT count(*) FROM memory_units WHERE memory_kind='raw'").fetchone()[0]
            continuity = connection.execute(
                "SELECT count(*) FROM memory_units WHERE context_json LIKE '%\"imported_from\": \"continuity\"%'"
            ).fetchone()[0]
        return {"total": total, "active": active, "candidate": candidates, "raw": raw, "continuity": continuity}

    def database_info(self) -> dict[str, Any]:
        return {
            "engine": "SQLite",
            "path": str(self.db_path),
            "size_bytes": self.db_path.stat().st_size if self.db_path.exists() else 0,
            "features": ["FTS5", "WAL"],
        }

    def _append_event(self, event: dict[str, Any]) -> None:
        self.event_log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.event_log_path.open("a", encoding="utf-8") as output:
            output.write(_json(event) + "\n")

    @staticmethod
    def _row(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        for source, target in (("evidence_json", "evidence"), ("context_json", "context"), ("tags_json", "tags"), ("source_refs_json", "source_refs")):
            result[target] = json.loads(result.pop(source))
        return result