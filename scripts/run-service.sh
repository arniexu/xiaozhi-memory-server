#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
runtime_dir="${KNOWLEDGE_AGENT_RUNTIME_DIR:-$project_dir/.runtime}"

export PYTHONPATH="$project_dir/src:$runtime_dir${PYTHONPATH:+:$PYTHONPATH}"

exec /usr/bin/python3 -m knowledge_agent_service.cli serve