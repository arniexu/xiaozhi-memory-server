#!/usr/bin/env python3
"""Entry point for the legacy database guard.

Thin wrapper that imports the standard-library-only guard logic so the same
code is exercised by both the command line and the test suite.

Usage:
    python3 scripts/legacy-db-guard.py inspect --source /path/to/legacy.sqlite3
    python3 scripts/legacy-db-guard.py apply --source /path/to/legacy.sqlite3 --staging-dir /tmp/staging
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC_DIR = _REPO_ROOT / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from knowledge_agent_service.legacy_db_guard import main  # noqa: E402


if __name__ == "__main__":
    sys.exit(main())
