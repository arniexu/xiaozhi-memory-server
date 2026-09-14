from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import load_settings
from .legacy_db_guard import GuardError
from .migration import migrate_extension_snapshot, migrate_legacy_memory_database


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="knowledge-agent-service")
    commands = result.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="run the local HTTP service")
    serve.add_argument("--host")
    serve.add_argument("--port", type=int)
    migrate = commands.add_parser("migrate-extension", help="inspect or incrementally import an extension snapshot")
    migrate.add_argument("snapshot", type=Path)
    migrate.add_argument("--apply", action="store_true")
    migrate.add_argument("--sync-neo4j", action="store_true")
    legacy = commands.add_parser(
        "migrate-legacy-memory",
        help="import a guard-staged legacy Memory Agent snapshot (internal; controlled by legacy-db-guard)",
    )
    legacy.add_argument("snapshot", type=Path)
    legacy.add_argument("--apply", action="store_true")
    return result


def main() -> None:
    args = parser().parse_args()
    settings = load_settings()
    if args.command == "serve":
        import uvicorn

        uvicorn.run("knowledge_agent_service.api:app", host=args.host or settings.host, port=args.port or settings.port)
        return
    if args.command == "migrate-extension":
        if args.sync_neo4j and not args.apply:
            raise SystemExit("--sync-neo4j requires --apply")
        report = migrate_extension_snapshot(
            args.snapshot.expanduser().resolve(), settings, apply=args.apply, sync_neo4j=args.sync_neo4j
        )
    elif args.command == "migrate-legacy-memory":
        try:
            report = migrate_legacy_memory_database(args.snapshot, settings, apply=args.apply)
        except GuardError as exc:
            print(json.dumps({"error": str(exc)}), file=sys.stderr)
            raise SystemExit(1)
    else:
        raise SystemExit(f"unknown command: {args.command}")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()