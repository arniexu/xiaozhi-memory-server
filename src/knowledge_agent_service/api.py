from __future__ import annotations

from fastapi import FastAPI
from pydantic import BaseModel, Field

from .config import load_settings
from .graph_store import Neo4jGraphStore
from .memory_store import MemoryStore
from .migration import migrate_continuity_data, migrate_document_data, migrate_extension_data
from .vector_store import VectorStore


settings = load_settings()
memories = MemoryStore(settings.memory_db, settings.event_log)
vectors = VectorStore(settings.vector_db)
graph = Neo4jGraphStore(settings)
app = FastAPI(title="Knowledge Agent Service", version="0.1.5")


class SearchRequest(BaseModel):
    query: str = Field(min_length=1)
    embedding: list[float] = []
    workspace_id: str = ""
    repo_id: str = ""
    session_id: str = ""
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
        "memory": memories.stats(),
        "vectors": vectors.stats(),
        "databases": {"memory": memories.database_info(), "vectors": vectors.database_info()},
        "neo4j": graph.health(),
    }


@app.get("/v1/stats")
def stats() -> dict:
    return health()


@app.post("/v1/search")
def search(request: SearchRequest) -> dict:
    lexical = memories.search(
        request.query,
        workspace_id=request.workspace_id,
        repo_id=request.repo_id,
        session_id=request.session_id,
        limit=request.limit,
    )
    semantic = vectors.search(request.embedding, limit=request.limit, source_kind="knowledge") if request.embedding else []
    semantic_ids = [item["source_id"] for item in semantic]
    graph_results = []
    graph_error = ""
    if settings.neo4j_configured:
        try:
            graph_results = graph.search(request.query, limit=request.limit)
            graph_results.extend(graph.document_links(limit=max(1, min(request.limit, 6))))
        except Exception as error:
            graph_error = type(error).__name__
    return {
        "lexical": lexical,
        "semantic": semantic,
        "semantic_memories": memories.get_many(semantic_ids),
        "graph": graph_results,
        "graph_error": graph_error,
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
    exposes no delete operation. Pass ``dry_run: false`` to persist the links.
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
        return {"skipped": True, "reason": "neo4j-not-configured", "links": []}
    try:
        links = graph.document_links(request.document_id)
    except Exception as error:
        return {"error": type(error).__name__, "links": []}
    return {"links": links, "count": len(links)}