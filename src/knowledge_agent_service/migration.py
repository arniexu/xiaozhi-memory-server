from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .config import Settings
from .graph_store import Neo4jGraphStore
from .memory_store import MemoryStore, utc_now
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