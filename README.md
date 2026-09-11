# Knowledge Agent Service

Version 0.1.5 adds idempotent PDF document and page-chunk ingestion with provenance, lifecycle-aware search, vectors, and Neo4j document relationships.

Independent multi-layer memory backend for the Knowledge Agent VS Code extension.

## Layers

1. Evidence remains immutable at its source and is referenced by ID.
2. SQLite stores scoped `raw`, `candidate`, and active `memory` records with FTS5 and audit events.
3. A separate SQLite vector store persists embeddings and provides semantic recall.
4. Neo4j stores normalized knowledge entities and relationships through additive `MERGE` operations only.
5. The API combines lexical and vector recall while preserving evidence and lifecycle state.

## Document Graph

Imported PDFs do not stop at `Document`/`DocumentChunk` nodes. Two layers connect them to the knowledge graph:

1. **Deterministic entity linking (always on).** `entity_linker.py` matches chunk text against the alias catalogue of active `KnowledgeEntity` nodes — label first, then explicit `aliases`, then `tags`. Matching is NFKC-normalized and casefolded, enforces word boundaries for ASCII aliases, allows substring matches for CJK aliases, gives the longest surface form priority, and resolves one alias pointing at several entities deterministically. It needs no model, so the result is reproducible and cheap enough to run on every import. Every match becomes a `MENTIONS` relationship:

   ```cypher
   (:DocumentChunk)-[:MENTIONS {alias, source, occurrences, confidence}]->(:KnowledgeEntity)
   (:Document)-[:MENTIONS {alias, chunks, occurrences, confidence, evidence_chunk_ids}]->(:KnowledgeEntity)
   ```

   Without this layer an imported document is an isolated subgraph. Two documents mentioning the same entity are related as soon as both carry `MENTIONS` edges to it.

2. **Semantic extraction (caller supplied).** The service never calls a model. The VS Code extension reads document excerpts with the active chat model and posts the resulting entities, aliases, and relationships through the existing snapshot sync path, which reaches Neo4j through the same additive `upsert`.

Because linking depends on the catalogue, `POST /v1/graph/relink` rebuilds `MENTIONS` edges for already stored chunks after the catalogue grows — no re-import needed. `POST /v1/graph/document-links` returns cross-document pairs derived from shared entities, and `/v1/search` appends the highest-scoring pairs to the `graph` array so retrieval sees them without a client change. Each pair carries the shared entity IDs and the source chunk evidence on both sides. The `document-links` response always carries `links` and `count`, so a skipped or failed lookup is shaped exactly like an empty result.

### Link Quality And Safety

`include_tags` defaults to `false`. Tags are topical labels attached to sessions and decisions — `hardware`, `implementation`, `git` — not entity names. On a four-document corpus of 2,869 chunks the difference was measured, not assumed:

| Setting | Aliases | Chunk mentions | Distinct entities |
| --- | --- | --- | --- |
| `include_tags=true` | 3,531 | 31,107 | 470 |
| `include_tags=false` (default) | 1,852 | 4,764 | 69 |

The tag-derived links were overwhelmingly generic words such as `board`, `register`, `status`, and `platform` that match nearly every datasheet page, and they added almost no distinct entities. File- and symbol-like surfaces (`.github/agents/yocto.agent.md`, `tasks.json`) are rejected for the same reason.

`max_alias_document_frequency` is available but off by default. Setting it to `0.25` drops aliases present in more than a quarter of chunks, which cuts mentions roughly in half by removing ubiquitous tokens such as `Intel` — useful when the goal is ranking document pairs by distinctiveness rather than collecting chunk-level evidence.

`POST /v1/graph/relink` defaults to `dry_run: true` because Neo4j writes here are additive and the adapter exposes no delete operation. A dry run reports exactly what would be written, including `planned_links`, a preview of the cross-document pairs the run would create. Pass `dry_run: false` to persist.

A committed relink is still reversible by hand, since every generated relationship is namespaced:

```cypher
MATCH ()-[r:MENTIONS]->() WHERE r.id STARTS WITH 'mention:' DELETE r
```

`POST /v1/import/continuity` accepts Continuity decisions plus optional embeddings and imports them additively. Repeating an import updates the same decision IDs without deleting existing data. The health response reports the number of records imported from Continuity.

The default data directory is `~/.local/share/knowledge-agent-service`. The service never depends on an OpenBMC checkout.

## Run

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/knowledge-agent-service serve
```

The API listens on `127.0.0.1:8765` by default. Override it with `KNOWLEDGE_AGENT_HOST` and `KNOWLEDGE_AGENT_PORT`.

## Start At Boot

Install and immediately start the user-level systemd service:

```bash
./scripts/install-user-service.sh
```

The installer keeps Python dependencies in this project's `.runtime` directory, writes the unit to `~/.config/systemd/user/knowledge-agent-service.service`, and enables it for the user manager. With systemd user lingering enabled, it starts during machine boot without an interactive login.

Runtime settings are read from `~/.config/knowledge-agent-service/environment`. The file is created with mode `0600`; Neo4j remains disabled unless its settings are explicitly added. Starting the service never imports data or synchronizes Neo4j.

Useful operations:

```bash
systemctl --user status knowledge-agent-service
systemctl --user restart knowledge-agent-service
journalctl --user -u knowledge-agent-service -f
systemctl --user disable --now knowledge-agent-service
```

## Neo4j

Set `KNOWLEDGE_NEO4J_URI`, `KNOWLEDGE_NEO4J_USER`, `KNOWLEDGE_NEO4J_PASSWORD`, and optionally `KNOWLEDGE_NEO4J_DATABASE`. Credentials are read only at runtime and must not be committed.

Neo4j writes are additive. The adapter contains no database-clearing operation and represents source relationship vocabulary as a `RELATED` relationship with a `type` property.

## Non-Destructive Migration

Inspect an existing extension snapshot without writing:

```bash
knowledge-agent-service migrate-extension /path/to/knowledge-agent.json
```

Apply an incremental import into the service's own databases:

```bash
knowledge-agent-service migrate-extension /path/to/knowledge-agent.json --apply
```

Add `--sync-neo4j` only when Neo4j credentials are configured. The migration verifies that the source file hash is unchanged and never deletes source or target records.