import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from knowledge_agent_service.config import Settings
from knowledge_agent_service.memory_store import MemoryStore
from knowledge_agent_service.migration import migrate_continuity_data, migrate_document_data, migrate_extension_snapshot
from knowledge_agent_service.vector_store import VectorStore


def settings(tmp_path: Path) -> Settings:
    return Settings(tmp_path / "data", "127.0.0.1", 8765, "", "", "", "neo4j")


class MigrationTests(unittest.TestCase):
    @staticmethod
    def snapshot() -> dict:
        return {
            "schemaVersion": 1,
            "sessions": [{"id": "s1", "workspaceId": "w1"}],
            "turns": [{"id": "t1", "sessionId": "s1"}],
            "vectors": [{"id": "v1", "sourceId": "k1", "sourceKind": "knowledge", "values": [1.0, 0.0], "createdAt": "now"}],
            "nodes": [{"id": "k1", "kind": "fact", "label": "Fact", "summary": "Verified", "status": "active", "confidence": "high", "evidenceTurnIds": ["t1"], "tags": [], "createdAt": "now", "updatedAt": "now"}],
            "edges": [],
        }

    def test_migration_is_dry_run_by_default_and_preserves_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "snapshot.json"
            source.write_text(json.dumps(self.snapshot()), encoding="utf-8")
            before = hashlib.sha256(source.read_bytes()).hexdigest()
            report = migrate_extension_snapshot(source, settings(root))
            self.assertEqual(report["mode"], "dry-run")
            self.assertFalse((root / "data").exists())
            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), before)

    def test_apply_is_incremental_and_preserves_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "snapshot.json"
            source.write_text(json.dumps(self.snapshot()), encoding="utf-8")
            first = migrate_extension_snapshot(source, settings(root), apply=True)
            second = migrate_extension_snapshot(source, settings(root), apply=True)
            self.assertEqual(first["memory_stats"]["total"], 1)
            self.assertEqual(second["memory_stats"]["total"], 1)
            self.assertEqual(second["vector_stats"]["total"], 1)
            self.assertTrue(second["source_unchanged"])

    def test_continuity_import_is_idempotent_and_searchable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            configured = settings(root)
            decisions = [{
                "id": "decision-1",
                "question": "Why use the layered service?",
                "answer": "It preserves decisions across branch switches.",
                "timestamp": "2026-09-10T00:00:00Z",
                "tags": ["architecture"],
                "status": "active",
                "priority": "high",
                "relationships": {"relatedTo": []},
            }, {
                "id": "decision-2",
                "question": "Why use the layered service?",
                "answer": "It preserves decisions across branch switches.",
                "timestamp": "2026-09-10T00:00:00Z",
                "status": "active",
            }]
            embeddings = [{"decisionId": "decision-1", "values": [1.0, 0.0]}]

            first = migrate_continuity_data(decisions, configured, source="decisions.json", embeddings=embeddings)
            second = migrate_continuity_data(decisions, configured, source="decisions.json", embeddings=embeddings)
            results = MemoryStore(configured.memory_db, configured.event_log).search("layered service")

            self.assertEqual((first["created"], first["updated"]), (2, 0))
            self.assertEqual((second["created"], second["updated"]), (0, 2))
            self.assertEqual(second["memory_stats"]["total"], 2)
            self.assertEqual(second["vector_stats"]["total"], 1)
            self.assertEqual({result["id"] for result in results}, {"continuity:decision-1", "continuity:decision-2"})
            self.assertEqual(results[0]["context"]["imported_from"], "continuity")

    def test_document_import_is_idempotent_searchable_and_draft_aware(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            configured = settings(root)
            documents = [{
                "id": "document:released",
                "filename": "Platform PAS.pdf",
                "sourcePath": "/docs/Platform PAS.pdf",
                "contentHash": "released",
                "pageCount": 1,
                "status": "active",
                "chunks": [{"id": "chunk:released", "index": 0, "page": 1, "text": "Verified thermal requirement", "contentHash": "a"}],
            }, {
                "id": "document:wip",
                "filename": "Register WIP.pdf",
                "sourcePath": "/docs/Register WIP.pdf",
                "contentHash": "wip",
                "pageCount": 1,
                "status": "draft",
                "chunks": [{"id": "chunk:wip", "index": 0, "page": 1, "text": "Tentative register value", "contentHash": "b"}],
            }]
            embeddings = [
                {"chunkId": "chunk:released", "values": [1.0, 0.0]},
                {"chunkId": "chunk:wip", "values": [0.0, 1.0]},
            ]

            first = migrate_document_data(documents, configured, embeddings=embeddings)
            second = migrate_document_data(documents, configured, embeddings=embeddings)
            store = MemoryStore(configured.memory_db, configured.event_log)

            self.assertEqual((first["created"], first["updated"]), (2, 0))
            self.assertEqual((second["created"], second["updated"]), (0, 2))
            self.assertEqual(second["memory_stats"]["candidate"], 1)
            self.assertEqual(second["vector_stats"]["total"], 2)
            self.assertEqual([item["id"] for item in store.search("thermal requirement")], ["chunk:released"])
            self.assertEqual(store.search("tentative register"), [])
            vector_store = VectorStore(configured.vector_db)
            self.assertEqual([item["source_id"] for item in vector_store.search([1.0, 0.0], source_kind="knowledge")], ["chunk:released"])
            self.assertEqual([item["source_id"] for item in vector_store.search([0.0, 1.0], source_kind="candidate")], ["chunk:wip"])


if __name__ == "__main__":
    unittest.main()