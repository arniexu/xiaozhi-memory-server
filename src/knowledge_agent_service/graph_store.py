from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from .config import Settings
from .entity_linker import Gazetteer, link_chunks
from .memory_store import utc_now


_CONFIDENCE_RANK = {"low": 0, "medium": 1, "high": 2}


class Neo4jGraphStore:
    """Additive Neo4j adapter. It deliberately exposes no delete or reset operation."""

    def __init__(self, settings: Settings):
        self.settings = settings

    def _driver(self):
        if not self.settings.neo4j_configured:
            raise RuntimeError("Neo4j is not configured")
        from neo4j import GraphDatabase

        return GraphDatabase.driver(
            self.settings.neo4j_uri,
            auth=(self.settings.neo4j_user, self.settings.neo4j_password),
        )

    def _endpoint(self) -> str:
        if not self.settings.neo4j_uri:
            return ""
        try:
            parsed = urlsplit(self.settings.neo4j_uri)
            hostname = parsed.hostname or ""
            if ":" in hostname:
                hostname = f"[{hostname}]"
            netloc = hostname
            if parsed.port:
                netloc += f":{parsed.port}"
            return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))
        except ValueError:
            return "configured"

    def health(self) -> dict[str, Any]:
        metadata = {
            "configured": self.settings.neo4j_configured,
            "ready": False,
            "endpoint": self._endpoint(),
            "database": self.settings.neo4j_database,
        }
        if not self.settings.neo4j_configured:
            return metadata
        try:
            with self._driver() as driver:
                driver.verify_connectivity()
                with driver.session(database=self.settings.neo4j_database) as session:
                    node_counts = session.run("MATCH (n) RETURN count(n) AS nodes").single()
                    relationship_counts = session.run("MATCH ()-[r]->() RETURN count(r) AS relationships").single()
            return {
                **metadata,
                "ready": True,
                "nodes": node_counts["nodes"] if node_counts else 0,
                "relationships": relationship_counts["relationships"] if relationship_counts else 0,
            }
        except Exception as error:
            return {**metadata, "error": type(error).__name__}

    @staticmethod
    def _safe_properties(properties: dict[str, Any]) -> dict[str, Any]:
        blocked = ("password", "secret", "token", "credential", "api_key")
        return {
            key: value
            for key, value in properties.items()
            if not any(item in key.lower() for item in blocked)
        }

    @staticmethod
    def _display(value: Any, limit: int = 240) -> str:
        if isinstance(value, (dict, list)):
            text = json.dumps(value, ensure_ascii=False, sort_keys=True)
        else:
            text = str(value)
        return text if len(text) <= limit else f"{text[: limit - 1]}…"

    @classmethod
    def _result(cls, row: dict[str, Any]) -> dict[str, Any]:
        labels = [str(label) for label in row.get("labels", [])]
        properties = cls._safe_properties(dict(row.get("properties") or {}))
        preferred_keys = ("label", "name", "title", "summary", "id")
        title_key = next((key for key in preferred_keys if properties.get(key)), "")
        if not title_key:
            title_key = next((key for key in properties if key.endswith("_id") and properties.get(key)), "")
        title = cls._display(properties.get(title_key)) if title_key else (labels[0] if labels else row.get("id", "Graph node"))
        matched = []
        for key in row.get("matched_keys", []):
            if key in properties:
                matched.append(f"{key}={cls._display(properties[key])}")
        connections = []
        connection_labels = []
        for connection in row.get("connections", []):
            if not connection:
                continue
            neighbor_properties = cls._safe_properties(dict(connection.get("neighbor_properties") or {}))
            neighbor_labels = connection.get("neighbor_labels") or []
            neighbor_key = next((key for key in preferred_keys if neighbor_properties.get(key)), "")
            neighbor = cls._display(neighbor_properties.get(neighbor_key)) if neighbor_key else "/".join(neighbor_labels)
            direction = str(connection.get("direction") or "outgoing")
            relation_type = str(connection.get("type") or "RELATED")
            arrow = "→" if direction == "outgoing" else "←"
            connection_labels.append(f"{relation_type} {arrow} {neighbor or 'node'}")
            connections.append({
                "edge_id": str(connection.get("edge_id") or ""),
                "type": relation_type,
                "direction": direction,
                "neighbor_id": str(neighbor_properties.get("id") or connection.get("neighbor_id") or ""),
                "neighbor_title": neighbor or "node",
                "neighbor_type": str(neighbor_properties.get("kind") or (neighbor_labels[0] if neighbor_labels else "node")),
                "confidence": str(connection.get("confidence") or ""),
                "evidence": [str(item) for item in (connection.get("evidence") or [])],
            })
        details = []
        if matched:
            details.append("Matched: " + "; ".join(matched))
        if connection_labels:
            details.append("Connections: " + "; ".join(connection_labels))
        stable_id = str(properties.get("id") or row.get("id", ""))
        return {
            "id": stable_id if properties.get("id") else f"neo4j:{stable_id}",
            "type": f"graph:{labels[0]}" if labels else "graph:node",
            "summary": title,
            "resolution": ". ".join(details) or "Neo4j graph match",
            "status": str(properties.get("status") or "active"),
            "source": "neo4j",
            "score": int(row.get("score") or 0),
            "connections": connections,
        }

    def search(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        if not self.settings.neo4j_configured:
            return []
        terms = list(dict.fromkeys(re.findall(r"[\w./:-]+", query.lower(), flags=re.UNICODE)))[:12]
        if not terms:
            return []
        cypher = """
            MATCH (n)
            WHERE coalesce(n.status, "active") = "active"
            WITH n, labels(n) AS node_labels,
                 [key IN keys(n) WHERE
                    NONE(blocked IN $blocked WHERE toLower(key) CONTAINS blocked) AND
                          ANY(term IN $terms WHERE toLower(coalesce(toStringOrNull(n[key]), "")) CONTAINS term)
                 ] AS matched_keys
            WHERE size(matched_keys) > 0
            OPTIONAL MATCH (n)-[r]-(neighbor)
            WITH n, node_labels, matched_keys,
                 collect(CASE WHEN r IS NULL THEN NULL ELSE {
                          edge_id: coalesce(r.id, elementId(r)),
                          type: coalesce(r.type, type(r)),
                          direction: CASE WHEN startNode(r) = n THEN "outgoing" ELSE "incoming" END,
                          neighbor_id: coalesce(neighbor.id, elementId(neighbor)),
                    neighbor_labels: labels(neighbor),
                          neighbor_properties: properties(neighbor),
                          confidence: coalesce(r.confidence, ""),
                          evidence: coalesce(r.evidence_turn_ids, [])
                 } END)[0..3] AS connections
            RETURN elementId(n) AS id, node_labels AS labels, properties(n) AS properties,
                   matched_keys, connections, size(matched_keys) AS score
            ORDER BY score DESC
            LIMIT $limit
        """
        with self._driver() as driver, driver.session(database=self.settings.neo4j_database) as session:
            rows = session.run(
                cypher,
                terms=terms,
                blocked=["password", "secret", "token", "credential", "api_key"],
                limit=max(1, min(limit, 50)),
            ).data()
        return [self._result(row) for row in rows]

    def upsert(self, nodes: list[dict[str, Any]], edges: list[dict[str, Any]]) -> dict[str, int]:
        node_records = [
            {
                "id": node["id"],
                "kind": node.get("kind", "concept"),
                "label": node.get("label", "Knowledge"),
                "summary": node.get("summary", ""),
                "status": node.get("status", "active"),
                "confidence": node.get("confidence", "medium"),
                "tags": node.get("tags", []),
                "aliases": node.get("aliases", []),
                "updated_at": node.get("updatedAt", node.get("createdAt", "")),
            }
            for node in nodes
        ]
        edge_records = [
            {
                "id": edge["id"],
                "from_id": edge["from"],
                "to_id": edge["to"],
                "type": edge["type"],
                "confidence": edge.get("confidence", "medium"),
                "evidence_turn_ids": edge.get("evidenceTurnIds", []),
                "updated_at": edge.get("updatedAt", edge.get("createdAt", "")),
            }
            for edge in edges
        ]
        with self._driver() as driver, driver.session(database=self.settings.neo4j_database) as session:
            session.run("CREATE CONSTRAINT knowledge_entity_id IF NOT EXISTS FOR (n:KnowledgeEntity) REQUIRE n.id IS UNIQUE")
            if node_records:
                session.run(
                    "UNWIND $nodes AS node "
                    "MERGE (n:KnowledgeEntity {id:node.id}) "
                    "SET n.kind=node.kind,n.label=node.label,n.summary=node.summary,n.status=node.status,"
                    "n.confidence=node.confidence,n.tags=node.tags,n.aliases=node.aliases,"
                    "n.updated_at=node.updated_at",
                    nodes=node_records,
                )
            written_edges = 0
            if edge_records:
                result = session.run(
                    "UNWIND $edges AS edge "
                    "MATCH (a:KnowledgeEntity {id:edge.from_id}),(b:KnowledgeEntity {id:edge.to_id}) "
                    "MERGE (a)-[r:RELATED {id:edge.id}]->(b) "
                    "SET r.type=edge.type,r.confidence=edge.confidence,r.evidence_turn_ids=edge.evidence_turn_ids,"
                    "r.updated_at=edge.updated_at RETURN count(r) AS count",
                    edges=edge_records,
                ).single()
                written_edges = result["count"] if result else 0
        return {"nodes": len(node_records), "edges": written_edges}

    def _entity_catalog(self) -> list[dict[str, Any]]:
        """Read-only alias catalogue of linkable entities (documents and chunks excluded)."""

        cypher = """
            MATCH (n:KnowledgeEntity)
            WHERE coalesce(n.status, "active") = "active"
              AND NONE(label IN labels(n) WHERE label IN ["Document", "DocumentChunk"])
            RETURN n.id AS id, n.label AS label, n.kind AS kind, n.tags AS tags, n.aliases AS aliases
        """
        with self._driver() as driver, driver.session(database=self.settings.neo4j_database) as session:
            return session.run(cypher).data()

    @staticmethod
    def _document_mentions(
        document: dict[str, Any],
        mentions: list[dict[str, Any]],
        updated_at: str,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Shape per-chunk matches into chunk-level rows plus a document rollup."""

        chunk_rows: list[dict[str, Any]] = []
        rolled: dict[str, dict[str, Any]] = {}
        for mention in mentions:
            chunk_id = str(mention["chunk_id"])
            entity_id = str(mention["entity_id"])
            chunk_rows.append({
                "id": f"mention:{chunk_id}|{entity_id}",
                "chunk_id": chunk_id,
                "entity_id": entity_id,
                "alias": mention["alias"],
                "source": mention["source"],
                "occurrences": mention["occurrences"],
                "confidence": mention["confidence"],
                "updated_at": updated_at,
            })
            bucket = rolled.setdefault(entity_id, {
                "id": f'mention:{document["id"]}|{entity_id}',
                "document_id": document["id"],
                "entity_id": entity_id,
                "alias": mention["alias"],
                "occurrences": 0,
                "chunks": 0,
                "confidence": "low",
                "evidence_chunk_ids": [],
            })
            bucket["occurrences"] += int(mention["occurrences"])
            bucket["chunks"] += 1
            if _CONFIDENCE_RANK[mention["confidence"]] > _CONFIDENCE_RANK[bucket["confidence"]]:
                bucket["confidence"] = mention["confidence"]
                bucket["alias"] = mention["alias"]
            bucket["evidence_chunk_ids"].append(chunk_id)
        document_rows = []
        for bucket in rolled.values():
            bucket["evidence_chunk_ids"] = sorted(bucket["evidence_chunk_ids"])[:20]
            bucket["updated_at"] = updated_at
            document_rows.append(bucket)
        document_rows.sort(key=lambda row: row["entity_id"])
        return chunk_rows, document_rows

    @staticmethod
    def _write_mentions(session, chunk_rows: list[dict[str, Any]], document_rows: list[dict[str, Any]]) -> int:
        written = 0
        if chunk_rows:
            result = session.run(
                "UNWIND $mentions AS mention "
                "MATCH (c:DocumentChunk {id:mention.chunk_id}),(e:KnowledgeEntity {id:mention.entity_id}) "
                "MERGE (c)-[m:MENTIONS {id:mention.id}]->(e) "
                "SET m.alias=mention.alias,m.source=mention.source,m.occurrences=mention.occurrences,"
                "m.confidence=mention.confidence,m.updated_at=mention.updated_at "
                "RETURN count(m) AS count",
                mentions=chunk_rows,
            ).single()
            written += result["count"] if result else 0
        if document_rows:
            result = session.run(
                "UNWIND $mentions AS mention "
                "MATCH (d:Document {id:mention.document_id}),(e:KnowledgeEntity {id:mention.entity_id}) "
                "MERGE (d)-[m:MENTIONS {id:mention.id}]->(e) "
                "SET m.alias=mention.alias,m.occurrences=mention.occurrences,m.chunks=mention.chunks,"
                "m.confidence=mention.confidence,m.evidence_chunk_ids=mention.evidence_chunk_ids,"
                "m.updated_at=mention.updated_at "
                "RETURN count(m) AS count",
                mentions=document_rows,
            ).single()
            written += result["count"] if result else 0
        return written

    def upsert_documents(
        self,
        documents: list[dict[str, Any]],
        *,
        link_entities: bool = True,
        include_tags: bool = False,
        max_alias_document_frequency: float | None = None,
    ) -> dict[str, Any]:
        document_records = [{
            "id": document["id"],
            "title": document["filename"],
            "content_hash": document["contentHash"],
            "page_count": document["pageCount"],
            "status": document.get("status", "active"),
        } for document in documents]
        chunk_records = [{
            "id": chunk["id"],
            "document_id": document["id"],
            "title": f'{document["filename"]} - page {chunk["page"]}',
            "text": chunk["text"],
            "page": chunk["page"],
            "chunk_index": chunk["index"],
            "content_hash": chunk["contentHash"],
            "status": document.get("status", "active"),
        } for document in documents for chunk in document.get("chunks", [])]
        gazetteer = Gazetteer(aliases=[])
        linking_error = ""
        if link_entities:
            try:
                corpus = [str(chunk.get("text") or "") for document in documents for chunk in document.get("chunks", [])]
                gazetteer = Gazetteer.from_entities(self._entity_catalog(), include_tags=include_tags)
                if max_alias_document_frequency is not None:
                    gazetteer = gazetteer.prune_common_aliases(corpus, max_ratio=max_alias_document_frequency)
            except Exception as error:
                linking_error = type(error).__name__
        updated_at = utc_now()
        all_chunk_rows: list[dict[str, Any]] = []
        all_document_rows: list[dict[str, Any]] = []
        for document in documents:
            chunks = [chunk for chunk in document.get("chunks", []) if str(chunk.get("id") or "") and str(chunk.get("text") or "")]
            if not chunks or not gazetteer.size:
                continue
            chunk_rows, document_rows = self._document_mentions(
                document,
                link_chunks(chunks, gazetteer),
                updated_at,
            )
            all_chunk_rows.extend(chunk_rows)
            all_document_rows.extend(document_rows)
        with self._driver() as driver, driver.session(database=self.settings.neo4j_database) as session:
            session.run("CREATE CONSTRAINT document_id IF NOT EXISTS FOR (n:Document) REQUIRE n.id IS UNIQUE")
            session.run("CREATE CONSTRAINT document_chunk_id IF NOT EXISTS FOR (n:DocumentChunk) REQUIRE n.id IS UNIQUE")
            if document_records:
                session.run(
                    "UNWIND $documents AS document "
                    "MERGE (d:KnowledgeEntity:Document {id:document.id}) "
                    "SET d.title=document.title,d.content_hash=document.content_hash,"
                    "d.page_count=document.page_count,d.status=document.status",
                    documents=document_records,
                )
            written_edges = 0
            if chunk_records:
                result = session.run(
                    "UNWIND $chunks AS chunk "
                    "MATCH (d:Document {id:chunk.document_id}) "
                    "MERGE (c:KnowledgeEntity:DocumentChunk {id:chunk.id}) "
                    "SET c.title=chunk.title,c.text=chunk.text,c.page=chunk.page,"
                    "c.chunk_index=chunk.chunk_index,c.content_hash=chunk.content_hash,c.status=chunk.status "
                    "MERGE (d)-[r:HAS_CHUNK]->(c) RETURN count(r) AS count",
                    chunks=chunk_records,
                ).single()
                written_edges = result["count"] if result else 0
            written_mentions = self._write_mentions(session, all_chunk_rows, all_document_rows)
        linked_chunks = len({row["chunk_id"] for row in all_chunk_rows})
        stats: dict[str, Any] = {
            "documents": len(document_records),
            "chunks": len(chunk_records),
            "edges": written_edges,
            "catalog_aliases": gazetteer.size,
            "catalog_entities": gazetteer.entity_count,
            "chunk_mentions": len(all_chunk_rows),
            "document_mentions": len(all_document_rows),
            "linked_chunks": linked_chunks,
            "mentions": written_mentions,
        }
        if gazetteer.ambiguous:
            stats["ambiguous_aliases"] = len(gazetteer.ambiguous)
        if gazetteer.pruned:
            stats["pruned_aliases"] = gazetteer.pruned
        if linking_error:
            stats["linking_error"] = linking_error
        return stats

    @classmethod
    def _document_link_result(cls, row: dict[str, Any]) -> dict[str, Any]:
        """Shape one cross-document pair into a graph search result."""

        entity_ids = [str(item) for item in (row.get("entity_ids") or [])]
        labels = [str(item) for item in (row.get("entity_labels") or [])]
        other_evidence = [str(item) for item in (row.get("other_evidence") or [])]
        source_title = str(row.get("source_title") or row.get("source_id") or "document")
        other_title = str(row.get("other_title") or row.get("other_id") or "document")
        return {
            "id": f'document-link:{row.get("source_id")}|{row.get("other_id")}',
            "type": "graph:DocumentLink",
            "summary": f"{source_title} shares knowledge with {other_title}",
            "resolution": "Cross-document link: " + ", ".join(labels) if labels else "Cross-document link",
            "status": "active",
            "source": "neo4j",
            "score": int(row.get("shared") or 0),
            "connections": [
                {
                    "edge_id": f'mentions:{row.get("other_id")}|{entity_id}',
                    "type": "SHARES_ENTITY",
                    "direction": "outgoing",
                    "neighbor_id": entity_id,
                    "neighbor_title": label,
                    "neighbor_type": "entity",
                    "confidence": "",
                    "evidence": other_evidence,
                }
                for entity_id, label in zip(entity_ids, labels)
            ],
        }

    def document_links(self, document_id: str = "", *, limit: int = 8) -> list[dict[str, Any]]:
        """Discover cross-document relationships from shared linked entities.

        This is the deterministic half of cross-document discovery: two documents
        become related as soon as they mention the same knowledge entity. It needs
        no model and stays explainable, because every link carries the shared
        entities and the source chunk evidence on both sides.
        """

        cypher = """
            MATCH (a:Document)-[ma:MENTIONS]->(entity:KnowledgeEntity)<-[mb:MENTIONS]-(b:Document)
            WHERE a.id < b.id
              AND ($document_id = "" OR a.id = $document_id OR b.id = $document_id)
            WITH a, b,
                 count(DISTINCT entity) AS shared,
                 collect(DISTINCT entity.id)[0..8] AS entity_ids,
                 collect(DISTINCT coalesce(entity.label, entity.id))[0..8] AS entity_labels,
                 reduce(acc = [], list IN collect(DISTINCT ma.evidence_chunk_ids) | acc + list)[0..3] AS source_evidence,
                 reduce(acc = [], list IN collect(DISTINCT mb.evidence_chunk_ids) | acc + list)[0..3] AS other_evidence
            RETURN a.id AS source_id, a.title AS source_title,
                   b.id AS other_id, b.title AS other_title,
                   shared, entity_ids, entity_labels, source_evidence, other_evidence
            ORDER BY shared DESC, source_id, other_id
            LIMIT $limit
        """
        with self._driver() as driver, driver.session(database=self.settings.neo4j_database) as session:
            rows = session.run(
                cypher,
                document_id=document_id,
                limit=max(1, min(limit, 50)),
            ).data()
        return [self._document_link_result(row) for row in rows]

    @classmethod
    def _planned_links(
        cls,
        grouped: dict[str, dict[str, Any]],
        document_rows: list[dict[str, Any]],
        labels: dict[str, str],
        *,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        """Preview the cross-document pairs a relink would create, without querying Neo4j."""

        by_entity: dict[str, set[str]] = {}
        for row in document_rows:
            by_entity.setdefault(str(row["entity_id"]), set()).add(str(row["document_id"]))
        pairs: dict[tuple[str, str], set[str]] = {}
        for entity_id, documents in by_entity.items():
            ordered = sorted(documents)
            for index, source in enumerate(ordered):
                for other in ordered[index + 1:]:
                    pairs.setdefault((source, other), set()).add(entity_id)
        ranked = sorted(pairs.items(), key=lambda item: (-len(item[1]), item[0]))[:limit]
        return [
            cls._document_link_result({
                "source_id": source,
                "source_title": grouped.get(source, {}).get("filename", source),
                "other_id": other,
                "other_title": grouped.get(other, {}).get("filename", other),
                "shared": len(entities),
                "entity_ids": sorted(entities),
                "entity_labels": [labels.get(entity_id, entity_id) for entity_id in sorted(entities)],
                "source_evidence": [],
                "other_evidence": [],
            })
            for (source, other), entities in ranked
        ]

    def relink_documents(
        self,
        document_id: str = "",
        *,
        limit: int = 20000,
        dry_run: bool = False,
        include_tags: bool = False,
        max_alias_document_frequency: float | None = None,
    ) -> dict[str, Any]:
        """Re-run deterministic linking over already stored chunks (no re-import needed).

        ``dry_run`` reports exactly what would be written without touching Neo4j.
        Because the adapter intentionally exposes no delete operation, previewing a
        relink is the only way to review link quality before it becomes permanent.
        """

        cypher = """
            MATCH (d:Document)
            WHERE $document_id = "" OR d.id = $document_id
            MATCH (d)-[:HAS_CHUNK]->(c:DocumentChunk)
            RETURN d.id AS document_id, d.title AS filename, d.status AS status,
                   c.id AS id, c.text AS text
            LIMIT $limit
        """
        with self._driver() as driver, driver.session(database=self.settings.neo4j_database) as session:
            rows = session.run(cypher, document_id=document_id, limit=max(1, limit)).data()
        gazetteer = Gazetteer.from_entities(self._entity_catalog(), include_tags=include_tags)
        if max_alias_document_frequency is not None:
            gazetteer = gazetteer.prune_common_aliases(
                [str(row["text"] or "") for row in rows],
                max_ratio=max_alias_document_frequency,
            )
        grouped: dict[str, dict[str, Any]] = {}
        for row in rows:
            entry = grouped.setdefault(str(row["document_id"]), {
                "id": str(row["document_id"]),
                "filename": str(row["filename"] or ""),
                "status": str(row["status"] or "active"),
                "chunks": [],
            })
            entry["chunks"].append({"id": str(row["id"]), "text": str(row["text"] or "")})

        updated_at = utc_now()
        all_chunk_rows: list[dict[str, Any]] = []
        all_document_rows: list[dict[str, Any]] = []
        for document in grouped.values():
            chunk_rows, document_rows = self._document_mentions(
                document,
                link_chunks(document["chunks"], gazetteer),
                updated_at,
            )
            all_chunk_rows.extend(chunk_rows)
            all_document_rows.extend(document_rows)
        with self._driver() as driver, driver.session(database=self.settings.neo4j_database) as session:
            mentions = 0 if dry_run else self._write_mentions(session, all_chunk_rows, all_document_rows)
        stats: dict[str, Any] = {
            "documents": len(grouped),
            "chunks": len(rows),
            "catalog_aliases": gazetteer.size,
            "catalog_entities": gazetteer.entity_count,
            "chunk_mentions": len(all_chunk_rows),
            "document_mentions": len(all_document_rows),
            "linked_chunks": len({row["chunk_id"] for row in all_chunk_rows}),
            "mentions": mentions,
        }
        if dry_run:
            stats["dry_run"] = True
            stats["sample"] = [
                {"chunk_id": row["chunk_id"], "entity_id": row["entity_id"], "alias": row["alias"], "confidence": row["confidence"]}
                for row in all_chunk_rows[:20]
            ]
            stats["planned_links"] = self._planned_links(
                grouped,
                all_document_rows,
                {alias.entity_id: alias.display for alias in gazetteer.aliases},
            )
        if gazetteer.ambiguous:
            stats["ambiguous_aliases"] = len(gazetteer.ambiguous)
        if gazetteer.pruned:
            stats["pruned_aliases"] = gazetteer.pruned
        if gazetteer.skipped:
            stats["skipped_aliases"] = gazetteer.skipped
        return stats