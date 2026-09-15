from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .scope import hash_aliases, workspace_aliases


MEMORY_TYPES = {"session", "decision", "lesson", "issue", "workflow", "fact", "concept", "person", "project", "tool"}
MEMORY_STATUSES = {"draft", "active", "contested", "superseded", "deprecated", "rejected"}
MEMORY_KINDS = {"raw", "candidate", "memory"}

# Only rows matching this predicate are ever returned by default retrieval.
SEARCHABLE_SQL = "m.status='active' AND m.memory_kind='memory' AND m.evidence_json<>'[]'"

# ASCII 词元必须排除 CJK：否则 \w 会把整段中文当成一个“ASCII 词”，
# 使 AND 门只匹配到逐字引用该查询的散文，把手册页整批排除在候选池之外。
_ASCII_TERM = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_./:-]*")
_CJK_CLASS = "\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uac00-\ud7af"
_CJK = re.compile(f"[{_CJK_CLASS}]")
_CJK_RUN = re.compile(f"[{_CJK_CLASS}]+")
_DOCUMENT = re.compile(r"^(?P<doc>.+?\.pdf)(?: - page (?P<page>\d+))?$")

# 融合权重：ASCII 词面命中比逐字短语更具体，文档页证据比"谈论该词的记忆"更可引用。
CHANNEL_WEIGHTS = {"all": 1.0, "any": 0.45, "cjk": 0.55}
PAGE_EVIDENCE_BOOST = 2  # 单位为 rank 步长（1/61）
SESSION_MATCH_BOOST = 0.06
# 作用域是排序边界而不是硬分区：本地命中优先，但不再把全局证据整批挡在候选池外。
SCOPE_MATCH_BOOST = 0.5


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def ascii_terms(query: str) -> list[str]:
    return _ASCII_TERM.findall(query or "")


def _quote(term: str) -> str:
    return '"' + term.replace('"', '""') + '"'


def fts_any(terms: list[str]) -> str:
    return " OR ".join(_quote(term) for term in terms)


def fts_all(terms: list[str]) -> str:
    return " AND ".join(_quote(term) for term in terms)


def cjk_spaced(text: str) -> str:
    """Space out CJK characters so unicode61 indexes them one by one.

    SQLite's unicode61 tokenizer treats a whole CJK run (including any ASCII
    glued to it) as a single token, so ``阈值`` inside a sentence is unindexed
    and ``MATCH "阈值"`` returns nothing. Indexing the spaced form and querying
    an ordered phrase restores substring recall for any CJK length.
    """
    return re.sub(r"\s+", " ", _CJK.sub(lambda match: f" {match.group(0)} ", text or "")).strip()


def cjk_phrase(query: str) -> str:
    phrases = ['"' + " ".join(run) + '"' for run in _CJK_RUN.findall(query or "")]
    return " AND ".join(phrases)


def document_key(summary: str, fallback: str) -> str:
    match = _DOCUMENT.match((summary or "").strip())
    return match.group("doc") if match else fallback


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
                CREATE VIRTUAL TABLE IF NOT EXISTS memory_units_fts_cjk USING fts5(
                    memory_id UNINDEXED, text
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

    @staticmethod
    def _index_text(summary: str, resolution: str, tags: list[str], source_refs: list[str]) -> str:
        return cjk_spaced(" ".join([summary, resolution, " ".join(tags), " ".join(source_refs)]))

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
        tags = json.loads(record["tags_json"])
        source_refs = json.loads(record["source_refs_json"])
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
                (record["id"], summary, resolution, " ".join(tags), " ".join(source_refs)),
            )
            connection.execute("DELETE FROM memory_units_fts_cjk WHERE memory_id=?", (record["id"],))
            connection.execute(
                "INSERT INTO memory_units_fts_cjk(memory_id,text) VALUES(?,?)",
                (record["id"], self._index_text(summary, resolution, tags, source_refs)),
            )
            connection.execute(
                "INSERT INTO memory_events(event_type,memory_id,detail_json,created_at) VALUES(?,?,?,?)",
                ("upsert", record["id"], _json({"created": existing is None, "status": status, "memory_kind": memory_kind}), now),
            )
        self._append_event({"type": "upsert", "memory_id": record["id"], "created": existing is None, "timestamp": now})
        return {"id": record["id"], "created": existing is None}

    def search(self, query: str, **kwargs: Any) -> list[dict[str, Any]]:
        """Backward-compatible wrapper; see ``search_with_meta`` for the scope report."""
        return self.search_with_meta(query, **kwargs)["results"]

    def search_with_meta(
        self,
        query: str,
        *,
        workspace_id: str = "",
        workspace_ids: list[str] | None = None,
        repo_id: str = "",
        session_id: str = "",
        session_scope: str = "prefer",
        limit: int = 10,
        scope_fallback: str = "global",
        per_document_limit: int = 2,
        page_boost: float | None = None,
    ) -> dict[str, Any]:
        aliases = workspace_aliases(workspace_id, *(workspace_ids or []))
        # 每个通道先取更深的候选池，否则排在第 9 位的高分文档页永远进不了合并结果。
        candidate_limit = max(limit * 4, 32)
        scoped, match_mode = self._search_once(
            query,
            aliases=aliases,
            repo_id=repo_id,
            session_id=session_id,
            session_scope=session_scope,
            limit=candidate_limit,
            page_boost=page_boost,
        )
        meta = {
            "scope_match": "exact" if aliases else "global",
            "scope_suppressed": 0,
            "match_mode": match_mode,
            "session_scope": session_scope,
        }
        merged = list(scoped)
        if aliases and scope_fallback == "global":
            wider, _ = self._search_once(
                query,
                aliases=[],
                repo_id=repo_id,
                session_id=session_id,
                session_scope=session_scope,
                limit=candidate_limit,
                page_boost=page_boost,
            )
            scoped_ids = {item["id"] for item in scoped}
            unit = 1.0 / 61.0
            for item in scoped:
                item["score"] = round(item.get("score", 0.0) + SCOPE_MATCH_BOOST * unit, 6)
            extra = [item for item in wider if item["id"] not in scoped_ids]
            # 两次检索的 RRF 分数同尺度，合并后必须重新排序；否则全局证据永远排在本地结果之后。
            merged = sorted(scoped + extra, key=lambda item: (-item.get("score", 0.0), item["id"]))
            if extra and scoped:
                meta["scope_match"] = "mixed"
            elif extra:
                meta["scope_match"] = "relaxed"
            meta["scope_suppressed"] = len(extra)
        meta["results"] = self._apply_document_quota(merged, limit, per_document_limit)
        return meta

    def _search_once(
        self,
        query: str,
        *,
        aliases: list[str],
        repo_id: str,
        session_id: str,
        session_scope: str,
        limit: int,
        page_boost: float | None = None,
    ) -> tuple[list[dict[str, Any]], str]:
        channels: list[tuple[list[dict[str, Any]], float]] = []
        labels: list[str] = []
        terms = ascii_terms(query)
        phrase = cjk_phrase(query)
        if terms:
            # AND 与 OR 并联而不是二选一：AND 命中不再吞掉 OR 的召回。
            all_rows = self._match(
                "memory_units_fts", fts_all(terms), aliases=aliases, repo_id=repo_id,
                session_id=session_id, session_scope=session_scope, limit=limit,
            )
            if all_rows:
                channels.append((all_rows, CHANNEL_WEIGHTS["all"]))
                labels.append("all")
            any_rows = self._match(
                "memory_units_fts", fts_any(terms), aliases=aliases, repo_id=repo_id,
                session_id=session_id, session_scope=session_scope, limit=limit,
            )
            if any_rows:
                channels.append((any_rows, CHANNEL_WEIGHTS["any"]))
                labels.append("any")
            if not channels and not phrase:
                return [], "none"
        if phrase:
            cjk_rows = self._match(
                "memory_units_fts_cjk", phrase, aliases=aliases, repo_id=repo_id,
                session_id=session_id, session_scope=session_scope, limit=limit,
            )
            if cjk_rows:
                channels.append((cjk_rows, CHANNEL_WEIGHTS["cjk"]))
                labels.append("cjk")
            elif not terms:
                # 纯中文查询：CJK 是唯一的门，未命中就是无结果（保住无关查询 0 召回）。
                return [], "none"
        if not channels:
            return [], "none"
        match_mode = "+".join(labels)
        scores: dict[str, float] = {}
        items: dict[str, dict[str, Any]] = {}
        for rows, weight in channels:
            for rank, item in enumerate(rows):
                key = item["id"]
                items.setdefault(key, item)
                scores[key] = scores.get(key, 0.0) + weight / (60 + rank + 1)
        unit = 1.0 / 61.0
        boost = PAGE_EVIDENCE_BOOST if page_boost is None else float(page_boost)
        for key, item in items.items():
            bonus = 0.0
            if boost and _DOCUMENT.match((item.get("summary") or "").strip()):
                bonus += boost
            if session_id and session_scope == "prefer" and item.get("session_id") == session_id:
                bonus += SESSION_MATCH_BOOST
            if bonus:
                scores[key] = scores.get(key, 0.0) + bonus * unit
        ordered = sorted(items.values(), key=lambda item: (-scores[item["id"]], item["id"]))
        for item in ordered:
            item["score"] = round(scores[item["id"]], 6)
        return ordered, match_mode

    def _match(
        self,
        table: str,
        match: str,
        *,
        aliases: list[str],
        repo_id: str,
        session_id: str,
        session_scope: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        clauses = [f"{table} MATCH ?", SEARCHABLE_SQL]
        params: list[Any] = [match]
        if aliases:
            placeholders = ",".join("?" for _ in aliases)
            fragments = [f"m.workspace_id IN ({placeholders})", "m.workspace_id=''"]
            params.extend(aliases)
            for alias in hash_aliases(aliases):
                fragments.append("m.workspace_id GLOB ?")
                params.append(f"{alias}-*")
            clauses.append("(" + " OR ".join(fragments) + ")")
        if repo_id:
            clauses.append("(m.repo_id=? OR m.repo_id='')")
            params.append(repo_id)
        # prefer: 不加任何会话约束，只在融合阶段给同会话结果加权；
        # strict: 恢复旧的硬隔离（空 session 只匹配无会话记录）。
        if session_scope == "strict":
            if session_id:
                clauses.append("m.session_id=?")
                params.append(session_id)
            else:
                clauses.append("m.session_id=''")
        params.append(max(1, min(limit, 50)))
        sql = (
            f"SELECT m.*, bm25({table}) AS bm25 FROM {table} "
            f"JOIN memory_units m ON m.id={table}.memory_id "
            f"WHERE {' AND '.join(clauses)} ORDER BY bm25, m.updated_at DESC LIMIT ?"
        )
        with self._connect() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [self._row(row) for row in rows]

    @staticmethod
    def _apply_document_quota(rows: list[dict[str, Any]], limit: int, per_document_limit: int) -> list[dict[str, Any]]:
        """Cap how many chunks of one document occupy the head of the result list."""
        if per_document_limit is None or per_document_limit <= 0:
            return rows[:limit]
        counts: dict[str, int] = {}
        kept: list[dict[str, Any]] = []
        deferred: list[dict[str, Any]] = []
        for item in rows:
            key = document_key(item.get("summary", ""), item["id"])
            counts[key] = counts.get(key, 0) + 1
            if counts[key] <= per_document_limit:
                kept.append(item)
            else:
                deferred.append(item)
        return (kept + deferred)[:limit]

    def get_many(self, memory_ids: list[str], *, searchable_only: bool = False) -> list[dict[str, Any]]:
        """Fetch records by id. ``searchable_only`` is the retrieval-path filter, not a storage one."""
        if not memory_ids:
            return []
        placeholders = ",".join("?" for _ in memory_ids)
        sql = f"SELECT * FROM memory_units WHERE id IN ({placeholders})"
        if searchable_only:
            sql += " AND status='active' AND memory_kind='memory' AND evidence_json<>'[]'"
        with self._connect() as connection:
            rows = connection.execute(sql, memory_ids).fetchall()
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

    def backfill_cjk_index(self, *, batch_size: int = 500, dry_run: bool = False) -> dict[str, int | bool]:
        """Rebuild the CJK character index from stored records (idempotent)."""
        with self._connect() as connection:
            total = connection.execute("SELECT count(*) FROM memory_units").fetchone()[0]
            indexed = connection.execute("SELECT count(*) FROM memory_units_fts_cjk").fetchone()[0]
            if dry_run:
                return {"total": total, "indexed": indexed, "planned": total - indexed, "dry_run": True}
            rows = connection.execute(
                "SELECT id, summary, resolution, tags_json, source_refs_json FROM memory_units"
            ).fetchall()
            connection.execute("DELETE FROM memory_units_fts_cjk")
            written = 0
            for start in range(0, len(rows), batch_size):
                batch = rows[start : start + batch_size]
                connection.executemany(
                    "INSERT INTO memory_units_fts_cjk(memory_id,text) VALUES(?,?)",
                    [
                        (
                            row["id"],
                            self._index_text(
                                row["summary"],
                                row["resolution"],
                                json.loads(row["tags_json"]),
                                json.loads(row["source_refs_json"]),
                            ),
                        )
                        for row in batch
                    ],
                )
                connection.commit()
                written += len(batch)
        return {"total": total, "indexed": written, "dry_run": False}

    def stats(self) -> dict[str, int]:
        with self._connect() as connection:
            total = connection.execute("SELECT count(*) FROM memory_units").fetchone()[0]
            active = connection.execute("SELECT count(*) FROM memory_units WHERE status='active' AND memory_kind='memory'").fetchone()[0]
            searchable = connection.execute(
                "SELECT count(*) FROM memory_units WHERE status='active' AND memory_kind='memory' AND evidence_json<>'[]'"
            ).fetchone()[0]
            candidates = connection.execute("SELECT count(*) FROM memory_units WHERE memory_kind='candidate'").fetchone()[0]
            raw = connection.execute("SELECT count(*) FROM memory_units WHERE memory_kind='raw'").fetchone()[0]
            continuity = connection.execute(
                "SELECT count(*) FROM memory_units WHERE context_json LIKE '%\"imported_from\": \"continuity\"%'"
            ).fetchone()[0]
        return {"total": total, "active": active, "searchable": searchable, "candidate": candidates, "raw": raw, "continuity": continuity}

    def database_info(self) -> dict[str, Any]:
        return {
            "engine": "SQLite",
            "path": str(self.db_path),
            "size_bytes": self.db_path.stat().st_size if self.db_path.exists() else 0,
            "features": ["FTS5", "WAL", "CJK-char-index"],
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
