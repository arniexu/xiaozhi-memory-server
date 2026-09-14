"""Read-only guard and safe apply proxy for legacy Memory Agent SQLite data.

This module is intentionally dependency-free (standard library only) so it can
be used to inspect real legacy databases without a full service install, and so
the ``scripts/legacy-db-guard.py`` entry point never imports FastAPI, Neo4j, or
the vector store.

It exposes two operations:

* ``inspect`` -- open a legacy database read-only, validate its expected schema
  and integrity, and emit only aggregate metadata. Stored transcript, summary,
  resolution, context, and evidence content are never printed.
* ``apply`` -- act as a safety proxy for the future legacy migration CLI. It
  validates that the staging directory is safe, copies the source into the
  staging directory as a read-only, integrity-checked snapshot (SQLite backup
  API from an immutable ``mode=ro`` handle), and then runs a fixed command
  whose only database argument is that snapshot path. The real source is never
  passed to the importer and is SHA-256 verified unchanged before and after.
  It never implements migration itself and never accepts an arbitrary command.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Sequence
from urllib.parse import quote

# Expected legacy schema. The guard fails clearly when a database does not look
# like a legacy Memory Agent store. This also protects against pointing the
# guard at the service's own ``memory.sqlite3``, which lacks ``loading_policy``,
# ``storage_targets_json``, and ``last_accessed_at``.
MEMORY_UNITS_TABLE = "memory_units"
SESSION_CONTEXTS_TABLE = "session_contexts"

MEMORY_UNITS_REQUIRED_COLUMNS = frozenset({
    "id", "fingerprint", "type", "summary", "resolution", "evidence_json",
    "context_json", "tags_json", "status", "memory_kind", "source_refs_json",
    "storage_targets_json", "loading_policy", "session_id", "workspace_id",
    "repo_id", "created_at", "updated_at", "last_accessed_at",
})

SESSION_CONTEXTS_REQUIRED_COLUMNS = frozenset({
    "session_id", "workspace_id", "repo_id", "context_json",
    "evidence_refs_json", "source_refs_json", "created_at", "updated_at",
})

DEFAULT_PRODUCTION_DIR = Path("~/.local/share/knowledge-agent-service").expanduser()

# Fixed command for the future importer. Callers must never supply a shell
# string or arbitrary argv; the guard appends only the staging snapshot and
# ``--apply``. The interpreter is resolved with ``sys.executable`` so the
# importer always runs under the same Python as the guard itself.
IMPORT_MODULE = "knowledge_agent_service.cli"
IMPORT_SUBCOMMAND = "migrate-legacy-memory"

# The importer receives this read-only snapshot inside the staging directory,
# never the real source path.
SNAPSHOT_FILENAME = "legacy-source.sqlite3"


class GuardError(Exception):
    """Raised when a guard precondition or validation fails."""


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_text(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2)


def _ensure_no_symlink(path: Path) -> None:
    probe = path
    while True:
        if probe.is_symlink():
            raise GuardError(f"symlinks are not allowed in this path: {path} (symlink at {probe})")
        parent = probe.parent
        if parent == probe:
            break
        probe = parent


def _prepare_source(path: Path) -> Path:
    raw = path.expanduser()
    _ensure_no_symlink(raw)
    resolved = raw.resolve()
    if not resolved.exists():
        raise GuardError(f"source does not exist: {resolved}")
    if not resolved.is_file():
        raise GuardError(f"source is not a regular file: {resolved}")
    return resolved


def _readonly_uri(path: Path) -> str:
    return "file:" + quote(str(path), safe="/") + "?mode=ro&immutable=1"


def _open_readonly(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(_readonly_uri(path), uri=True)
    connection.execute("PRAGMA query_only=ON")
    return connection


def _table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
    return {row[1] for row in rows}


def _missing_columns(connection: sqlite3.Connection, table: str, required: frozenset[str]) -> list[str]:
    return sorted(required - _table_columns(connection, table))


def _non_empty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _non_empty_json_list(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = json.loads(value)
    except (ValueError, TypeError):
        return False
    return isinstance(parsed, list) and len(parsed) > 0


def _distribution(connection: sqlite3.Connection, table: str, column: str) -> dict[str, int]:
    result: dict[str, int] = {}
    for (value, count) in connection.execute(f"SELECT {column}, COUNT(*) FROM {table} GROUP BY {column}"):
        key = value if _non_empty_string(value) else "(empty)"
        result[key] = result.get(key, 0) + int(count)
    return result


def _integrity_check(connection: sqlite3.Connection) -> str:
    messages = [str(row[0]) for row in connection.execute("PRAGMA integrity_check").fetchall()]
    if messages == ["ok"]:
        return "ok"
    return "; ".join(messages)


def _build_inspect_report(connection: sqlite3.Connection, source: Path, sha256: str) -> dict[str, Any]:
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for required_table in (MEMORY_UNITS_TABLE, SESSION_CONTEXTS_TABLE):
        if required_table not in tables:
            raise GuardError(f"expected table {required_table!r} not found in {source}")

    missing_memory = _missing_columns(connection, MEMORY_UNITS_TABLE, MEMORY_UNITS_REQUIRED_COLUMNS)
    if missing_memory:
        raise GuardError(f"{MEMORY_UNITS_TABLE} is missing required columns: {missing_memory}")
    missing_session = _missing_columns(connection, SESSION_CONTEXTS_TABLE, SESSION_CONTEXTS_REQUIRED_COLUMNS)
    if missing_session:
        raise GuardError(f"{SESSION_CONTEXTS_TABLE} is missing required columns: {missing_session}")

    integrity = _integrity_check(connection)
    if integrity != "ok":
        raise GuardError(f"integrity_check failed: {integrity}")

    memory_units = connection.execute(f"SELECT COUNT(*) FROM {MEMORY_UNITS_TABLE}").fetchone()[0]
    session_contexts = connection.execute(f"SELECT COUNT(*) FROM {SESSION_CONTEXTS_TABLE}").fetchone()[0]

    coverage = {"evidence": 0, "source_refs": 0, "workspace": 0, "repo": 0}
    for (evidence_json, source_refs_json, workspace_id, repo_id) in connection.execute(
        f"SELECT evidence_json, source_refs_json, workspace_id, repo_id FROM {MEMORY_UNITS_TABLE}"
    ):
        if _non_empty_json_list(evidence_json):
            coverage["evidence"] += 1
        if _non_empty_json_list(source_refs_json):
            coverage["source_refs"] += 1
        if _non_empty_string(workspace_id):
            coverage["workspace"] += 1
        if _non_empty_string(repo_id):
            coverage["repo"] += 1

    session_coverage = {"evidence_refs": 0, "source_refs": 0}
    for (evidence_refs_json, source_refs_json) in connection.execute(
        f"SELECT evidence_refs_json, source_refs_json FROM {SESSION_CONTEXTS_TABLE}"
    ):
        if _non_empty_json_list(evidence_refs_json):
            session_coverage["evidence_refs"] += 1
        if _non_empty_json_list(source_refs_json):
            session_coverage["source_refs"] += 1

    return {
        "mode": "inspect",
        "source": str(source),
        "sha256": sha256,
        "size_bytes": source.stat().st_size,
        "integrity_check": integrity,
        "tables": {MEMORY_UNITS_TABLE: memory_units, SESSION_CONTEXTS_TABLE: session_contexts},
        "memory_units": {
            "status": _distribution(connection, MEMORY_UNITS_TABLE, "status"),
            "memory_kind": _distribution(connection, MEMORY_UNITS_TABLE, "memory_kind"),
            "loading_policy": _distribution(connection, MEMORY_UNITS_TABLE, "loading_policy"),
            "coverage": coverage,
        },
        "session_contexts": {"coverage": session_coverage},
    }


def inspect_database(path: Path) -> dict[str, Any]:
    source = _prepare_source(path)
    before = file_sha256(source)
    connection = _open_readonly(source)
    try:
        report = _build_inspect_report(connection, source, before)
    finally:
        connection.close()

    after = file_sha256(source)
    if after != before:
        raise GuardError(f"source changed during inspection: sha256 {before} -> {after}")
    report["source_unchanged"] = True
    return report


def build_import_command(snapshot: Path) -> list[str]:
    return [sys.executable, "-m", IMPORT_MODULE, IMPORT_SUBCOMMAND, str(snapshot), "--apply"]


def _snapshot_source(source: Path, snapshot_path: Path) -> str:
    """Copy ``source`` into ``snapshot_path`` as a consistent, read-only snapshot.

    The copy is produced with the SQLite backup API from a read-only immutable
    handle, so it is a transactionally consistent point-in-time image even if
    the source is in WAL mode. The snapshot is then made read-only (``0444``),
    integrity-checked, and hashed. The real source is never modified.
    """
    if snapshot_path.exists():
        raise GuardError(f"snapshot already exists: {snapshot_path}")

    source_connection = _open_readonly(source)
    try:
        try:
            with sqlite3.connect(snapshot_path) as destination:
                source_connection.backup(destination)
        except Exception:
            if snapshot_path.exists():
                snapshot_path.unlink()
            raise
    finally:
        source_connection.close()

    snapshot_path.chmod(0o444)

    snapshot_connection = _open_readonly(snapshot_path)
    try:
        integrity = _integrity_check(snapshot_connection)
    finally:
        snapshot_connection.close()
    if integrity != "ok":
        raise GuardError(f"snapshot integrity_check failed: {integrity}")

    return file_sha256(snapshot_path)


def _run_import(command: Sequence[str], env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(command, env=env)


def _validate_staging(source: Path, staging: Path) -> None:
    production = DEFAULT_PRODUCTION_DIR.resolve()
    source_parent = source.parent

    if staging == source_parent or source_parent in staging.parents:
        raise GuardError(f"staging-dir must not be the source parent directory or inside it: {staging}")
    if staging == production or production in staging.parents or staging in production.parents:
        raise GuardError(f"staging-dir must not be the production data directory or its parent/child: {staging}")
    if staging == source or staging in source.parents:
        raise GuardError(f"source must not be inside the staging directory: {source}")
    if staging.exists() and not staging.is_dir():
        raise GuardError(f"staging-dir exists and is not a directory: {staging}")
    if staging.exists() and any(staging.iterdir()):
        raise GuardError(f"staging-dir already exists and is not empty: {staging}")


def run_apply(
    source: Path,
    staging_dir: Path,
    *,
    runner: Callable[[Sequence[str], dict[str, str]], subprocess.CompletedProcess] | None = None,
) -> dict[str, Any]:
    source = _prepare_source(source)
    raw_staging = staging_dir.expanduser()
    _ensure_no_symlink(raw_staging)
    staging = raw_staging.resolve()
    _validate_staging(source, staging)

    before = file_sha256(source)

    snapshot_path = staging / SNAPSHOT_FILENAME
    snapshot_sha256: str | None = None
    completed: subprocess.CompletedProcess | None = None
    importer_error: Exception | None = None

    try:
        staging.mkdir(parents=True, exist_ok=True)
        snapshot_sha256 = _snapshot_source(source, snapshot_path)

        # Close the TOCTOU window: re-hash the real source after the snapshot
        # is complete and require it to match the pre-snapshot hash.
        after_snapshot = file_sha256(source)
        if after_snapshot != before:
            raise GuardError(f"source changed while creating snapshot: sha256 {before} -> {after_snapshot}")

        command = build_import_command(snapshot_path)
        env = {**os.environ, "KNOWLEDGE_AGENT_DATA_DIR": str(staging)}
        import_callable = runner if runner is not None else _run_import
        try:
            completed = import_callable(command, env)
        except Exception as exc:
            importer_error = exc
    finally:
        # Verify the real source on every path: importer success, non-zero
        # exit, or exception. Nothing may mask a changed source.
        after = file_sha256(source)

    if after != before:
        raise GuardError(f"source changed during apply: sha256 {before} -> {after}")

    if importer_error is not None:
        raise GuardError(f"importer failed: {importer_error!r}") from importer_error

    assert completed is not None and snapshot_sha256 is not None
    return {
        "mode": "apply",
        "source": str(source),
        "source_sha256": before,
        "source_unchanged": True,
        "snapshot_path": str(snapshot_path),
        "snapshot_sha256": snapshot_sha256,
        "staging_dir": str(staging),
        "command": command,
        "exit_code": completed.returncode,
        "success": completed.returncode == 0,
        "staging_kept": completed.returncode != 0,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="legacy-db-guard",
        description="Read-only inspection and safe apply proxy for legacy Memory Agent SQLite data.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    inspect = subparsers.add_parser("inspect", help="validate and report aggregate metadata for a legacy database")
    inspect.add_argument("--source", type=Path, required=True, help="legacy SQLite database (opened read-only)")

    apply = subparsers.add_parser("apply", help="run the legacy migration into a safe staging directory")
    apply.add_argument("--source", type=Path, required=True)
    apply.add_argument("--staging-dir", type=Path, required=True, help="staging directory that receives all migration writes")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "inspect":
            report = inspect_database(args.source)
            print(_json_text(report))
            return 0
        if args.command == "apply":
            report = run_apply(args.source, args.staging_dir)
            print(_json_text(report))
            if report["success"] and report["source_unchanged"]:
                return 0
            return report["exit_code"] if 0 < report["exit_code"] < 256 else 1
    except GuardError as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 1
    return 1


if __name__ == "__main__":
    sys.exit(main())
