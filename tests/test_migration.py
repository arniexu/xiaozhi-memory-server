import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from knowledge_agent_service import legacy_db_guard
from knowledge_agent_service.config import Settings
from knowledge_agent_service.memory_store import MemoryStore
from knowledge_agent_service.migration import (
    migrate_continuity_data,
    migrate_document_data,
    migrate_extension_snapshot,
    migrate_legacy_memory_database,
)
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


LEGACY_SCHEMA = """
CREATE TABLE memory_units (
    id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL UNIQUE, type TEXT NOT NULL,
    summary TEXT NOT NULL, resolution TEXT NOT NULL, evidence_json TEXT NOT NULL,
    context_json TEXT NOT NULL, tags_json TEXT NOT NULL, status TEXT NOT NULL,
    source_refs_json TEXT NOT NULL, storage_targets_json TEXT NOT NULL,
    loading_policy TEXT NOT NULL, session_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL, repo_id TEXT NOT NULL, created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL, last_accessed_at TEXT NOT NULL,
    memory_kind TEXT NOT NULL DEFAULT 'memory'
);
CREATE TABLE session_contexts (
    session_id TEXT NOT NULL, workspace_id TEXT NOT NULL, repo_id TEXT NOT NULL,
    context_json TEXT NOT NULL, evidence_refs_json TEXT NOT NULL,
    source_refs_json TEXT NOT NULL, created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL, PRIMARY KEY (session_id, workspace_id, repo_id)
);
"""


def legacy_memory_unit(**overrides: object) -> dict:
    unit = {
        "id": "memory-1",
        "fingerprint": "fp-1",
        "type": "lesson",
        "summary": "Legacy lesson about BMC power sequencing",
        "resolution": "Power on in this order",
        "evidence_json": '[{"kind":"turn","id":"turn-1"}]',
        "context_json": '{"domain":"bmc"}',
        "tags_json": '["legacy","bmc"]',
        "status": "active",
        "source_refs_json": '["turn:turn-1"]',
        "storage_targets_json": '{"default":"memory"}',
        "loading_policy": "candidate_prefetch",
        "session_id": "session-1",
        "workspace_id": "workspace-1",
        "repo_id": "repo-1",
        "created_at": "2026-08-14T00:00:00Z",
        "updated_at": "2026-08-15T00:00:00Z",
        "last_accessed_at": "2026-08-15T00:00:00Z",
        "memory_kind": "memory",
    }
    unit.update(overrides)
    return unit


def legacy_session_context(
    *,
    session_id: str = "session-1",
    workspace_id: str = "workspace-1",
    repo_id: str = "repo-1",
    context_json: str = '{"goal":"Debug BMC"}',
    evidence_refs_json: str = '["turn-1"]',
    source_refs_json: str = '["turn:turn-1"]',
    created_at: str = "2026-08-14T00:00:00Z",
    updated_at: str = "2026-08-15T00:00:00Z",
) -> tuple:
    return (session_id, workspace_id, repo_id, context_json, evidence_refs_json, source_refs_json, created_at, updated_at)


def create_legacy_db(path: Path, units: list[dict], sessions: list[tuple] | None = None) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(LEGACY_SCHEMA)
        insert = (
            "INSERT INTO memory_units VALUES (:id,:fingerprint,:type,:summary,:resolution,"
            ":evidence_json,:context_json,:tags_json,:status,:source_refs_json,"
            ":storage_targets_json,:loading_policy,:session_id,:workspace_id,:repo_id,"
            ":created_at,:updated_at,:last_accessed_at,:memory_kind)"
        )
        for unit in units:
            connection.execute(insert, unit)
        for session in sessions or []:
            connection.execute("INSERT INTO session_contexts VALUES (?,?,?,?,?,?,?,?)", session)


class LegacyMigrationTests(unittest.TestCase):
    @staticmethod
    def staging(root: Path, units: list[dict], sessions: list[tuple] | None = None) -> tuple[Path, Settings]:
        staging_dir = root / "staging"
        staging_dir.mkdir()
        snapshot = staging_dir / legacy_db_guard.SNAPSHOT_FILENAME
        create_legacy_db(snapshot, units, sessions)
        snapshot.chmod(0o444)
        configured = Settings(staging_dir, "127.0.0.1", 8765, "", "", "", "neo4j")
        return snapshot, configured

    def test_dry_run_does_not_write(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "legacy.sqlite3"
            create_legacy_db(source, [legacy_memory_unit()])
            before = hashlib.sha256(source.read_bytes()).hexdigest()
            configured = Settings(root / "data", "127.0.0.1", 8765, "", "", "", "neo4j")

            report = migrate_legacy_memory_database(source, configured)

            self.assertEqual(report["mode"], "dry-run")
            self.assertEqual(report["created"], 0)
            self.assertEqual(report["updated"], 0)
            self.assertFalse((root / "data").exists())
            self.assertTrue(report["source_unchanged"])
            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), before)
            self.assertEqual(report["source_distributions"]["status"], {"active": 1})
            self.assertEqual(report["source_distributions"]["memory_kind"], {"memory": 1})
            self.assertEqual(report["source_distributions"]["type"], {"lesson": 1})
            self.assertEqual(report["source_distributions"]["loading_policy"], {"candidate_prefetch": 1})
            self.assertEqual(report["planned_target_distributions"]["status"], {"draft": 1})
            self.assertEqual(report["planned_target_distributions"]["memory_kind"], {"candidate": 1})

    def test_apply_rejects_non_staging_source_name(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "legacy.sqlite3"
            create_legacy_db(source, [legacy_memory_unit()])
            configured = Settings(root / "data", "127.0.0.1", 8765, "", "", "", "neo4j")

            with self.assertRaises(legacy_db_guard.GuardError):
                migrate_legacy_memory_database(source, configured, apply=True)

    def test_apply_rejects_snapshot_outside_data_dir(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / legacy_db_guard.SNAPSHOT_FILENAME
            create_legacy_db(source, [legacy_memory_unit()])
            source.chmod(0o444)
            configured = Settings(root / "data", "127.0.0.1", 8765, "", "", "", "neo4j")

            with self.assertRaises(legacy_db_guard.GuardError):
                migrate_legacy_memory_database(source, configured, apply=True)

    def test_apply_rejects_writable_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            staging_dir = root / "staging"
            staging_dir.mkdir()
            source = staging_dir / legacy_db_guard.SNAPSHOT_FILENAME
            create_legacy_db(source, [legacy_memory_unit()])
            source.chmod(0o644)
            configured = Settings(staging_dir, "127.0.0.1", 8765, "", "", "", "neo4j")

            with self.assertRaises(legacy_db_guard.GuardError):
                migrate_legacy_memory_database(source, configured, apply=True)

    def test_apply_rejects_production_data_dir(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            production = root / "production"
            production.mkdir()
            source = production / legacy_db_guard.SNAPSHOT_FILENAME
            create_legacy_db(source, [legacy_memory_unit()])
            source.chmod(0o444)
            configured = Settings(production, "127.0.0.1", 8765, "", "", "", "neo4j")

            with mock.patch.object(legacy_db_guard, "DEFAULT_PRODUCTION_DIR", production.resolve()):
                with self.assertRaises(legacy_db_guard.GuardError):
                    migrate_legacy_memory_database(source, configured, apply=True)

    def test_apply_staging_snapshot_is_idempotent_and_stable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot, configured = self.staging(
                root,
                [
                    legacy_memory_unit(),
                    legacy_memory_unit(id="memory-2", fingerprint="fp-2", summary="Second lesson", resolution="Second"),
                ],
                [legacy_session_context()],
            )
            before = hashlib.sha256(snapshot.read_bytes()).hexdigest()

            first = migrate_legacy_memory_database(snapshot, configured, apply=True)
            second = migrate_legacy_memory_database(snapshot, configured, apply=True)

            self.assertEqual(first["created"], 3)
            self.assertEqual(first["updated"], 0)
            self.assertEqual(second["created"], 0)
            self.assertEqual(second["updated"], 3)
            self.assertTrue(first["source_unchanged"])
            self.assertTrue(second["source_unchanged"])
            self.assertEqual(hashlib.sha256(snapshot.read_bytes()).hexdigest(), before)
            store = MemoryStore(configured.memory_db, configured.event_log)
            self.assertEqual(store.stats()["total"], 3)
            self.assertEqual(store.stats()["active"], 0)

    def test_lifecycle_mapping_active_raw_rejected_and_type_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot, configured = self.staging(root, [
                legacy_memory_unit(id="active-1", fingerprint="fp-a", status="active", memory_kind="memory"),
                legacy_memory_unit(id="raw-1", fingerprint="fp-r", status="draft", memory_kind="raw"),
                legacy_memory_unit(id="rejected-1", fingerprint="fp-j", status="rejected", memory_kind="memory"),
                legacy_memory_unit(id="weird-1", fingerprint="fp-w", type="snippet", summary="Odd type", resolution="Odd"),
            ])

            migrate_legacy_memory_database(snapshot, configured, apply=True)
            store = MemoryStore(configured.memory_db, configured.event_log)
            rows = {item["id"]: item for item in store.get_many(["legacy:active-1", "legacy:raw-1", "legacy:rejected-1", "legacy:weird-1"])}

            self.assertEqual((rows["legacy:active-1"]["status"], rows["legacy:active-1"]["memory_kind"]), ("draft", "candidate"))
            self.assertEqual((rows["legacy:raw-1"]["status"], rows["legacy:raw-1"]["memory_kind"]), ("draft", "raw"))
            self.assertEqual((rows["legacy:rejected-1"]["status"], rows["legacy:rejected-1"]["memory_kind"]), ("rejected", "memory"))
            self.assertEqual(rows["legacy:weird-1"]["type"], "lesson")
            self.assertEqual(rows["legacy:weird-1"]["context"]["source_type"], "snippet")
            self.assertEqual(rows["legacy:active-1"]["context"]["imported_from"], "legacy-memory-agent")
            self.assertEqual(rows["legacy:active-1"]["context"]["source_status"], "active")
            self.assertEqual(rows["legacy:active-1"]["context"]["source_memory_kind"], "memory")
            self.assertEqual(rows["legacy:active-1"]["context"]["loading_policy"], "candidate_prefetch")
            self.assertEqual(rows["legacy:active-1"]["context"]["storage_targets"], {"default": "memory"})
            self.assertEqual(rows["legacy:active-1"]["context"]["last_accessed_at"], "2026-08-15T00:00:00Z")

    def test_bad_json_is_skipped_and_counted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot, configured = self.staging(root, [
                legacy_memory_unit(),
                legacy_memory_unit(id="bad-1", fingerprint="fp-b", evidence_json="{not json"),
            ])

            report = migrate_legacy_memory_database(snapshot, configured, apply=True)

            self.assertEqual(report["skipped"], 1)
            self.assertEqual(report["errors"], 1)
            self.assertEqual(report["created"], 1)
            store = MemoryStore(configured.memory_db, configured.event_log)
            self.assertEqual(store.stats()["total"], 1)

    def test_storage_targets_accepts_object_and_array_rejects_scalar_and_null(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot, configured = self.staging(root, [
                legacy_memory_unit(id="obj-1", fingerprint="fp-o", storage_targets_json='{"default":"memory"}'),
                legacy_memory_unit(id="arr-1", fingerprint="fp-a2", storage_targets_json='["memory","vector"]'),
                legacy_memory_unit(id="scalar-1", fingerprint="fp-s", storage_targets_json='"scalar"'),
                legacy_memory_unit(id="null-1", fingerprint="fp-n", storage_targets_json="null"),
            ])

            report = migrate_legacy_memory_database(snapshot, configured, apply=True)

            self.assertEqual(report["created"], 2)
            self.assertEqual(report["skipped"], 2)
            self.assertEqual(report["errors"], 2)
            store = MemoryStore(configured.memory_db, configured.event_log)
            rows = {item["id"]: item for item in store.get_many(["legacy:obj-1", "legacy:arr-1"])}
            self.assertEqual(rows["legacy:obj-1"]["context"]["storage_targets"], {"default": "memory"})
            self.assertEqual(rows["legacy:arr-1"]["context"]["storage_targets"], ["memory", "vector"])
            self.assertEqual(store.get_many(["legacy:scalar-1", "legacy:null-1"]), [])

    def test_dry_run_planned_distributions_count_accepted_records(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot, configured = self.staging(root, [
                legacy_memory_unit(id="obj-1", fingerprint="fp-o", storage_targets_json='{"default":"memory"}'),
                legacy_memory_unit(id="arr-1", fingerprint="fp-a2", storage_targets_json='["memory","vector"]'),
                legacy_memory_unit(id="scalar-1", fingerprint="fp-s", storage_targets_json='"scalar"'),
            ])

            report = migrate_legacy_memory_database(snapshot, configured)

            self.assertEqual(report["mode"], "dry-run")
            self.assertEqual(report["memory_units"], 3)
            self.assertEqual(report["skipped"], 1)
            self.assertEqual(report["errors"], 1)
            self.assertEqual(report["created"], 0)
            self.assertEqual(report["planned_target_distributions"]["status"], {"draft": 2})
            self.assertEqual(report["planned_target_distributions"]["memory_kind"], {"candidate": 2})

    def test_id_namespace_and_source_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot, configured = self.staging(root, [legacy_memory_unit()])
            before = hashlib.sha256(snapshot.read_bytes()).hexdigest()

            report = migrate_legacy_memory_database(snapshot, configured, apply=True)

            store = MemoryStore(configured.memory_db, configured.event_log)
            self.assertEqual(store.get_many(["legacy:memory-1"])[0]["id"], "legacy:memory-1")
            self.assertEqual(store.get_many(["memory-1"]), [])
            self.assertTrue(report["source_unchanged"])
            self.assertEqual(hashlib.sha256(snapshot.read_bytes()).hexdigest(), before)

    def test_default_search_excludes_legacy_imports(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot, configured = self.staging(root, [legacy_memory_unit()])

            migrate_legacy_memory_database(snapshot, configured, apply=True)
            store = MemoryStore(configured.memory_db, configured.event_log)

            self.assertEqual(store.search("power sequencing"), [])
            self.assertEqual(store.search("BMC"), [])

    def test_session_context_fields_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot, configured = self.staging(root, [legacy_memory_unit()], [legacy_session_context()])

            migrate_legacy_memory_database(snapshot, configured, apply=True)
            store = MemoryStore(configured.memory_db, configured.event_log)
            key = json.dumps(["session-1", "workspace-1", "repo-1"], ensure_ascii=False, sort_keys=True)
            session_id = "legacy-session-context:" + hashlib.sha256(key.encode()).hexdigest()
            row = store.get_many([session_id])[0]

            self.assertEqual(row["type"], "session")
            self.assertEqual(row["summary"], "Legacy session context")
            self.assertEqual(row["status"], "draft")
            self.assertEqual(row["memory_kind"], "candidate")
            self.assertEqual(row["resolution"], json.dumps({"goal": "Debug BMC"}, ensure_ascii=False, sort_keys=True))
            self.assertEqual(row["evidence"], [{"kind": "session-evidence", "id": "turn-1"}])
            self.assertEqual(row["source_refs"], ["turn:turn-1"])
            self.assertEqual(row["session_id"], "session-1")
            self.assertEqual(row["workspace_id"], "workspace-1")
            self.assertEqual(row["repo_id"], "repo-1")
            self.assertEqual(row["created_at"], "2026-08-14T00:00:00Z")
            self.assertEqual(row["updated_at"], "2026-08-15T00:00:00Z")

    def test_cli_subcommand_parses(self) -> None:
        from knowledge_agent_service.cli import parser as cli_parser

        args = cli_parser().parse_args(["migrate-legacy-memory", "/tmp/x/legacy-source.sqlite3", "--apply"])
        self.assertEqual(args.command, "migrate-legacy-memory")
        self.assertTrue(args.apply)


if __name__ == "__main__":
    unittest.main()