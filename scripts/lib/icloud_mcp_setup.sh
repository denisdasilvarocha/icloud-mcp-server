#!/usr/bin/env bash

set -euo pipefail

SERVER_NAME="icloud"
SETUP_STEP=0
SETUP_TOTAL=1

if [ -t 1 ] && [ "${NO_COLOR:-}" = "" ]; then
  COLOR_RESET="$(printf '\033[0m')"
  COLOR_BOLD="$(printf '\033[1m')"
  COLOR_DIM="$(printf '\033[2m')"
  COLOR_GREEN="$(printf '\033[32m')"
  COLOR_BLUE="$(printf '\033[34m')"
  COLOR_YELLOW="$(printf '\033[33m')"
  COLOR_RED="$(printf '\033[31m')"
else
  COLOR_RESET=""
  COLOR_BOLD=""
  COLOR_DIM=""
  COLOR_GREEN=""
  COLOR_BLUE=""
  COLOR_YELLOW=""
  COLOR_RED=""
fi

setup_error() {
  local exit_code=$?
  printf '\n%sSetup failed.%s Last step: %s\n' "$COLOR_RED" "$COLOR_RESET" "${SETUP_CURRENT_STEP:-startup}" >&2
  printf 'Fix the message above, then run the same setup script again.\n' >&2
  exit "$exit_code"
}

trap setup_error ERR

setup_title() {
  local title="$1"
  SETUP_TOTAL="$2"
  SETUP_STEP=0
  printf '\n%s%s%s\n' "$COLOR_BOLD" "$title" "$COLOR_RESET"
  printf '%sConfigures the iCloud MCP server and verifies the local client entry.%s\n\n' "$COLOR_DIM" "$COLOR_RESET"
}

setup_step() {
  SETUP_STEP=$((SETUP_STEP + 1))
  SETUP_CURRENT_STEP="$1"
  local filled empty width done_count remaining_count
  width=20
  done_count=$((SETUP_STEP * width / SETUP_TOTAL))
  remaining_count=$((width - done_count))
  filled="$(printf '%*s' "$done_count" '' | tr ' ' '#')"
  empty="$(printf '%*s' "$remaining_count" '' | tr ' ' '.')"
  printf '%s[%s%s] %s/%s%s %s\n' "$COLOR_BLUE" "$filled" "$empty" "$SETUP_STEP" "$SETUP_TOTAL" "$COLOR_RESET" "$1"
}

setup_ok() {
  printf '  %sOK%s %s\n' "$COLOR_GREEN" "$COLOR_RESET" "$1"
}

setup_warn() {
  printf '  %sNote:%s %s\n' "$COLOR_YELLOW" "$COLOR_RESET" "$1"
}

setup_fail() {
  printf '  %sError:%s %s\n' "$COLOR_RED" "$COLOR_RESET" "$1" >&2
  return 1
}

setup_path() {
  printf '  %s%s%s\n' "$COLOR_DIM" "$1" "$COLOR_RESET"
}

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
    printf 'Apple ID / iCloud email\n'
    setup_path 'Use the iCloud email address for the account you want to sync.'
    read -r -p "> " ICLOUD_SETUP_APPLE_ID
  fi
  if [ -z "${ICLOUD_SETUP_APP_PASSWORD:-}" ]; then
    printf 'Apple app-specific password\n'
    setup_path 'Use an Apple app-specific password, not your Apple ID password.'
    read -r -s -p "> " ICLOUD_SETUP_APP_PASSWORD
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
  printf 'Where should MCP client config be written?\n'
  printf '  1) current project - repo-local config, ignored by git where needed\n'
  printf '  2) global user config - available from any directory\n'
  read -r -p "Choice [1/2]: " choice
  case "$choice" in
    1|"") ICLOUD_SETUP_SCOPE="project" ;;
    2) ICLOUD_SETUP_SCOPE="global" ;;
    *) printf 'Invalid choice.\n' >&2; return 1 ;;
  esac
  export ICLOUD_SETUP_SCOPE
}

prompt_password_storage() {
  if [ -n "${ICLOUD_SETUP_PERSIST_APP_PASSWORD:-}" ]; then
    return
  fi

  local default answer prompt
  default="false"
  prompt="Store app password in the client env file instead of the OS keychain? [y/N]: "
  if [ "$(uname -s 2>/dev/null || true)" = "Linux" ]; then
    default="true"
    prompt="Store app password in the client env file? Recommended on Linux/headless systems. [Y/n]: "
  fi

  printf 'Credential storage\n'
  setup_path 'OS keychain is safer when available. Env file is more reliable for Linux/headless MCP launches and is chmod 600.'
  read -r -p "$prompt" answer
  case "${answer:-}" in
    "")
      ICLOUD_SETUP_PERSIST_APP_PASSWORD="$default"
      ;;
    y|Y|yes|YES)
      ICLOUD_SETUP_PERSIST_APP_PASSWORD="true"
      ;;
    n|N|no|NO)
      ICLOUD_SETUP_PERSIST_APP_PASSWORD="false"
      ;;
    *)
      printf 'Invalid choice.\n' >&2
      return 1
      ;;
  esac
  export ICLOUD_SETUP_PERSIST_APP_PASSWORD
}

prompt_sync_on_start() {
  if [ -n "${ICLOUD_SETUP_SYNC_ON_START:-}" ]; then
    return
  fi
  printf 'Initial sync behavior\n'
  setup_path 'Yes gives useful search results immediately. No only uses existing local cache until you run sync.'
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
  if [ "${ICLOUD_SETUP_PERSIST_APP_PASSWORD:-false}" != "true" ]; then
    app_password=""
  fi
  "$PYTHON_BIN" - "$root" "$uv_bin" "$apple_id" "$app_password" "$sync_on_start" <<'PY'
import json
import sys

root, uv_bin, apple_id, app_password, sync_on_start = sys.argv[1:]
env = {
    "ICLOUD_APPLE_ID": apple_id,
    "ICLOUD_MCP_SYNC_ON_START": sync_on_start,
}
if app_password:
    env["ICLOUD_APP_PASSWORD"] = app_password
payload = {
    "name": "icloud",
    "command": uv_bin,
    "args": ["run", "--project", root, "icloud-mcp"],
    "cwd": root,
    "env": env,
}
print(json.dumps(payload))
PY
}

ensure_project_ready() {
  local root="$1"
  local uv_bin="$2"
  "$uv_bin" run --project "$root" python - <<'PY' >/dev/null
from icloud_mcp.mcp.server import create_server
print(create_server)
PY
}

store_keychain_credentials() {
  local root="$1"
  local uv_bin="$2"
  if [ "${ICLOUD_SETUP_PERSIST_APP_PASSWORD:-false}" = "true" ]; then
    return
  fi
  "$uv_bin" run --project "$root" python - "$ICLOUD_SETUP_APPLE_ID" "$ICLOUD_SETUP_APP_PASSWORD" <<'PY'
import sys

from icloud_mcp.platform.secrets import store_icloud_credentials

store_icloud_credentials(sys.argv[1], sys.argv[2])
PY
}

verify_runtime_credentials() {
  local root="$1"
  local uv_bin="$2"
  if [ "${ICLOUD_SETUP_PERSIST_APP_PASSWORD:-false}" = "true" ]; then
    ICLOUD_APPLE_ID="$ICLOUD_SETUP_APPLE_ID" \
    ICLOUD_APP_PASSWORD="$ICLOUD_SETUP_APP_PASSWORD" \
    "$uv_bin" run --project "$root" python - <<'PY' >/dev/null
from icloud_mcp.platform.config import Settings
from icloud_mcp.platform.secrets import load_icloud_credentials

creds = load_icloud_credentials(Settings.from_env())
if creds is None:
    raise SystemExit("credentials did not load from environment")
PY
  else
    ICLOUD_APPLE_ID="$ICLOUD_SETUP_APPLE_ID" \
    "$uv_bin" run --project "$root" python - <<'PY' >/dev/null
from icloud_mcp.platform.config import Settings
from icloud_mcp.platform.secrets import load_icloud_credentials

creds = load_icloud_credentials(Settings.from_env())
if creds is None:
    raise SystemExit("credentials did not load from keychain")
PY
  fi
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
  local app_password=""
  if [ "${ICLOUD_SETUP_PERSIST_APP_PASSWORD:-false}" = "true" ]; then
    app_password="$ICLOUD_SETUP_APP_PASSWORD"
  fi
  mkdir -p "$(dirname "$path")"
  touch "$path"
  chmod 600 "$path"
  "$PYTHON_BIN" - "$path" "$ICLOUD_SETUP_APPLE_ID" "$app_password" "$ICLOUD_SETUP_SYNC_ON_START" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
values = {
    "ICLOUD_APPLE_ID": sys.argv[2],
    "ICLOUD_MCP_SYNC_ON_START": sys.argv[4],
}
if sys.argv[3]:
    values["ICLOUD_APP_PASSWORD"] = sys.argv[3]
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
  setup_ok "$client MCP config updated"
  setup_path "$path"
}

print_next_steps() {
  local client="$1"
  local config_path="$2"
  local extra="${3:-}"
  printf '\n%sNext steps%s\n' "$COLOR_BOLD" "$COLOR_RESET"
  printf '  1. Restart %s, or reload MCP if the client supports it.\n' "$client"
  printf '  2. Ask the client: "Which MCP tools are available?"\n'
  printf '  3. Run an iCloud sync if you disabled sync-on-start.\n'
  printf '\n%sChanged%s\n' "$COLOR_BOLD" "$COLOR_RESET"
  setup_path "$config_path"
  if [ -n "$extra" ]; then
    setup_path "$extra"
  fi
}

verify_codex_config() {
  local path="$1"
  "$PYTHON_BIN" - "$path" <<'PY' >/dev/null
import sys
from pathlib import Path

path = Path(sys.argv[1])
text = path.read_text()
required = [
    "[mcp_servers.icloud]",
    "command = ",
    '"icloud-mcp"',
    "[mcp_servers.icloud.env]",
    "ICLOUD_APPLE_ID = ",
]
for item in required:
    if item not in text:
        raise SystemExit(f"missing Codex config entry: {item}")
PY
}

verify_claude_config() {
  local path="$1"
  "$PYTHON_BIN" - "$path" <<'PY' >/dev/null
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
server = json.loads(path.read_text())["mcpServers"]["icloud"]
if server["type"] != "stdio" or not server["command"] or "icloud-mcp" not in server["args"]:
    raise SystemExit("invalid Claude Code icloud MCP entry")
if not server.get("env", {}).get("ICLOUD_APPLE_ID"):
    raise SystemExit("missing ICLOUD_APPLE_ID")
PY
}

verify_hermes_config() {
  local config_path="$1"
  local env_path="$2"
  "$PYTHON_BIN" - "$config_path" "$env_path" <<'PY' >/dev/null
import sys
from pathlib import Path

config_path = Path(sys.argv[1])
env_path = Path(sys.argv[2])
config_text = config_path.read_text()
required = [
    "mcp_servers:",
    "  icloud:",
    "    command:",
    "    args:",
    "      - \"icloud-mcp\"",
    "    env:",
    "      ICLOUD_APPLE_ID:",
]
for item in required:
    if item not in config_text:
        raise SystemExit(f"missing Hermes config entry: {item}")
env_text = env_path.read_text()
if "ICLOUD_APPLE_ID=" not in env_text or "ICLOUD_MCP_SYNC_ON_START=" not in env_text:
    raise SystemExit("missing Hermes .env values")
PY
}
