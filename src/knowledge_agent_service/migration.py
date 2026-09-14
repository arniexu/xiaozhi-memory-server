from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

from . import legacy_db_guard
from .config import Settings
from .graph_store import Neo4jGraphStore
from .memory_store import MEMORY_TYPES, MemoryStore, utc_now
from .vector_store import VectorStore


CONTINUITY_STATUS_MAP = {
    "active": ("active", "memory"),
    "draft": ("draft", "candidate"),
    "superseded": ("superseded", "memory"),
    "outdated": ("deprecated", "memory"),
}


def _continuity_id(decision_id: str) -> str:
    return f"continuity:{decision_id}"


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def inspect_snapshot(path: Path) -> dict[str, int]:
    snapshot = json.loads(path.read_text(encoding="utf-8"))
    return {key: len(snapshot.get(key, [])) for key in ("sessions", "turns", "vectors", "nodes", "edges")}


def migrate_extension_data(snapshot: dict[str, Any], settings: Settings, *, sync_neo4j: bool = False) -> dict[str, Any]:
    memory_store = MemoryStore(settings.memory_db, settings.event_log)
    vector_store = VectorStore(settings.vector_db)
    turns = {turn.get("id"): turn for turn in snapshot.get("turns", [])}
    sessions = {session.get("id"): session for session in snapshot.get("sessions", [])}
    imported_memories = 0
    for node in snapshot.get("nodes", []):
        evidence_ids = [item for item in node.get("evidenceTurnIds", []) if item in turns]
        session_ids = {turns[item].get("sessionId", "") for item in evidence_ids}
        session_id = next(iter(session_ids)) if len(session_ids) == 1 else ""
        session = sessions.get(session_id, {})
        memory_store.upsert({
            "id": node["id"],
            "type": node.get("kind", "concept"),
            "summary": node.get("label", "Knowledge"),
            "resolution": node.get("summary", ""),
            "evidence": [{"kind": "turn", "id": item} for item in evidence_ids],
            "context": {"imported_from": "knowledge-agent-extension", "confidence": node.get("confidence", "medium")},
            "tags": node.get("tags", []),
            "status": node.get("status", "active"),
            "memory_kind": "memory",
            "source_refs": [f"turn:{item}" for item in evidence_ids],
            "session_id": session_id,
            "workspace_id": session.get("workspaceId", ""),
            "repo_id": "",
            "created_at": node.get("createdAt", ""),
            "updated_at": node.get("updatedAt", ""),
        })
        imported_memories += 1
    for vector in snapshot.get("vectors", []):
        vector_store.upsert(
            vector["sourceId"], vector.get("sourceKind", "knowledge"), vector["values"],
            {"imported_from": "knowledge-agent-extension", "vector_id": vector.get("id", "")},
            vector.get("createdAt", ""),
        )
    report: dict[str, Any] = {
        "imported_memories": imported_memories,
        "imported_vectors": len(snapshot.get("vectors", [])),
    }
    if sync_neo4j:
        report["neo4j"] = Neo4jGraphStore(settings).upsert(snapshot.get("nodes", []), snapshot.get("edges", []))
    report["memory_stats"] = memory_store.stats()
    report["vector_stats"] = vector_store.stats()
    return report


def migrate_continuity_data(
    decisions: list[dict[str, Any]],
    settings: Settings,
    *,
    source: str = "",
    workspace_id: str = "",
    repo_id: str = "",
    embeddings: list[dict[str, Any]] | None = None,
    sync_neo4j: bool = False,
) -> dict[str, Any]:
    memory_store = MemoryStore(settings.memory_db, settings.event_log)
    vector_store = VectorStore(settings.vector_db)
    embedding_by_id = {
        str(item.get("decisionId", "")): item.get("values", [])
        for item in embeddings or []
        if isinstance(item, dict)
    }
    created = 0
    updated = 0
    skipped = 0
    imported_vectors = 0
    graph_nodes: list[dict[str, Any]] = []
    graph_edges: list[dict[str, Any]] = []

    for decision in decisions:
        decision_id = str(decision.get("id", "")).strip()
        question = str(decision.get("question", "")).strip()
        if not decision_id or not question:
            skipped += 1
            continue
        answer = str(decision.get("answer", "")).strip()
        source_status = str(decision.get("status", "active")).strip().lower()
        status, memory_kind = CONTINUITY_STATUS_MAP.get(source_status, ("deprecated", "memory"))
        memory_id = _continuity_id(decision_id)
        timestamp = str(decision.get("timestamp", ""))
        history = decision.get("history", [])
        updated_at = timestamp
        if isinstance(history, list):
            updated_at = max(
                (str(item.get("timestamp", "")) for item in history if isinstance(item, dict)),
                default=timestamp,
            )
        result = memory_store.upsert({
            "id": memory_id,
            "type": "decision",
            "summary": question,
            "resolution": answer,
            "evidence": [{"kind": "continuity-decision", "id": decision_id}],
            "context": {
                "imported_from": "continuity",
                "source": source,
                "priority": decision.get("priority", "medium"),
                "source_status": source_status,
            },
            "tags": decision.get("tags", []),
            "status": status,
            "memory_kind": memory_kind,
            "source_refs": [f"continuity:{decision_id}", *([source] if source else [])],
            "session_id": "",
            "workspace_id": workspace_id,
            "repo_id": repo_id,
            "created_at": timestamp,
            "updated_at": updated_at,
        })
        if result["created"]:
            created += 1
        else:
            updated += 1

        values = embedding_by_id.get(decision_id)
        if isinstance(values, list) and values:
            vector_store.upsert(
                memory_id,
                "knowledge",
                values,
                {"imported_from": "continuity", "decision_id": decision_id},
                updated_at,
            )
            imported_vectors += 1

        priority = str(decision.get("priority", "medium")).lower()
        confidence = priority if priority in {"low", "medium", "high"} else "medium"
        graph_nodes.append({
            "id": memory_id,
            "kind": "decision",
            "label": question,
            "summary": answer,
            "status": status,
            "confidence": confidence,
            "tags": decision.get("tags", []),
            "createdAt": timestamp,
            "updatedAt": updated_at,
        })
        relationships = decision.get("relationships", {})
        if isinstance(relationships, dict):
            for relation, targets in relationships.items():
                if not isinstance(targets, list):
                    continue
                for target in targets:
                    target_id = str(target).strip()
                    if target_id:
                        graph_edges.append({
                            "id": f"continuity:{decision_id}:{relation}:{target_id}",
                            "from": memory_id,
                            "to": _continuity_id(target_id),
                            "type": relation,
                            "confidence": confidence,
                            "evidenceTurnIds": [f"continuity:{decision_id}"],
                            "createdAt": timestamp,
                            "updatedAt": updated_at,
                        })

    report: dict[str, Any] = {
        "source": source,
        "received": len(decisions),
        "created": created,
        "updated": updated,
        "skipped": skipped,
        "imported_vectors": imported_vectors,
        "memory_stats": memory_store.stats(),
        "vector_stats": vector_store.stats(),
    }
    if sync_neo4j:
        report["neo4j"] = Neo4jGraphStore(settings).upsert(graph_nodes, graph_edges)
    return report


def migrate_document_data(
    documents: list[dict[str, Any]],
    settings: Settings,
    *,
    workspace_id: str = "",
    repo_id: str = "",
    embeddings: list[dict[str, Any]] | None = None,
    sync_neo4j: bool = False,
) -> dict[str, Any]:
    memory_store = MemoryStore(settings.memory_db, settings.event_log)
    vector_store = VectorStore(settings.vector_db)
    embedding_by_id = {
        str(item.get("chunkId", "")): item.get("values", [])
        for item in embeddings or []
        if isinstance(item, dict)
    }
    created = 0
    updated = 0
    skipped = 0
    imported_vectors = 0
    accepted_documents: list[dict[str, Any]] = []
    imported_at = utc_now()

    for document in documents:
        document_id = str(document.get("id", "")).strip()
        filename = str(document.get("filename", "")).strip()
        chunks = document.get("chunks", [])
        if not document_id or not filename or not isinstance(chunks, list):
            skipped += 1
            continue
        source_path = str(document.get("sourcePath", "")).strip()
        source_status = str(document.get("status", "active")).strip().lower()
        status = "draft" if source_status == "draft" else "active"
        memory_kind = "candidate" if status == "draft" else "memory"
        valid_chunks = []
        for chunk in chunks:
            if not isinstance(chunk, dict):
                skipped += 1
                continue
            chunk_id = str(chunk.get("id", "")).strip()
            text = str(chunk.get("text", "")).strip()
            page = int(chunk.get("page", 0) or 0)
            if not chunk_id or not text or page < 1:
                skipped += 1
                continue
            result = memory_store.upsert({
                "id": chunk_id,
                "type": "fact",
                "summary": f"{filename} - page {page}",
                "resolution": text,
                "evidence": [{"kind": "document-page", "id": f"{document_id}#page={page}"}],
                "context": {
                    "imported_from": "document",
                    "document_id": document_id,
                    "filename": filename,
                    "page": page,
                    "content_hash": chunk.get("contentHash", ""),
                    "source_path": source_path,
                },
                "tags": ["document", "pdf", status],
                "status": status,
                "memory_kind": memory_kind,
                "source_refs": [f"document:{filename}#page={page}"],
                "workspace_id": workspace_id,
                "repo_id": repo_id,
                "updated_at": imported_at,
            })
            if result["created"]:
                created += 1
            else:
                updated += 1
            values = embedding_by_id.get(chunk_id)
            if isinstance(values, list) and values:
                vector_store.upsert(
                    chunk_id,
                    "candidate" if status == "draft" else "knowledge",
                    values,
                    {"imported_from": "document", "document_id": document_id, "page": page},
                    imported_at,
                )
                imported_vectors += 1
            valid_chunks.append(chunk)
        accepted_documents.append({**document, "status": status, "chunks": valid_chunks})

    report: dict[str, Any] = {
        "received_documents": len(documents),
        "imported_documents": len(accepted_documents),
        "created": created,
        "updated": updated,
        "skipped": skipped,
        "imported_vectors": imported_vectors,
        "memory_stats": memory_store.stats(),
        "vector_stats": vector_store.stats(),
    }
    if sync_neo4j:
        report["neo4j"] = Neo4jGraphStore(settings).upsert_documents(accepted_documents)
    return report


def migrate_extension_snapshot(path: Path, settings: Settings, *, apply: bool = False, sync_neo4j: bool = False) -> dict[str, Any]:
    source_hash = file_sha256(path)
    snapshot = json.loads(path.read_text(encoding="utf-8"))
    report: dict[str, Any] = {"mode": "apply" if apply else "dry-run", "source": str(path), "source_sha256": source_hash, **inspect_snapshot(path)}
    if apply:
        report.update(migrate_extension_data(snapshot, settings, sync_neo4j=sync_neo4j))
    report["source_unchanged"] = file_sha256(path) == source_hash
    return report


LEGACY_IMPORT_SOURCE = "legacy-memory-agent"
LEGACY_ID_PREFIX = "legacy:"
LEGACY_SESSION_CONTEXT_PREFIX = "legacy-session-context:"
LEGACY_SESSION_CONTEXT_SUMMARY = "Legacy session context"


class _SkipLegacyRecord(Exception):
    """Raised internally to skip a single malformed legacy source record."""


def _legacy_lifecycle(source_status: str, source_memory_kind: str) -> tuple[str, str]:
    kind = source_memory_kind.strip().lower()
    status = source_status.strip().lower()
    if kind == "raw":
        return "draft", "raw"
    if status == "rejected":
        return "rejected", "memory"
    return "draft", "candidate"


def _legacy_session_context_id(session_id: str, workspace_id: str, repo_id: str) -> str:
    payload = json.dumps([session_id, workspace_id, repo_id], ensure_ascii=False, sort_keys=True)
    return LEGACY_SESSION_CONTEXT_PREFIX + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _legacy_parse_json(text: str, field: str, expected: type) -> Any:
    try:
        value = json.loads(text)
    except (ValueError, TypeError):
        raise _SkipLegacyRecord(f"invalid JSON in {field}") from None
    if not isinstance(value, expected):
        raise _SkipLegacyRecord(f"{field} must be a JSON {expected.__name__}")
    return value


def _legacy_parse_storage_targets(text: str, field: str) -> Any:
    """Parse legacy ``storage_targets`` JSON.

    The canonical legacy shape is a JSON object; a JSON array is accepted for
    backward compatibility. Any other scalar or ``null`` is rejected so the
    record is skipped rather than mis-imported.
    """
    try:
        value = json.loads(text)
    except (ValueError, TypeError):
        raise _SkipLegacyRecord(f"invalid JSON in {field}") from None
    if not isinstance(value, (dict, list)):
        raise _SkipLegacyRecord(f"{field} must be a JSON object or array")
    return value


def _legacy_distribution_bump(distribution: dict[str, int], value: str) -> None:
    key = value if value else "(empty)"
    distribution[key] = distribution.get(key, 0) + 1


def _validate_apply_staging(source: Path, settings: Settings) -> None:
    """Reject anything that is not a guard-staged, read-only snapshot.

    This is the importer-side mirror of the guard's own staging checks. It
    exists so that calling ``--apply`` directly (bypassing the guard) cannot
    write to the production data directory or to a mutable database file.
    """
    data_dir = settings.data_dir.resolve()
    if source.name != legacy_db_guard.SNAPSHOT_FILENAME:
        raise legacy_db_guard.GuardError(
            f"apply requires the guard staging snapshot {legacy_db_guard.SNAPSHOT_FILENAME!r}; got {source.name!r}"
        )
    if data_dir not in source.parents:
        raise legacy_db_guard.GuardError("apply source must be located inside settings.data_dir")
    if source.stat().st_mode & 0o222:
        raise legacy_db_guard.GuardError("apply source must be read-only (no write bits)")
    production = legacy_db_guard.DEFAULT_PRODUCTION_DIR.resolve()
    if data_dir == production or production in data_dir.parents or data_dir in production.parents:
        raise legacy_db_guard.GuardError(
            "apply settings.data_dir must not be the production data directory or its parent/child"
        )


def _prepare_legacy_memory_unit(row: sqlite3.Row) -> dict[str, Any]:
    source_id = str(row["id"] or "").strip()
    if not source_id:
        raise _SkipLegacyRecord("memory unit has empty id")
    summary = str(row["summary"] or "").strip()
    if not summary:
        raise _SkipLegacyRecord("memory unit has empty summary")

    source_type = str(row["type"] or "").strip()
    source_status = str(row["status"] or "").strip()
    source_memory_kind = str(row["memory_kind"] or "").strip()

    evidence = _legacy_parse_json(row["evidence_json"], "evidence", list)
    context = _legacy_parse_json(row["context_json"], "context", dict)
    tags = _legacy_parse_json(row["tags_json"], "tags", list)
    source_refs = _legacy_parse_json(row["source_refs_json"], "source_refs", list)
    storage_targets = _legacy_parse_storage_targets(row["storage_targets_json"], "storage_targets")

    memory_type = source_type if source_type in MEMORY_TYPES else "lesson"
    status, memory_kind = _legacy_lifecycle(source_status, source_memory_kind)

    provenance: dict[str, Any] = {
        "imported_from": LEGACY_IMPORT_SOURCE,
        "source_id": source_id,
        "source_status": source_status,
        "source_memory_kind": source_memory_kind,
        "loading_policy": str(row["loading_policy"] or "").strip(),
        "storage_targets": storage_targets,
        "last_accessed_at": str(row["last_accessed_at"] or ""),
    }
    if memory_type != source_type:
        provenance["source_type"] = source_type

    return {
        "id": f"{LEGACY_ID_PREFIX}{source_id}",
        "type": memory_type,
        "summary": summary,
        "resolution": str(row["resolution"] or "").strip(),
        "evidence": evidence,
        "context": {**context, **provenance},
        "tags": [str(tag) for tag in tags],
        "status": status,
        "memory_kind": memory_kind,
        "source_refs": [str(ref) for ref in source_refs],
        "session_id": str(row["session_id"] or ""),
        "workspace_id": str(row["workspace_id"] or ""),
        "repo_id": str(row["repo_id"] or ""),
        "created_at": str(row["created_at"] or ""),
        "updated_at": str(row["updated_at"] or ""),
    }


def _prepare_legacy_session_context(row: sqlite3.Row) -> dict[str, Any]:
    session_id = str(row["session_id"] or "").strip()
    workspace_id = str(row["workspace_id"] or "").strip()
    repo_id = str(row["repo_id"] or "").strip()
    if not session_id:
        raise _SkipLegacyRecord("session context has empty session_id")

    context = _legacy_parse_json(row["context_json"], "context", dict)
    evidence_refs = _legacy_parse_json(row["evidence_refs_json"], "evidence_refs", list)
    source_refs = _legacy_parse_json(row["source_refs_json"], "source_refs", list)

    evidence: list[dict[str, Any]] = []
    for ref in evidence_refs:
        if isinstance(ref, dict):
            evidence.append(ref)
        else:
            evidence.append({"kind": "session-evidence", "id": str(ref)})

    return {
        "id": _legacy_session_context_id(session_id, workspace_id, repo_id),
        "type": "session",
        "summary": LEGACY_SESSION_CONTEXT_SUMMARY,
        "resolution": json.dumps(context, ensure_ascii=False, sort_keys=True),
        "evidence": evidence,
        "context": {"imported_from": LEGACY_IMPORT_SOURCE, "source_kind": "session_context"},
        "tags": [],
        "status": "draft",
        "memory_kind": "candidate",
        "source_refs": [str(ref) for ref in source_refs],
        "session_id": session_id,
        "workspace_id": workspace_id,
        "repo_id": repo_id,
        "created_at": str(row["created_at"] or ""),
        "updated_at": str(row["updated_at"] or ""),
    }


def migrate_legacy_memory_database(path: Path, settings: Settings, *, apply: bool = False) -> dict[str, Any]:
    """Import a legacy Memory Agent SQLite database.

    The source is always opened through the guard's read-only capabilities
    (``mode=ro&immutable=1`` + ``PRAGMA query_only=ON``) and validated against
    the expected legacy schema and integrity checks. Dry-run never creates
    ``settings.data_dir`` and never writes. Apply only accepts a guard-staged
    read-only ``legacy-source.sqlite3`` snapshot inside ``settings.data_dir``.
    """
    inspect_report = legacy_db_guard.inspect_database(path)
    source = Path(inspect_report["source"])
    source_sha256 = inspect_report["sha256"]

    if apply:
        _validate_apply_staging(source, settings)

    connection = legacy_db_guard._open_readonly(source)
    connection.row_factory = sqlite3.Row
    try:
        memory_rows = connection.execute(
            f"SELECT id, type, summary, resolution, evidence_json, context_json, tags_json, "
            f"status, memory_kind, source_refs_json, storage_targets_json, loading_policy, "
            f"session_id, workspace_id, repo_id, created_at, updated_at, last_accessed_at "
            f"FROM {legacy_db_guard.MEMORY_UNITS_TABLE}"
        ).fetchall()
        session_rows = connection.execute(
            f"SELECT session_id, workspace_id, repo_id, context_json, evidence_refs_json, "
            f"source_refs_json, created_at, updated_at FROM {legacy_db_guard.SESSION_CONTEXTS_TABLE}"
        ).fetchall()
    finally:
        connection.close()

    source_distributions = {"status": {}, "memory_kind": {}, "type": {}, "loading_policy": {}}
    prepared_memory: list[dict[str, Any]] = []
    skipped = 0
    errors = 0
    for row in memory_rows:
        _legacy_distribution_bump(source_distributions["status"], str(row["status"] or ""))
        _legacy_distribution_bump(source_distributions["memory_kind"], str(row["memory_kind"] or ""))
        _legacy_distribution_bump(source_distributions["type"], str(row["type"] or ""))
        _legacy_distribution_bump(source_distributions["loading_policy"], str(row["loading_policy"] or ""))
        try:
            prepared_memory.append(_prepare_legacy_memory_unit(row))
        except _SkipLegacyRecord:
            skipped += 1
            errors += 1

    prepared_sessions: list[dict[str, Any]] = []
    for row in session_rows:
        try:
            prepared_sessions.append(_prepare_legacy_session_context(row))
        except _SkipLegacyRecord:
            skipped += 1
            errors += 1

    planned = {"status": {}, "memory_kind": {}}
    for unit in [*prepared_memory, *prepared_sessions]:
        _legacy_distribution_bump(planned["status"], unit["status"])
        _legacy_distribution_bump(planned["memory_kind"], unit["memory_kind"])

    created = 0
    updated = 0
    if apply:
        memory_store = MemoryStore(settings.memory_db, settings.event_log)
        for unit in [*prepared_memory, *prepared_sessions]:
            try:
                result = memory_store.upsert(unit)
            except ValueError:
                skipped += 1
                errors += 1
                continue
            if result["created"]:
                created += 1
            else:
                updated += 1

    after = legacy_db_guard.file_sha256(source)
    if after != source_sha256:
        raise legacy_db_guard.GuardError(
            f"source changed during legacy migration: sha256 {source_sha256} -> {after}"
        )

    return {
        "mode": "apply" if apply else "dry-run",
        "source": str(source),
        "source_sha256": source_sha256,
        "source_unchanged": True,
        "memory_units": len(memory_rows),
        "session_contexts": len(session_rows),
        "source_distributions": source_distributions,
        "planned_target_distributions": planned,
        "created": created,
        "updated": updated,
        "skipped": skipped,
        "errors": errors,
    }