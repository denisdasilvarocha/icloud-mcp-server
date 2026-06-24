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
  printf 'Fix the message above, then run setup again.\n' >&2
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

setup_path() {
  printf '  %s%s%s\n' "$COLOR_DIM" "$1" "$COLOR_RESET"
}

script_dir() {
  cd -P "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd
}

repo_root() {
  local dir
  dir="$(cd "$(script_dir)/.." >/dev/null 2>&1 && pwd)"
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

add_git_exclude() {
  local root="$1"
  local rel_path="$2"
  local exclude_file="$root/.git/info/exclude"
  if [ -d "$root/.git" ]; then
    mkdir -p "$(dirname "$exclude_file")"
    grep -qxF "$rel_path" "$exclude_file" 2>/dev/null || printf '%s\n' "$rel_path" >>"$exclude_file"
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
    read -r -p "> " ICLOUD_SETUP_APP_PASSWORD
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
  printf '  1) Current Project - repo-local config, ignored by git where needed\n'
  printf '  2) Global User Config - available from any directory\n'
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
  prompt="Store app password in the OS keychain when possible? [Y/n]: "
  if [ "$(uname -s 2>/dev/null || true)" = "Linux" ]; then
    default="true"
    prompt="Store app password in the OS keychain when possible? Not recommended on Linux/headless systems. [y/N]: "
  fi

  printf 'Credential storage\n'
  setup_path 'OS keychain is safer when available. Env file is more reliable for Linux/headless MCP launches and is chmod 600.'
  read -r -p "$prompt" answer
  case "${answer:-}" in
    "") ICLOUD_SETUP_PERSIST_APP_PASSWORD="$default" ;;
    y|Y|yes|YES) ICLOUD_SETUP_PERSIST_APP_PASSWORD="false" ;;
    n|N|no|NO) ICLOUD_SETUP_PERSIST_APP_PASSWORD="true" ;;
    *) printf 'Invalid choice.\n' >&2; return 1 ;;
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

pick_agent() {
  printf 'Which agent/runtime should be configured?\n' >&2
  printf '  1) Codex\n' >&2
  printf '  2) Claude Code\n' >&2
  printf '  3) Hermes Agent\n' >&2
  printf '  4) All MCP clients\n' >&2
  printf '  5) Docker Compose (Recommended)\n' >&2
  read -r -p "Choice [1-5]: " choice
  case "$choice" in
    1) printf 'codex\n' ;;
    2) printf 'claude-code\n' ;;
    3) printf 'hermes-agent\n' ;;
    4) printf 'all\n' ;;
    5|"") printf 'docker\n' ;;
    *) printf 'Invalid choice.\n' >&2; return 1 ;;
  esac
}

pick_docker_agent() {
  printf 'Which MCP client should use Docker Compose?\n' >&2
  printf '  1) Codex\n' >&2
  printf '  2) Claude Code\n' >&2
  printf '  3) Hermes Agent\n' >&2
  read -r -p "Choice [1-3]: " choice
  case "$choice" in
    1|"") printf 'codex\n' ;;
    2) printf 'claude-code\n' ;;
    3) printf 'hermes-agent\n' ;;
    *) printf 'Invalid choice.\n' >&2; return 1 ;;
  esac
}

client_label() {
  case "$1" in
    codex) printf 'Codex\n' ;;
    claude-code) printf 'Claude Code\n' ;;
    hermes-agent) printf 'Hermes Agent\n' ;;
    *) printf '%s\n' "$1" ;;
  esac
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

if load_icloud_credentials(Settings.from_env()) is None:
    raise SystemExit("credentials did not load from environment")
PY
  else
    ICLOUD_APPLE_ID="$ICLOUD_SETUP_APPLE_ID" \
    "$uv_bin" run --project "$root" python - <<'PY' >/dev/null
from icloud_mcp.platform.config import Settings
from icloud_mcp.platform.secrets import load_icloud_credentials

if load_icloud_credentials(Settings.from_env()) is None:
    raise SystemExit("credentials did not load from keychain")
PY
  fi
}

build_standard_payload_json() {
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
print(json.dumps({"name": "icloud", "command": uv_bin, "args": ["run", "--project", root, "icloud-mcp"], "cwd": root, "env": env}))
PY
}

build_docker_payload_json() {
  "$PYTHON_BIN" - <<'PY'
import json

print(json.dumps({"name": "icloud", "command": "docker", "args": ["exec", "-i", "icloud-mcp-server", "icloud-mcp"], "env": {}}))
PY
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
values = {"ICLOUD_APPLE_ID": sys.argv[2], "ICLOUD_MCP_SYNC_ON_START": sys.argv[4]}
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

client_paths() {
  local client="$1"
  local root="$2"
  local config_path="" env_path=""
  case "$client" in
    codex)
      if [ "$ICLOUD_SETUP_SCOPE" = "project" ]; then
        config_path="$root/.codex/config.toml"
        mkdir -p "$(dirname "$config_path")"
        add_git_exclude "$root" ".codex/config.toml"
      else
        config_path="$HOME/.codex/config.toml"
        mkdir -p "$(dirname "$config_path")"
      fi
      ;;
    claude-code)
      if [ "$ICLOUD_SETUP_SCOPE" = "project" ]; then
        config_path="$root/.mcp.json"
        add_git_exclude "$root" ".mcp.json"
      else
        config_path="$HOME/.claude.json"
      fi
      ;;
    hermes-agent)
      if [ "$ICLOUD_SETUP_SCOPE" = "project" ]; then
        config_path="$root/.hermes/config.yaml"
        env_path="$root/.hermes/.env"
        add_git_exclude "$root" ".hermes/"
      else
        config_path="$HOME/.hermes/config.yaml"
        env_path="$HOME/.hermes/.env"
      fi
      mkdir -p "$(dirname "$config_path")"
      touch "$env_path"
      chmod 600 "$env_path"
      ;;
    *) printf 'Unknown MCP client: %s\n' "$client" >&2; return 1 ;;
  esac
  printf '%s\n%s\n' "$config_path" "$env_path"
}

write_client_config_python() {
  local client="$1"
  local config_path="$2"
  local env_path="$3"
  local payload="$4"
  PAYLOAD_JSON="$payload" "$PYTHON_BIN" - "$client" "$config_path" "$env_path" <<'PY'
import json
import os
import sys
from pathlib import Path

client, config_path, env_path = sys.argv[1:]
path = Path(config_path)
payload = json.loads(os.environ["PAYLOAD_JSON"])

if client == "codex":
    lines = path.read_text().splitlines() if path.exists() else []
    kept = []
    i = 0
    while i < len(lines):
        stripped = lines[i].strip()
        if stripped == "[mcp_servers.icloud]" or stripped.startswith("[mcp_servers.icloud."):
            i += 1
            while i < len(lines):
                next_stripped = lines[i].strip()
                if next_stripped.startswith("[") and next_stripped.endswith("]") and not next_stripped.startswith("[mcp_servers.icloud."):
                    break
                i += 1
            continue
        kept.append(lines[i])
        i += 1
    if kept and kept[-1].strip():
        kept.append("")
    kept.extend(
        [
            "[mcp_servers.icloud]",
            f"command = {json.dumps(payload['command'])}",
            f"args = {json.dumps(payload['args'])}",
        ]
    )
    if payload.get("cwd"):
        kept.append(f"cwd = {json.dumps(payload['cwd'])}")
    kept.extend(["enabled = true", "startup_timeout_sec = 30", "tool_timeout_sec = 120"])
    if payload["env"]:
        kept.append("[mcp_servers.icloud.env]")
        for key, value in payload["env"].items():
            kept.append(f"{key} = {json.dumps(value)}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(kept).rstrip() + "\n")
elif client == "claude-code":
    data = json.loads(path.read_text()) if path.exists() and path.read_text().strip() else {}
    data.setdefault("mcpServers", {})[payload["name"]] = {
        "type": "stdio",
        "command": payload["command"],
        "args": payload["args"],
        "env": payload["env"],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
else:
    text = path.read_text() if path.exists() else ""
    lines = text.splitlines()
    kept = []
    in_mcp = False
    mcp_seen = False
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if stripped in {"mcp_servers: {}", "mcp_servers: null"}:
            kept.append("mcp_servers:")
            in_mcp = True
            mcp_seen = True
            i += 1
            continue
        is_root_key = bool(line) and not line.startswith((" ", "\t")) and stripped.endswith(":")
        if is_root_key:
            in_mcp = stripped == "mcp_servers:"
            mcp_seen = mcp_seen or in_mcp
        if in_mcp and line.startswith("  icloud:"):
            i += 1
            while i < len(lines):
                next_line = lines[i]
                if not next_line.strip():
                    i += 1
                    continue
                if not next_line.startswith((" ", "\t")) or (next_line.startswith("  ") and not next_line.startswith("    ")):
                    break
                i += 1
            continue
        kept.append(line)
        i += 1
    block = [
        "  icloud:",
        f"    command: {json.dumps(payload['command'])}",
        "    args:",
        *[f"      - {json.dumps(arg)}" for arg in payload["args"]],
    ]
    if payload["env"]:
        block.extend(["    env:", *[f"      {key}: {json.dumps(value)}" for key, value in payload["env"].items()]])
    block.extend(["    enabled: true", "    timeout: 120", "    connect_timeout: 60", "    tools:", "      resources: true", "      prompts: true"])
    if not mcp_seen:
        if kept and kept[-1].strip():
            kept.append("")
        kept.append("mcp_servers:")
        kept.extend(block)
    else:
        insert_at = len(kept)
        for index, line in enumerate(kept):
            if line.strip() == "mcp_servers:":
                insert_at = index + 1
                while insert_at < len(kept) and (kept[insert_at].startswith((" ", "\t")) or not kept[insert_at].strip()):
                    insert_at += 1
                break
        kept[insert_at:insert_at] = block
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(kept).rstrip() + "\n")
    if env_path:
        Path(env_path).touch(mode=0o600, exist_ok=True)
print(path)
PY
}

write_standard_client_config() {
  local client="$1"
  local root="$2"
  local uv_bin="$3"
  local paths config_path env_path payload
  paths="$(client_paths "$client" "$root")"
  config_path="$(printf '%s\n' "$paths" | sed -n '1p')"
  env_path="$(printf '%s\n' "$paths" | sed -n '2p')"
  if [ "$client" = "hermes-agent" ]; then
    write_env_file "$env_path"
    payload="$(build_standard_payload_json "$root" "$uv_bin" '${ICLOUD_APPLE_ID}' '${ICLOUD_APP_PASSWORD}' '${ICLOUD_MCP_SYNC_ON_START}')"
  else
    payload="$(build_standard_payload_json "$root" "$uv_bin" "$ICLOUD_SETUP_APPLE_ID" "$ICLOUD_SETUP_APP_PASSWORD" "$ICLOUD_SETUP_SYNC_ON_START")"
  fi
  write_client_config_python "$client" "$config_path" "$env_path" "$payload"
}

write_docker_client_config() {
  local client="$1"
  local root="$2"
  local paths config_path env_path payload
  paths="$(client_paths "$client" "$root")"
  config_path="$(printf '%s\n' "$paths" | sed -n '1p')"
  env_path="$(printf '%s\n' "$paths" | sed -n '2p')"
  payload="$(build_docker_payload_json)"
  write_client_config_python "$client" "$config_path" "$env_path" "$payload"
}

setup_standard() {
  local client="$1"
  local label root uv_bin
  label="$(client_label "$client")"
  setup_title "$label MCP setup" 6

  setup_step "Finding project and runtimes"
  root="$(repo_root)"
  uv_bin="$(find_uv)"
  PYTHON_BIN="$(find_python)"
  export PYTHON_BIN
  setup_ok "Using project: $root"
  setup_path "uv: $uv_bin"

  setup_step "Collecting setup choices"
  prompt_credentials
  prompt_scope
  prompt_password_storage
  prompt_sync_on_start
  setup_ok "Scope: $ICLOUD_SETUP_SCOPE"

  setup_step "Checking iCloud MCP server"
  ensure_project_ready "$root" "$uv_bin"
  setup_ok "Server imports successfully"

  setup_step "Saving credentials"
  store_keychain_credentials "$root" "$uv_bin"
  if [ "${ICLOUD_SETUP_PERSIST_APP_PASSWORD:-false}" = "true" ]; then
    setup_ok "Password will be provided through client env"
  else
    setup_ok "Password stored in OS keychain"
  fi

  setup_step "Writing $label config"
  config_path="$(write_standard_client_config "$client" "$root" "$uv_bin")"
  setup_ok "$label config updated"
  setup_path "$config_path"

  setup_step "Verifying credentials"
  verify_runtime_credentials "$root" "$uv_bin"
  setup_ok "Credentials load successfully"
}

setup_all() {
  setup_title "All MCP client setup" 4
  setup_step "Collecting shared setup choices"
  prompt_credentials
  prompt_scope
  prompt_password_storage
  prompt_sync_on_start

  setup_step "Installing Codex"
  setup_standard codex
  setup_step "Installing Claude Code"
  setup_standard claude-code
  setup_step "Installing Hermes Agent"
  setup_standard hermes-agent
}

setup_docker() {
  setup_title "Docker Compose MCP setup" 6

  setup_step "Finding project and Docker"
  ROOT="$(repo_root)"
  PYTHON_BIN="$(find_python)"
  export PYTHON_BIN
  require_command docker
  setup_ok "Using project: $ROOT"

  setup_step "Choosing MCP client"
  DOCKER_AGENT="$(pick_docker_agent)"
  prompt_scope
  setup_ok "Client: $DOCKER_AGENT"
  setup_ok "Scope: $ICLOUD_SETUP_SCOPE"

  setup_step "Collecting Docker credentials"
  prompt_credentials
  ICLOUD_SETUP_SYNC_ON_START="${ICLOUD_SETUP_SYNC_ON_START:-true}"
  ICLOUD_SETUP_PERSIST_APP_PASSWORD=true
  export ICLOUD_SETUP_SYNC_ON_START ICLOUD_SETUP_PERSIST_APP_PASSWORD
  setup_ok "Docker will use ICLOUD_APP_PASSWORD from Compose env"

  setup_step "Writing Compose .env"
  ENV_PATH="$ROOT/.env"
  write_env_file "$ENV_PATH"
  add_git_exclude "$ROOT" ".env"
  setup_ok "Compose env updated"
  setup_path "$ENV_PATH"

  setup_step "Writing Docker MCP client config"
  CONFIG_PATH="$(write_docker_client_config "$DOCKER_AGENT" "$ROOT")"
  setup_ok "Docker MCP config updated"
  setup_path "$CONFIG_PATH"

  setup_step "Building Docker image"
  (cd "$ROOT" && docker compose build)
  setup_ok "Docker image built"

  setup_step "Starting Docker Compose"
  (cd "$ROOT" && docker compose up -d)
  setup_ok "Docker container is running in the background"
}

main() {
  local target="${1:-}"
  if [ -z "$target" ]; then
    target="$(pick_agent)"
  fi

  case "$target" in
    codex) setup_standard codex ;;
    claude|claude-code) setup_standard claude-code ;;
    hermes|hermes-agent) setup_standard hermes-agent ;;
    all) setup_all ;;
    docker) setup_docker ;;
    -h|--help|help) printf 'Usage: %s [codex|claude-code|hermes-agent|all|docker]\n' "$0" ;;
    *) printf 'Unknown target: %s\n' "$target" >&2; return 1 ;;
  esac
}

main "$@"
