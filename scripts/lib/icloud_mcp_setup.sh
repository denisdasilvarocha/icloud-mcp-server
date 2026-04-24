#!/usr/bin/env bash

set -euo pipefail

SERVER_NAME="icloud"

script_dir() {
  local source="${BASH_SOURCE[0]}"
  while [ -L "$source" ]; do
    local dir
    dir="$(cd -P "$(dirname "$source")" >/dev/null 2>&1 && pwd)"
    source="$(readlink "$source")"
    [[ "$source" != /* ]] && source="$dir/$source"
  done
  cd -P "$(dirname "$source")" >/dev/null 2>&1 && pwd
}

repo_root() {
  local dir
  dir="$(cd "$(script_dir)/../.." >/dev/null 2>&1 && pwd)"
  if command -v git >/dev/null 2>&1 && git -C "$dir" rev-parse --show-toplevel >/dev/null 2>&1; then
    git -C "$dir" rev-parse --show-toplevel
  else
    printf '%s\n' "$dir"
  fi
}

require_command() {
  local name="$1"
  if ! command -v "$name" >/dev/null 2>&1; then
    printf 'Missing required command: %s\n' "$name" >&2
    return 1
  fi
}

find_uv() {
  if command -v uv >/dev/null 2>&1; then
    command -v uv
    return
  fi
  printf 'Missing uv. Install uv first: https://docs.astral.sh/uv/\n' >&2
  return 1
}

find_python() {
  if command -v python3.12 >/dev/null 2>&1; then
    command -v python3.12
  elif command -v python3 >/dev/null 2>&1; then
    command -v python3
  else
    printf 'Missing python3.\n' >&2
    return 1
  fi
}

prompt_credentials() {
  if [ -z "${ICLOUD_SETUP_APPLE_ID:-}" ]; then
    read -r -p "Apple ID / iCloud email: " ICLOUD_SETUP_APPLE_ID
  fi
  if [ -z "${ICLOUD_SETUP_APP_PASSWORD:-}" ]; then
    read -r -s -p "Apple app-specific password: " ICLOUD_SETUP_APP_PASSWORD
    printf '\n'
  fi
  if [ -z "$ICLOUD_SETUP_APPLE_ID" ] || [ -z "$ICLOUD_SETUP_APP_PASSWORD" ]; then
    printf 'Apple ID and app-specific password are required.\n' >&2
    return 1
  fi
  export ICLOUD_SETUP_APPLE_ID ICLOUD_SETUP_APP_PASSWORD
}

prompt_scope() {
  if [ -n "${ICLOUD_SETUP_SCOPE:-}" ]; then
    return
  fi
  printf 'Configure where?\n'
  printf '  1) current project\n'
  printf '  2) global user config\n'
  read -r -p "Choice [1/2]: " choice
  case "$choice" in
    1|"") ICLOUD_SETUP_SCOPE="project" ;;
    2) ICLOUD_SETUP_SCOPE="global" ;;
    *) printf 'Invalid choice.\n' >&2; return 1 ;;
  esac
  export ICLOUD_SETUP_SCOPE
}

prompt_sync_on_start() {
  if [ -n "${ICLOUD_SETUP_SYNC_ON_START:-}" ]; then
    return
  fi
  read -r -p "Sync iCloud on MCP server start? [Y/n]: " answer
  case "${answer:-Y}" in
    y|Y|yes|YES) ICLOUD_SETUP_SYNC_ON_START="true" ;;
    n|N|no|NO) ICLOUD_SETUP_SYNC_ON_START="false" ;;
    *) printf 'Invalid choice.\n' >&2; return 1 ;;
  esac
  export ICLOUD_SETUP_SYNC_ON_START
}

build_payload_json() {
  local root="$1"
  local uv_bin="$2"
  local apple_id="$3"
  local app_password="$4"
  local sync_on_start="$5"
  "$PYTHON_BIN" - "$root" "$uv_bin" "$apple_id" "$app_password" "$sync_on_start" <<'PY'
import json
import sys

root, uv_bin, apple_id, app_password, sync_on_start = sys.argv[1:]
payload = {
    "name": "icloud",
    "command": uv_bin,
    "args": ["run", "--project", root, "icloud-mcp"],
    "cwd": root,
    "env": {
        "ICLOUD_APPLE_ID": apple_id,
        "ICLOUD_APP_PASSWORD": app_password,
        "ICLOUD_MCP_SYNC_ON_START": sync_on_start,
    },
}
print(json.dumps(payload))
PY
}

ensure_project_ready() {
  local root="$1"
  local uv_bin="$2"
  printf 'Checking MCP server imports...\n'
  "$uv_bin" run --project "$root" python - <<'PY' >/dev/null
from icloud_mcp.server import create_server
print(create_server)
PY
}

add_git_exclude() {
  local root="$1"
  local rel_path="$2"
  local exclude_file="$root/.git/info/exclude"
  if [ -d "$root/.git" ]; then
    mkdir -p "$(dirname "$exclude_file")"
    grep -qxF "$rel_path" "$exclude_file" 2>/dev/null || printf '%s\n' "$rel_path" >>"$exclude_file"
  fi
}

write_env_file() {
  local path="$1"
  mkdir -p "$(dirname "$path")"
  touch "$path"
  chmod 600 "$path"
  "$PYTHON_BIN" - "$path" "$ICLOUD_SETUP_APPLE_ID" "$ICLOUD_SETUP_APP_PASSWORD" "$ICLOUD_SETUP_SYNC_ON_START" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
values = {
    "ICLOUD_APPLE_ID": sys.argv[2],
    "ICLOUD_APP_PASSWORD": sys.argv[3],
    "ICLOUD_MCP_SYNC_ON_START": sys.argv[4],
}
lines = []
if path.exists():
    for line in path.read_text().splitlines():
        key = line.split("=", 1)[0].strip() if "=" in line else None
        if key not in values:
            lines.append(line)
for key, value in values.items():
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    lines.append(f'{key}="{escaped}"')
path.write_text("\n".join(lines) + "\n")
PY
}

print_done() {
  local client="$1"
  local path="$2"
  printf '%s MCP config updated: %s\n' "$client" "$path"
}
