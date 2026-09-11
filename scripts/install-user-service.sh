#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
runtime_dir="$project_dir/.runtime"
unit_name="knowledge-agent-service.service"
unit_source="$project_dir/systemd/$unit_name.in"
unit_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
config_dir="${XDG_CONFIG_HOME:-$HOME/.config}/knowledge-agent-service"
environment_file="$config_dir/environment"

if ! systemctl --user show-environment >/dev/null 2>&1; then
    echo "A systemd user manager is not available for $USER." >&2
    exit 1
fi

mkdir -p "$runtime_dir"
if ! PYTHONPATH="$project_dir/src:$runtime_dir" /usr/bin/python3 -c \
    'import fastapi, neo4j, uvicorn' >/dev/null 2>&1; then
    /usr/bin/python3 -m pip install --upgrade --target "$runtime_dir" "$project_dir"
fi

mkdir -p "$unit_dir" "$config_dir"
if [[ ! -e "$environment_file" ]]; then
    install -m 600 "$project_dir/systemd/environment.example" "$environment_file"
fi

escaped_project_dir="${project_dir//&/\\&}"
sed "s&@PROJECT_DIR@&$escaped_project_dir&g" "$unit_source" >"$unit_dir/$unit_name"
chmod 644 "$unit_dir/$unit_name"

systemctl --user daemon-reload
systemctl --user enable --now "$unit_name"
systemctl --user --no-pager --full status "$unit_name"