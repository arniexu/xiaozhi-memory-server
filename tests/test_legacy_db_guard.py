import hashlib
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from knowledge_agent_service import legacy_db_guard

REPO_ROOT = Path(__file__).resolve().parents[1]
GUARD_SCRIPT = REPO_ROOT / "scripts" / "legacy-db-guard.py"

SECRET_SUMMARY = "TOP SECRET customer BMC password reset flow"
SECRET_RESOLUTION = "CONFIDENTIAL host credential material"
SECRET_EVIDENCE = '[{"kind":"turn","id":"turn-1","text":"API_KEY=abcdef123456"}]'

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


def create_legacy_db(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(LEGACY_SCHEMA)
        base = {
            "fingerprint": "fp-active", "type": "lesson", "summary": SECRET_SUMMARY,
            "resolution": SECRET_RESOLUTION, "evidence_json": SECRET_EVIDENCE,
            "context_json": '{}', "tags_json": '["legacy"]',
            "source_refs_json": '["turn:turn-1"]', "storage_targets_json": '[]',
            "loading_policy": "candidate_prefetch",
            "session_id": "session-1", "workspace_id": "workspace-1", "repo_id": "repo-1",
            "created_at": "2026-08-14T00:00:00Z", "updated_at": "2026-08-15T00:00:00Z",
            "last_accessed_at": "2026-08-15T00:00:00Z",
        }
        insert = (
            "INSERT INTO memory_units VALUES (:id,:fingerprint,:type,:summary,:resolution,"
            ":evidence_json,:context_json,:tags_json,:status,:source_refs_json,"
            ":storage_targets_json,:loading_policy,:session_id,:workspace_id,:repo_id,"
            ":created_at,:updated_at,:last_accessed_at,:memory_kind)"
        )
        connection.execute(insert, {**base, "id": "memory-1", "status": "active", "memory_kind": "memory"})
        connection.execute(
            insert,
            {**base, "id": "raw-1", "fingerprint": "fp-raw", "status": "draft", "memory_kind": "raw"},
        )
        connection.execute(
            "INSERT INTO session_contexts VALUES (?,?,?,?,?,?,?,?)",
            ("session-1", "workspace-1", "repo-1", '{"goal":"Debug BMC"}', '["turn-1"]', '["turn:turn-1"]', "2026-08-14T00:00:00Z", "2026-08-15T00:00:00Z"),
        )


def run_guard(*argv: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(GUARD_SCRIPT), *argv],
        capture_output=True,
        text=True,
        timeout=60,
    )


class InspectTests(unittest.TestCase):
    def test_inspect_succeeds_and_preserves_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "legacy.sqlite3"
            create_legacy_db(source)
            before = hashlib.sha256(source.read_bytes()).hexdigest()

            proc = run_guard("inspect", "--source", str(source))

            self.assertEqual(proc.returncode, 0, proc.stderr)
            report = json.loads(proc.stdout)
            self.assertEqual(report["mode"], "inspect")
            self.assertEqual(report["sha256"], before)
            self.assertEqual(report["integrity_check"], "ok")
            self.assertEqual(report["tables"]["memory_units"], 2)
            self.assertEqual(report["tables"]["session_contexts"], 1)
            self.assertEqual(report["memory_units"]["status"], {"active": 1, "draft": 1})
            self.assertEqual(report["memory_units"]["memory_kind"], {"memory": 1, "raw": 1})
            self.assertEqual(report["memory_units"]["loading_policy"], {"candidate_prefetch": 2})
            self.assertEqual(report["memory_units"]["coverage"]["evidence"], 2)
            self.assertEqual(report["memory_units"]["coverage"]["source_refs"], 2)
            self.assertEqual(report["memory_units"]["coverage"]["workspace"], 2)
            self.assertEqual(report["memory_units"]["coverage"]["repo"], 2)
            self.assertEqual(report["session_contexts"]["coverage"]["evidence_refs"], 1)
            self.assertEqual(report["session_contexts"]["coverage"]["source_refs"], 1)
            self.assertTrue(report["source_unchanged"])
            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), before)

    def test_inspect_never_leaks_stored_content(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "legacy.sqlite3"
            create_legacy_db(source)

            proc = run_guard("inspect", "--source", str(source))

            self.assertEqual(proc.returncode, 0, proc.stderr)
            stdout = proc.stdout
            self.assertNotIn("TOP SECRET", stdout)
            self.assertNotIn("CONFIDENTIAL", stdout)
            self.assertNotIn("API_KEY", stdout)
            self.assertNotIn("abcdef123456", stdout)
            self.assertNotIn("Debug BMC", stdout)
            # Structural leak check: no stored-content column names either.
            self.assertNotIn("summary", stdout)
            self.assertNotIn("resolution", stdout)
            self.assertNotIn("context_json", stdout)
            self.assertNotIn("evidence_json", stdout)

    def test_inspect_missing_table_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "not-legacy.sqlite3"
            with sqlite3.connect(source) as connection:
                connection.execute("CREATE TABLE unrelated (id TEXT)")

            proc = run_guard("inspect", "--source", str(source))

            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("memory_units", proc.stderr)

    def test_inspect_missing_column_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "partial.sqlite3"
            with sqlite3.connect(source) as connection:
                connection.executescript(
                    """
                    CREATE TABLE memory_units (id TEXT PRIMARY KEY, summary TEXT NOT NULL);
                    CREATE TABLE session_contexts (
                        session_id TEXT NOT NULL, workspace_id TEXT NOT NULL, repo_id TEXT NOT NULL,
                        context_json TEXT NOT NULL, evidence_refs_json TEXT NOT NULL,
                        source_refs_json TEXT NOT NULL, created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL, PRIMARY KEY (session_id, workspace_id, repo_id)
                    );
                    """
                )

            proc = run_guard("inspect", "--source", str(source))

            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("missing required columns", proc.stderr)

    def test_inspect_rejects_symlink_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "legacy.sqlite3"
            create_legacy_db(source)
            link = root / "link.sqlite3"
            link.symlink_to(source)

            proc = run_guard("inspect", "--source", str(link))

            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("symlink", proc.stderr)


class ApplyTests(unittest.TestCase):
    @staticmethod
    def _source(root: Path) -> Path:
        source = root / "legacy" / "memory.sqlite3"
        source.parent.mkdir()
        create_legacy_db(source)
        return source

    def test_apply_rejects_production_staging(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self._source(root)
            production = Path("~/.local/share/knowledge-agent-service").expanduser()

            proc = run_guard("apply", "--source", str(source), "--staging-dir", str(production))

            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("production", proc.stderr)

    def test_apply_rejects_source_parent_staging(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self._source(root)

            proc = run_guard("apply", "--source", str(source), "--staging-dir", str(source.parent))

            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("parent", proc.stderr)

    def test_apply_rejects_non_empty_staging(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self._source(root)
            staging = root / "staging"
            staging.mkdir()
            (staging / "existing.txt").write_text("x", encoding="utf-8")

            proc = run_guard("apply", "--source", str(source), "--staging-dir", str(staging))

            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("not empty", proc.stderr)

    def test_apply_rejects_source_inside_staging(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self._source(root)

            proc = run_guard("apply", "--source", str(source), "--staging-dir", str(root))

            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("inside the staging", proc.stderr)

    def test_apply_rejects_symlink_staging(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self._source(root)
            real_dir = root / "real-dir"
            real_dir.mkdir()
            link = root / "staging-link"
            link.symlink_to(real_dir, target_is_directory=True)

            proc = run_guard("apply", "--source", str(source), "--staging-dir", str(link))

            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("symlink", proc.stderr)

    def test_apply_requires_staging(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self._source(root)

            proc = run_guard("apply", "--source", str(source))

            self.assertNotEqual(proc.returncode, 0)

    def test_apply_rejects_sync_neo4j_flag(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self._source(root)
            staging = root / "staging"

            proc = run_guard(
                "apply", "--source", str(source), "--staging-dir", str(staging), "--sync-neo4j"
            )

            self.assertNotEqual(proc.returncode, 0)

    def test_apply_runs_fixed_argv_with_staging_env(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self._source(root)
            staging = root / "staging"
            before = hashlib.sha256(source.read_bytes()).hexdigest()
            fake = subprocess.CompletedProcess([], 0)
            snapshot = staging.resolve() / "legacy-source.sqlite3"

            with mock.patch.object(legacy_db_guard, "_run_import", return_value=fake) as runner:
                report = legacy_db_guard.run_apply(source, staging)

            runner.assert_called_once()
            command, env = runner.call_args.args
            self.assertEqual(
                command,
                [sys.executable, "-m", "knowledge_agent_service.cli", "migrate-legacy-memory", str(snapshot), "--apply"],
            )
            self.assertNotIn(str(source.resolve()), command)
            self.assertEqual(env["KNOWLEDGE_AGENT_DATA_DIR"], str(staging.resolve()))
            self.assertTrue(report["success"])
            self.assertTrue(report["source_unchanged"])
            self.assertEqual(report["snapshot_path"], str(snapshot))
            self.assertTrue(staging.exists())
            self.assertTrue(snapshot.exists())
            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), before)

    def test_apply_keeps_staging_and_verifies_source_on_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self._source(root)
            staging = root / "staging"
            before = hashlib.sha256(source.read_bytes()).hexdigest()
            fake = subprocess.CompletedProcess([], 7)

            with mock.patch.object(legacy_db_guard, "_run_import", return_value=fake):
                report = legacy_db_guard.run_apply(source, staging)

            self.assertFalse(report["success"])
            self.assertEqual(report["exit_code"], 7)
            self.assertTrue(report["source_unchanged"])
            self.assertTrue(staging.exists())
            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), before)

    def test_apply_runner_receives_snapshot_not_real_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self._source(root)
            staging = root / "staging"
            before = hashlib.sha256(source.read_bytes()).hexdigest()
            received_db: list[Path] = []

            def runner(command: list[str], env: dict[str, str]) -> subprocess.CompletedProcess:
                db_path = Path(command[command.index("migrate-legacy-memory") + 1])
                received_db.append(db_path)
                # A buggy importer tries to write to whichever database argv
                # handed it. The real source must remain untouched regardless.
                try:
                    with sqlite3.connect(db_path) as connection:
                        connection.execute("CREATE TABLE injected_by_importer (id TEXT)")
                except (sqlite3.Error, OSError):
                    pass
                return subprocess.CompletedProcess(command, 0)

            report = legacy_db_guard.run_apply(source, staging, runner=runner)

            self.assertEqual(len(received_db), 1)
            self.assertEqual(received_db[0], staging.resolve() / "legacy-source.sqlite3")
            self.assertNotEqual(received_db[0], source.resolve())
            self.assertTrue(report["success"])
            self.assertTrue(report["source_unchanged"])
            self.assertEqual(report["source_sha256"], before)
            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), before)
            snapshot = received_db[0]
            self.assertEqual(snapshot.stat().st_mode & 0o444, 0o444)
            with legacy_db_guard._open_readonly(snapshot) as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM memory_units").fetchone()[0], 2)

    def test_apply_runner_oserror_raises_guard_error_and_verifies_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self._source(root)
            staging = root / "staging"
            before = hashlib.sha256(source.read_bytes()).hexdigest()

            def runner(command: list[str], env: dict[str, str]) -> subprocess.CompletedProcess:
                raise OSError("boom")

            with self.assertRaises(legacy_db_guard.GuardError) as ctx:
                legacy_db_guard.run_apply(source, staging, runner=runner)

            self.assertIn("importer failed", str(ctx.exception))
            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), before)

    def test_apply_detects_malicious_runner_modifying_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self._source(root)
            staging = root / "staging"
            before = hashlib.sha256(source.read_bytes()).hexdigest()

            def runner(command: list[str], env: dict[str, str]) -> subprocess.CompletedProcess:
                with sqlite3.connect(source) as connection:
                    connection.execute("CREATE TABLE injected_by_importer (id TEXT)")
                return subprocess.CompletedProcess(command, 0)

            with self.assertRaises(legacy_db_guard.GuardError) as ctx:
                legacy_db_guard.run_apply(source, staging, runner=runner)

            self.assertIn("source changed", str(ctx.exception))
            self.assertNotEqual(hashlib.sha256(source.read_bytes()).hexdigest(), before)

    def test_apply_runner_oserror_and_source_change_reports_source_change(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self._source(root)
            staging = root / "staging"

            def runner(command: list[str], env: dict[str, str]) -> subprocess.CompletedProcess:
                with sqlite3.connect(source) as connection:
                    connection.execute("CREATE TABLE injected_by_importer (id TEXT)")
                raise OSError("boom")

            with self.assertRaises(legacy_db_guard.GuardError) as ctx:
                legacy_db_guard.run_apply(source, staging, runner=runner)

            self.assertIn("source changed", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
