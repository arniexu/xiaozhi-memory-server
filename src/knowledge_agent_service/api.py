from __future__ import annotations

import os
import subprocess
from pathlib import Path

from fastapi import FastAPI
from pydantic import BaseModel, Field

from .capabilities import SkillProvider
from .config import load_settings
from .graph_store import Neo4jGraphStore
from .memory_store import MemoryStore, utc_now
from .migration import migrate_continuity_data, migrate_document_data, migrate_extension_data
from .vector_store import VectorStore


SERVICE_VERSION = "0.1.7"


def _running_commit() -> str:
    override = os.getenv("KNOWLEDGE_AGENT_COMMIT", "").strip()
    if override:
        return override
    try:
        result = subprocess.run(
            ["git", "-C", str(Path(__file__).resolve().parents[2]), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=3,
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    except Exception:
        return ""


settings = load_settings()
memories = MemoryStore(settings.memory_db, settings.event_log)
vectors = VectorStore(settings.vector_db)
graph = Neo4jGraphStore(settings)
skills = SkillProvider(memories)
app = FastAPI(title="Knowledge Agent Service", version=SERVICE_VERSION)
STARTED_AT = utc_now()
COMMIT = _running_commit()


class SearchRequest(BaseModel):
    query: str = Field(min_length=1)
    embedding: list[float] = []
    workspace_id: str = ""
    workspace_ids: list[str] = []
    repo_id: str = ""
    session_id: str = ""
    session_scope: str = "prefer"
    scope_fallback: str = "global"
    min_similarity: float = 0.35
    include_retired: bool = False
    per_document_limit: int = 2
    page_boost: float | None = None
    limit: int = Field(default=8, ge=1, le=50)


class ContinuityImportRequest(BaseModel):
    decisions: list[dict] = []
    embeddings: list[dict] = []
    source: str = ""
    workspace_id: str = ""
    repo_id: str = ""


class DocumentImportRequest(BaseModel):
    documents: list[dict] = []
    embeddings: list[dict] = []
    workspace_id: str = ""
    repo_id: str = ""


class RelinkRequest(BaseModel):
    document_id: str = ""
    """Preview by default: this endpoint becomes permanent once dry_run is false."""
    dry_run: bool = True
    """Treat entity tags as names. Off by default because tags are topical labels."""
    include_tags: bool = False
    """Drop aliases appearing in more than this share of chunks; None disables the filter."""
    max_alias_document_frequency: float | None = None


@app.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "version": SERVICE_VERSION,
        "commit": COMMIT,
        "started_at": STARTED_AT,
        "memory": memories.stats(),
        "vectors": vectors.stats(),
        "databases": {"memory": memories.database_info(), "vectors": vectors.database_info()},
        "neo4j": graph.health(),
    }


@app.get("/v1/stats")
def stats() -> dict:
    return health()


@app.get("/v1/capabilities")
def capabilities() -> dict:
    """Report the code revision actually serving requests, so deployment drift is visible."""
    endpoints = sorted({getattr(route, "path", "") for route in app.routes if str(getattr(route, "path", "")).startswith("/v1/")})
    return {
        "version": SERVICE_VERSION,
        "commit": COMMIT,
        "started_at": STARTED_AT,
        "endpoints": endpoints,
        "search_fields": sorted(SearchRequest.model_fields),
    }


@app.get("/v1/capabilities/skills")
def list_skills(query: str = "", limit: int = 20) -> dict:
    """Read-only discovery of human-approved, evidence-backed skills."""
    items = skills.list_skills(query=query, limit=limit)
    return {"skills": items, "count": len(items)}


@app.get("/v1/capabilities/skills/{skill_id}")
def get_skill(skill_id: str) -> dict:
    """Read one skill by stable ID; ``found`` is false for unknown or ineligible IDs."""
    manifest = skills.get_skill(skill_id)
    if manifest is None:
        return {"found": False, "skill": None}
    return {"found": True, "skill": manifest}


@app.post("/v1/search")
def search(request: SearchRequest) -> dict:
    lexical = memories.search_with_meta(
        request.query,
        workspace_id=request.workspace_id,
        workspace_ids=request.workspace_ids,
        repo_id=request.repo_id,
        session_id=request.session_id,
        session_scope=request.session_scope,
        scope_fallback=request.scope_fallback,
        per_document_limit=request.per_document_limit,
        page_boost=request.page_boost,
        limit=request.limit,
    )
    semantic = (
        [
            item
            for item in vectors.search(request.embedding, limit=request.limit, source_kind="knowledge")
            if item.get("score", 0.0) >= request.min_similarity
        ]
        if request.embedding
        else []
    )
    semantic_ids = [item["source_id"] for item in semantic]
    semantic_memories = memories.get_many(semantic_ids, searchable_only=not request.include_retired)
    graph_results = []
    graph_error = ""
    if settings.neo4j_configured:
        try:
            graph_results = graph.search(request.query, limit=request.limit)
            # 只注入与本次查询命中实体相关的文档对；查询没命中实体就不注入，
            # 避免把全局 top-N 文档对当成“本次证据”塞给模型。
            matched_entities = [
                str(item["id"])
                for item in graph_results
                if item.get("type") == "graph:KnowledgeEntity" and item.get("id")
            ]
            graph_results.extend(
                graph.document_links(limit=max(1, min(request.limit, 6)), entity_ids=matched_entities)
            )
        except Exception as error:
            graph_error = type(error).__name__
    return {
        "lexical": lexical["results"],
        "semantic": semantic,
        "semantic_memories": semantic_memories,
        "semantic_suppressed": len(semantic_ids) - len(semantic_memories),
        "graph": graph_results,
        "graph_error": graph_error,
        "scope_match": lexical["scope_match"],
        "scope_suppressed": lexical["scope_suppressed"],
        "match_mode": lexical["match_mode"],
        "session_scope": lexical["session_scope"],
    }


@app.post("/v1/import/extension")
def import_extension(snapshot: dict) -> dict:
    return migrate_extension_data(snapshot, settings, sync_neo4j=settings.neo4j_configured)


@app.post("/v1/import/continuity")
def import_continuity(request: ContinuityImportRequest) -> dict:
    return migrate_continuity_data(
        request.decisions,
        settings,
        source=request.source,
        workspace_id=request.workspace_id,
        repo_id=request.repo_id,
        embeddings=request.embeddings,
        sync_neo4j=settings.neo4j_configured,
    )


@app.post("/v1/import/documents")
def import_documents(request: DocumentImportRequest) -> dict:
    return migrate_document_data(
        request.documents,
        settings,
        workspace_id=request.workspace_id,
        repo_id=request.repo_id,
        embeddings=request.embeddings,
        sync_neo4j=settings.neo4j_configured,
    )


@app.post("/v1/graph/relink")
def relink_documents(request: RelinkRequest) -> dict:
    """Rebuild deterministic document -> entity MENTIONS edges for stored chunks.

    Defaults to a dry run because Neo4j writes here are additive and the adapter
    exposes no delete operation. Pass ``dry_run: false`` to persist.
    """

    if not settings.neo4j_configured:
        return {"skipped": True, "reason": "neo4j-not-configured"}
    try:
        return graph.relink_documents(
            request.document_id,
            dry_run=request.dry_run,
            include_tags=request.include_tags,
            max_alias_document_frequency=request.max_alias_document_frequency,
        )
    except Exception as error:
        return {"error": type(error).__name__}


@app.post("/v1/graph/document-links")
def document_links(request: RelinkRequest) -> dict:
    """Cross-document relationships implied by shared knowledge entities."""

    if not settings.neo4j_configured:
        return {"skipped": True, "reason": "neo4j-not-configured", "links": [], "count": 0}
    try:
        links = graph.document_links(request.document_id)
    except Exception as error:
        return {"error": type(error).__name__, "links": [], "count": 0}
    return {"links": links, "count": len(links)}
