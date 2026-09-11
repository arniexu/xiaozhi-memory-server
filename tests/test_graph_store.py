import unittest
from pathlib import Path
from unittest.mock import patch

from knowledge_agent_service.config import Settings
from knowledge_agent_service.graph_store import Neo4jGraphStore


def settings(uri: str = "", user: str = "", password: str = "") -> Settings:
    return Settings(
        data_dir=Path("/tmp/knowledge-agent-test"),
        host="127.0.0.1",
        port=8765,
        neo4j_uri=uri,
        neo4j_user=user,
        neo4j_password=password,
        neo4j_database="knowledge",
    )


class Neo4jGraphStoreTests(unittest.TestCase):
    def test_unconfigured_health_has_safe_metadata(self) -> None:
        self.assertEqual(
            Neo4jGraphStore(settings()).health(),
            {"configured": False, "ready": False, "endpoint": "", "database": "knowledge"},
        )

    def test_health_never_exposes_credentials(self) -> None:
        store = Neo4jGraphStore(settings("neo4j://visible-user:uri-secret@127.0.0.1:7687", "neo4j", "auth-secret"))
        with patch.object(store, "_driver", side_effect=RuntimeError("connection failed")):
            health = store.health()
        self.assertEqual(health["endpoint"], "neo4j://127.0.0.1:7687")
        self.assertEqual(health["database"], "knowledge")
        self.assertEqual(health["error"], "RuntimeError")
        self.assertNotIn("secret", str(health))
        self.assertNotIn("visible-user", str(health))

    def test_search_formats_relationship_context_and_redacts_secrets(self) -> None:
        store = Neo4jGraphStore(settings("neo4j://127.0.0.1:7687", "neo4j", "auth-secret"))
        rows = [{
            "id": "node-1",
            "labels": ["CPLD"],
            "properties": {"id": "cpld:main", "name": "Main CPLD", "description": "Reset controller", "password": "hidden"},
            "matched_keys": ["name", "description", "password"],
            "connections": [{
                "edge_id": "edge:controls-reset",
                "type": "CONTROLS",
                "direction": "outgoing",
                "neighbor_id": "signal:pltrst",
                "neighbor_labels": ["ResetSignal"],
                "neighbor_properties": {"id": "signal:pltrst", "name": "PLTRST_N", "token": "hidden"},
                "confidence": "high",
                "evidence": ["turn:reset-analysis"],
            }],
            "score": 2,
        }]

        class Result:
            def data(self):
                return rows

        class Session:
            query = ""
            parameters = {}

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def run(self, query, **parameters):
                self.query = query
                self.parameters = parameters
                return Result()

        class Driver:
            def __init__(self):
                self.active_session = Session()

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def session(self, **_kwargs):
                return self.active_session

        driver = Driver()
        with patch.object(store, "_driver", return_value=driver):
            results = store.search("CPLD reset", limit=4)

        self.assertIn("toStringOrNull", driver.active_session.query)
        self.assertIn('coalesce(n.status, "active") = "active"', driver.active_session.query)
        self.assertEqual(driver.active_session.parameters["terms"], ["cpld", "reset"])
        self.assertEqual(results[0]["id"], "cpld:main")
        self.assertEqual(results[0]["summary"], "Main CPLD")
        self.assertIn("CONTROLS → PLTRST_N", results[0]["resolution"])
        self.assertEqual(results[0]["connections"], [{
            "edge_id": "edge:controls-reset",
            "type": "CONTROLS",
            "direction": "outgoing",
            "neighbor_id": "signal:pltrst",
            "neighbor_title": "PLTRST_N",
            "neighbor_type": "ResetSignal",
            "confidence": "high",
            "evidence": ["turn:reset-analysis"],
        }])
        self.assertNotIn("hidden", str(results))
        self.assertEqual(results[0]["source"], "neo4j")

    def test_upsert_maps_extension_timestamps_and_batches_records(self) -> None:
        store = Neo4jGraphStore(settings("neo4j://127.0.0.1:7687", "neo4j", "auth-secret"))

        class Result:
            def __init__(self, count=0):
                self.count = count

            def single(self):
                return {"count": self.count}

        class Session:
            def __init__(self):
                self.calls = []

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def run(self, query, **parameters):
                self.calls.append((query, parameters))
                return Result(1 if "UNWIND $edges" in query else 0)

        class Driver:
            def __init__(self):
                self.active_session = Session()

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def session(self, **_kwargs):
                return self.active_session

        driver = Driver()
        nodes = [{
            "id": "node-1", "kind": "fact", "label": "Reset", "summary": "Verified",
            "status": "active", "confidence": "high", "tags": ["reset"],
            "createdAt": "created", "updatedAt": "updated",
        }]
        edges = [{
            "id": "edge-1", "from": "node-1", "to": "node-1", "type": "SUPPORTS",
            "confidence": "high", "evidenceTurnIds": ["turn-1"], "createdAt": "edge-created",
        }]

        with patch.object(store, "_driver", return_value=driver):
            report = store.upsert(nodes, edges)

        self.assertEqual(report, {"nodes": 1, "edges": 1})
        self.assertEqual(len(driver.active_session.calls), 3)
        node_parameters = driver.active_session.calls[1][1]["nodes"][0]
        edge_parameters = driver.active_session.calls[2][1]["edges"][0]
        self.assertEqual(node_parameters["updated_at"], "updated")
        self.assertEqual(edge_parameters["updated_at"], "edge-created")
        self.assertNotIn("updatedAt", node_parameters)

    def test_document_upsert_labels_chunks_and_links_known_entities(self) -> None:
        store = Neo4jGraphStore(settings("neo4j://127.0.0.1:7687", "neo4j", "auth-secret"))

        class Result:
            def __init__(self, count=1, rows=None):
                self.count = count
                self.rows = rows or []

            def single(self):
                return {"count": self.count}

            def data(self):
                return self.rows

        class Session:
            def __init__(self):
                self.calls = []

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def run(self, query, **parameters):
                self.calls.append((query, parameters))
                if "AS aliases" in query:
                    return Result(rows=[{"id": "e-bmc", "label": "BMC", "kind": "concept", "tags": ["firmware"], "aliases": []}])
                return Result()

        class Driver:
            def __init__(self):
                self.active_session = Session()

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def session(self, **_kwargs):
                return self.active_session

        driver = Driver()
        documents = [{
            "id": "document:abc", "filename": "PAS.pdf", "contentHash": "abc",
            "pageCount": 1, "status": "active", "chunks": [{
                "id": "chunk:abc", "index": 0, "page": 1,
                "text": "The BMC platform requirement", "contentHash": "def",
            }],
        }]

        with patch.object(store, "_driver", return_value=driver):
            report = store.upsert_documents(documents)

        self.assertEqual(report["documents"], 1)
        self.assertEqual(report["chunks"], 1)
        self.assertEqual(report["edges"], 1)
        self.assertEqual(report["catalog_aliases"], 1)
        self.assertEqual(report["catalog_entities"], 1)
        self.assertEqual(report["chunk_mentions"], 1)
        self.assertEqual(report["document_mentions"], 1)
        self.assertEqual(report["linked_chunks"], 1)
        queries = " ".join(call[0] for call in driver.active_session.calls)
        self.assertIn("KnowledgeEntity:Document", queries)
        self.assertIn("KnowledgeEntity:DocumentChunk", queries)
        self.assertIn("HAS_CHUNK", queries)
        self.assertIn("MERGE (c)-[m:MENTIONS", queries)
        self.assertIn("MERGE (d)-[m:MENTIONS", queries)
        self.assertNotIn("DELETE", queries.upper())
        chunk_mention = next(params for query, params in driver.active_session.calls if "MERGE (c)-[m:MENTIONS" in query)["mentions"][0]
        self.assertEqual(chunk_mention["id"], "mention:chunk:abc|e-bmc")
        self.assertEqual(chunk_mention["alias"], "bmc")
        self.assertEqual(chunk_mention["source"], "label")
        document_mention = next(params for query, params in driver.active_session.calls if "MERGE (d)-[m:MENTIONS" in query)["mentions"][0]
        self.assertEqual(document_mention["evidence_chunk_ids"], ["chunk:abc"])
        self.assertEqual(document_mention["chunks"], 1)

    def test_document_upsert_can_skip_linking_and_reports_no_catalog_read(self) -> None:
        store = Neo4jGraphStore(settings("neo4j://127.0.0.1:7687", "neo4j", "auth-secret"))

        class Result:
            def single(self):
                return {"count": 1}

            def data(self):
                raise AssertionError("catalogue must not be read when linking is disabled")

        class Session:
            def __init__(self):
                self.queries = []

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def run(self, query, **_parameters):
                self.queries.append(query)
                return Result()

        class Driver:
            def __init__(self):
                self.active_session = Session()

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def session(self, **_kwargs):
                return self.active_session

        driver = Driver()
        documents = [{
            "id": "document:abc", "filename": "PAS.pdf", "contentHash": "abc",
            "pageCount": 1, "status": "active", "chunks": [{
                "id": "chunk:abc", "index": 0, "page": 1,
                "text": "The BMC platform requirement", "contentHash": "def",
            }],
        }]

        with patch.object(store, "_driver", return_value=driver):
            report = store.upsert_documents(documents, link_entities=False)

        self.assertEqual(report["catalog_aliases"], 0)
        self.assertEqual(report["catalog_entities"], 0)
        self.assertEqual(report["chunk_mentions"], 0)
        self.assertEqual(report["document_mentions"], 0)
        self.assertNotIn("MENTIONS", " ".join(driver.active_session.queries))

    def test_document_mentions_roll_up_occurrences_and_evidence(self) -> None:
        chunk_rows, document_rows = Neo4jGraphStore._document_mentions(
            {"id": "document:abc"},
            [
                {"chunk_id": "c1", "entity_id": "e-bmc", "alias": "bmc", "source": "label", "occurrences": 2, "confidence": "high"},
                {"chunk_id": "c2", "entity_id": "e-bmc", "alias": "bmc", "source": "label", "occurrences": 1, "confidence": "medium"},
                {"chunk_id": "c2", "entity_id": "e-ipmi", "alias": "ipmi", "source": "tag", "occurrences": 1, "confidence": "low"},
            ],
            "2026-09-11T00:00:00Z",
        )
        self.assertEqual(len(chunk_rows), 3)
        self.assertEqual(len(document_rows), 2)
        bmc = next(row for row in document_rows if row["entity_id"] == "e-bmc")
        self.assertEqual(bmc["id"], "mention:document:abc|e-bmc")
        self.assertEqual(bmc["occurrences"], 3)
        self.assertEqual(bmc["chunks"], 2)
        self.assertEqual(bmc["confidence"], "high")
        self.assertEqual(bmc["evidence_chunk_ids"], ["c1", "c2"])
        self.assertEqual(bmc["updated_at"], "2026-09-11T00:00:00Z")

    def test_relink_documents_requires_neo4j(self) -> None:
        with self.assertRaises(RuntimeError):
            Neo4jGraphStore(settings()).relink_documents()

    def test_document_links_shape_cross_document_relationship_with_evidence(self) -> None:
        link = Neo4jGraphStore._document_link_result({
            "source_id": "document:aaa",
            "source_title": "PAS.pdf",
            "other_id": "document:bbb",
            "other_title": "Runbook.pdf",
            "shared": 2,
            "entity_ids": ["e-bmc", "e-ipmi"],
            "entity_labels": ["BMC", "IPMI"],
            "source_evidence": ["chunk:a1"],
            "other_evidence": ["chunk:b1", "chunk:b2"],
        })
        self.assertEqual(link["id"], "document-link:document:aaa|document:bbb")
        self.assertEqual(link["type"], "graph:DocumentLink")
        self.assertEqual(link["source"], "neo4j")
        self.assertEqual(link["score"], 2)
        self.assertIn("PAS.pdf", link["summary"])
        self.assertIn("Runbook.pdf", link["summary"])
        self.assertIn("BMC", link["resolution"])
        self.assertEqual([item["type"] for item in link["connections"]], ["SHARES_ENTITY", "SHARES_ENTITY"])
        self.assertEqual(link["connections"][0]["neighbor_title"], "BMC")
        self.assertEqual(link["connections"][1]["evidence"], ["chunk:b1", "chunk:b2"])

    def test_document_links_query_uses_shared_entity_pattern_and_stays_deterministic(self) -> None:
        store = Neo4jGraphStore(settings("neo4j://127.0.0.1:7687", "neo4j", "auth-secret"))

        class Result:
            def data(self):
                return [{
                    "source_id": "document:aaa", "source_title": "PAS.pdf",
                    "other_id": "document:bbb", "other_title": "Runbook.pdf",
                    "shared": 1, "entity_ids": ["e-bmc"], "entity_labels": ["BMC"],
                    "source_evidence": ["chunk:a1"], "other_evidence": ["chunk:b1"],
                }]

        class Session:
            query = ""
            parameters = {}

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def run(self, query, **parameters):
                self.query = query
                self.parameters = parameters
                return Result()

        class Driver:
            def __init__(self):
                self.active_session = Session()

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def session(self, **_kwargs):
                return self.active_session

        driver = Driver()
        with patch.object(store, "_driver", return_value=driver):
            links = store.document_links("document:aaa", limit=4)

        self.assertIn("WHERE a.id < b.id", driver.active_session.query)
        self.assertIn("ORDER BY shared DESC", driver.active_session.query)
        self.assertEqual(driver.active_session.parameters["document_id"], "document:aaa")
        self.assertEqual(driver.active_session.parameters["limit"], 4)
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0]["connections"][0]["neighbor_id"], "e-bmc")

    def test_document_links_requires_neo4j(self) -> None:
        with self.assertRaises(RuntimeError):
            Neo4jGraphStore(settings()).document_links()

    def test_relink_dry_run_reports_matches_without_writing(self) -> None:
        store = Neo4jGraphStore(settings("neo4j://127.0.0.1:7687", "neo4j", "auth-secret"))

        class Result:
            def __init__(self, rows=None):
                self.rows = rows or []

            def data(self):
                return self.rows

            def single(self):
                return {"count": 99}

        class Session:
            def __init__(self):
                self.queries = []

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def run(self, query, **parameters):
                self.queries.append(query)
                if "AS text" in query:
                    return Result([{
                        "document_id": "document:aaa", "filename": "PAS.pdf", "status": "active",
                        "id": "chunk:aaa1", "text": "The BMC reset sequence",
                    }])
                if "AS aliases" in query:
                    return Result([{"id": "e-bmc", "label": "BMC", "kind": "concept", "tags": [], "aliases": []}])
                return Result()

        class Driver:
            def __init__(self):
                self.active_session = Session()

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def session(self, **_kwargs):
                return self.active_session

        driver = Driver()
        with patch.object(store, "_driver", return_value=driver):
            report = store.relink_documents(dry_run=True)

        self.assertTrue(report["dry_run"])
        self.assertEqual(report["mentions"], 0)
        self.assertEqual(report["chunk_mentions"], 1)
        self.assertEqual(report["linked_chunks"], 1)
        self.assertEqual(report["sample"][0]["chunk_id"], "chunk:aaa1")
        self.assertNotIn("MERGE (c)-[m:MENTIONS", " ".join(driver.active_session.queries))


if __name__ == "__main__":
    unittest.main()