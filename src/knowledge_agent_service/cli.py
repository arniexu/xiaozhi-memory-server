from __future__ import annotations

import argparse
import json
from pathlib import Path

from .config import load_settings
from .migration import migrate_extension_snapshot


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
    return result


def main() -> None:
    args = parser().parse_args()
    settings = load_settings()
    if args.command == "serve":
        import uvicorn

        uvicorn.run("knowledge_agent_service.api:app", host=args.host or settings.host, port=args.port or settings.port)
        return
    if args.sync_neo4j and not args.apply:
        raise SystemExit("--sync-neo4j requires --apply")
    report = migrate_extension_snapshot(args.snapshot.expanduser().resolve(), settings, apply=args.apply, sync_neo4j=args.sync_neo4j)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()