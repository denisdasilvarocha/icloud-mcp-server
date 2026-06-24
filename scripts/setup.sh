#!/usr/bin/env bash

set -Eeuo pipefail
IFS=$'\n\t'

readonly SERVER_NAME="icloud"
readonly PROGRESS_WIDTH=24

SETUP_STEP=0
SETUP_TOTAL=1
SETUP_CURRENT_STEP="startup"
PYTHON_BIN=""

if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
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

# ---------- Output helpers -------------------------------------------------

say() {
  printf '%s\n' "$*" >&2
}

note() {
  printf '  %s%s%s\n' "$COLOR_DIM" "$*" "$COLOR_RESET" >&2
}

ok() {
  printf '  %sOK%s %s\n' "$COLOR_GREEN" "$COLOR_RESET" "$*" >&2
}

warn() {
  printf '  %sWarning:%s %s\n' "$COLOR_YELLOW" "$COLOR_RESET" "$*" >&2
}

fail() {
  printf '%sError:%s %s\n' "$COLOR_RED" "$COLOR_RESET" "$*" >&2
  return 1
}

setup_error() {
  local exit_code=$?
  trap - ERR
  printf '\n%sSetup failed.%s Last step: %s\n' "$COLOR_RED" "$COLOR_RESET" "${SETUP_CURRENT_STEP:-startup}" >&2
  printf 'Fix the message above, then run this setup again.\n' >&2
  exit "$exit_code"
}

trap setup_error ERR

repeat_char() {
  local count="$1"
  local char="$2"

  if [ "$count" -le 0 ]; then
    return
  fi

  printf '%*s' "$count" '' | tr ' ' "$char"
}

setup_title() {
  local title="$1"
  local total="$2"
  local summary="${3:-Configures the iCloud MCP server and updates your selected MCP client.}"

  SETUP_TOTAL="$total"
  SETUP_STEP=0
  SETUP_CURRENT_STEP="startup"

  printf '\n%s%s%s\n' "$COLOR_BOLD" "$title" "$COLOR_RESET" >&2
  printf '%s%s%s\n\n' "$COLOR_DIM" "$summary" "$COLOR_RESET" >&2
}

setup_step() {
  local label="$1"
  local filled_count empty_count filled empty

  SETUP_STEP=$((SETUP_STEP + 1))
  SETUP_CURRENT_STEP="$label"

  filled_count=$((SETUP_STEP * PROGRESS_WIDTH / SETUP_TOTAL))
  if [ "$filled_count" -gt "$PROGRESS_WIDTH" ]; then
    filled_count="$PROGRESS_WIDTH"
  fi
  empty_count=$((PROGRESS_WIDTH - filled_count))

  filled="$(repeat_char "$filled_count" '#')"
  empty="$(repeat_char "$empty_count" '.')"

  printf '%s[%s%s] %s/%s%s %s\n' \
    "$COLOR_BLUE" "$filled" "$empty" "$SETUP_STEP" "$SETUP_TOTAL" "$COLOR_RESET" "$label" >&2
}

usage() {
  cat >&2 <<USAGE
Usage:
  $(basename "$0") [target]

Targets:
  docker         Configure a client to launch the server through Docker Compose
  codex          Configure Codex
  claude-code    Configure Claude Code
  hermes-agent   Configure Hermes Agent

Aliases:
  claude         Same as claude-code
  hermes         Same as hermes-agent

Environment overrides:
  ICLOUD_SETUP_APPLE_ID                iCloud email / Apple ID
  ICLOUD_SETUP_APP_PASSWORD            Apple app-specific password
  ICLOUD_SETUP_SCOPE                   project | global
  ICLOUD_SETUP_PERSIST_APP_PASSWORD    true = write password to env/client config, false = use OS keychain
  ICLOUD_SETUP_SYNC_ON_START           true | false

Examples:
  $(basename "$0") docker
  $(basename "$0") codex
  ICLOUD_SETUP_SCOPE=global $(basename "$0") claude-code
USAGE
}

# ---------- Generic helpers ------------------------------------------------

is_interactive() {
  [ -t 0 ]
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
    fail "Missing required command: $name"
  fi
}

find_uv() {
  if command -v uv >/dev/null 2>&1; then
    command -v uv
    return
  fi

  fail "Missing uv. Install it first: https://docs.astral.sh/uv/"
}

find_python() {
  if command -v python3.12 >/dev/null 2>&1; then
    command -v python3.12
  elif command -v python3 >/dev/null 2>&1; then
    command -v python3
  else
    fail "Missing python3. Install Python 3.12 or any compatible python3."
  fi
}

normalize_bool() {
  case "$1" in
    true|TRUE|True|1|yes|YES|Yes|y|Y) printf 'true\n' ;;
    false|FALSE|False|0|no|NO|No|n|N) printf 'false\n' ;;
    *) return 1 ;;
  esac
}

normalize_bool_var() {
  local var_name="$1"
  local label="$2"
  local raw="${!var_name:-}"
  local normalized

  if [ -z "$raw" ]; then
    return
  fi

  if ! normalized="$(normalize_bool "$raw")"; then
    fail "$label must be true or false; got: $raw"
  fi

  printf -v "$var_name" '%s' "$normalized"
  export "$var_name"
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

require_compose_file() {
  local root="$1"

  if [ -f "$root/compose.yaml" ] || [ -f "$root/compose.yml" ] || [ -f "$root/docker-compose.yaml" ] || [ -f "$root/docker-compose.yml" ]; then
    return
  fi

  fail "No Docker Compose file found in $root"
}

# ---------- Prompts --------------------------------------------------------

prompt_required_value() {
  local var_name="$1"
  local label="$2"
  local help_text="$3"
  local value

  if [ -n "${!var_name:-}" ]; then
    export "$var_name"
    return
  fi

  if ! is_interactive; then
    fail "$label is required. Set $var_name before running non-interactively."
  fi

  printf '\n%s\n' "$label" >&2
  note "$help_text"

  read -r -p "  > " value

  if [ -z "$value" ]; then
    fail "$label cannot be empty."
  fi

  printf -v "$var_name" '%s' "$value"
  export "$var_name"
}

prompt_credentials() {
  prompt_required_value \
    ICLOUD_SETUP_APPLE_ID \
    "Apple ID / iCloud email" \
    "Use the iCloud email address for the account this MCP server should sync."

  prompt_required_value \
    ICLOUD_SETUP_APP_PASSWORD \
    "Apple app-specific password" \
    "Use an app-specific password, not your Apple ID login password."
}

prompt_scope() {
  local choice

  case "${ICLOUD_SETUP_SCOPE:-}" in
    project|global) export ICLOUD_SETUP_SCOPE; return ;;
    "") ;;
    *) fail "ICLOUD_SETUP_SCOPE must be project or global; got: $ICLOUD_SETUP_SCOPE" ;;
  esac

  if ! is_interactive; then
    fail "Config scope is required. Set ICLOUD_SETUP_SCOPE=project or ICLOUD_SETUP_SCOPE=global."
  fi

  printf '\nWhere should the MCP client config be written?\n' >&2
  note "Global scope makes the server available from any directory for that client."
  note "Project scope keeps config in this repo and ignores sensitive files where needed."
  printf '  1) Global user config  %s(recommended)%s\n' "$COLOR_DIM" "$COLOR_RESET" >&2
  printf '  2) Project config\n' >&2

  while :; do
    read -r -p "  Choice [1/2, default 1]: " choice
    case "${choice:-1}" in
      1) ICLOUD_SETUP_SCOPE="global"; break ;;
      2) ICLOUD_SETUP_SCOPE="project"; break ;;
      *) warn "Please choose 1 or 2." ;;
    esac
  done

  export ICLOUD_SETUP_SCOPE
}

prompt_password_storage() {
  local answer default_choice default_hint system_name

  normalize_bool_var ICLOUD_SETUP_PERSIST_APP_PASSWORD "ICLOUD_SETUP_PERSIST_APP_PASSWORD"
  if [ -n "${ICLOUD_SETUP_PERSIST_APP_PASSWORD:-}" ]; then
    return
  fi

  if ! is_interactive; then
    fail "Credential storage choice is required. Set ICLOUD_SETUP_PERSIST_APP_PASSWORD=true or false."
  fi

  system_name="$(uname -s 2>/dev/null || true)"
  default_choice="yes"
  default_hint="Y/n"

  if [ "$system_name" = "Linux" ]; then
    default_choice="no"
    default_hint="y/N"
  fi

  printf '\nCredential storage\n' >&2
  note "Yes: store the password in the OS keychain when supported. Best for desktop macOS."
  note "No: write the password to a chmod 600 env file or client env. Best for Linux/headless launches."

  while :; do
    read -r -p "  Use the OS keychain when possible? [$default_hint]: " answer
    case "${answer:-$default_choice}" in
      y|Y|yes|YES|Yes)
        ICLOUD_SETUP_PERSIST_APP_PASSWORD="false"
        break
        ;;
      n|N|no|NO|No)
        ICLOUD_SETUP_PERSIST_APP_PASSWORD="true"
        break
        ;;
      *) warn "Please answer yes or no." ;;
    esac
  done

  export ICLOUD_SETUP_PERSIST_APP_PASSWORD
}

prompt_sync_on_start() {
  local answer

  normalize_bool_var ICLOUD_SETUP_SYNC_ON_START "ICLOUD_SETUP_SYNC_ON_START"
  if [ -n "${ICLOUD_SETUP_SYNC_ON_START:-}" ]; then
    return
  fi

  if ! is_interactive; then
    fail "Initial sync choice is required. Set ICLOUD_SETUP_SYNC_ON_START=true or false."
  fi

  printf '\nInitial sync behavior\n' >&2
  note "Yes gives useful search results immediately after the MCP server starts."
  note "No starts faster and uses the existing local cache until you run a sync manually."

  while :; do
    read -r -p "  Sync iCloud when the MCP server starts? [Y/n]: " answer
    case "${answer:-Y}" in
      y|Y|yes|YES|Yes)
        ICLOUD_SETUP_SYNC_ON_START="true"
        break
        ;;
      n|N|no|NO|No)
        ICLOUD_SETUP_SYNC_ON_START="false"
        break
        ;;
      *) warn "Please answer yes or no." ;;
    esac
  done

  export ICLOUD_SETUP_SYNC_ON_START
}

pick_agent() {
  local choice

  printf 'Which agent/runtime should be configured?\n' >&2
  printf '  1) Docker Compose %s(recommended)%s\n' "$COLOR_DIM" "$COLOR_RESET" >&2
  printf '  2) Codex\n' >&2
  printf '  3) Claude Code\n' >&2
  printf '  4) Hermes Agent\n' >&2

  while :; do
    read -r -p "Choice [1-4, default 1]: " choice
    case "${choice:-1}" in
      1) printf 'docker\n'; return ;;
      2) printf 'codex\n'; return ;;
      3) printf 'claude-code\n'; return ;;
      4) printf 'hermes-agent\n'; return ;;
      *) warn "Please choose a number from 1 to 4." ;;
    esac
  done
}

pick_docker_agent() {
  local choice

  printf '\nWhich MCP client should use Docker Compose?\n' >&2
  printf '  1) Codex\n' >&2
  printf '  2) Claude Code\n' >&2
  printf '  3) Hermes Agent\n' >&2

  while :; do
    read -r -p "Choice [1-3, default 1]: " choice
    case "${choice:-1}" in
      1) printf 'codex\n'; return ;;
      2) printf 'claude-code\n'; return ;;
      3) printf 'hermes-agent\n'; return ;;
      *) warn "Please choose a number from 1 to 3." ;;
    esac
  done
}

client_label() {
  case "$1" in
    codex) printf 'Codex\n' ;;
    claude-code) printf 'Claude Code\n' ;;
    hermes-agent) printf 'Hermes Agent\n' ;;
    *) printf '%s\n' "$1" ;;
  esac
}

# ---------- iCloud MCP checks and credentials -----------------------------

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

# ---------- Config payloads and file writers -------------------------------

build_standard_payload_json() {
  local root="$1"
  local uv_bin="$2"
  local apple_id="$3"
  local app_password="$4"
  local sync_on_start="$5"

  if [ "${ICLOUD_SETUP_PERSIST_APP_PASSWORD:-false}" != "true" ]; then
    app_password=""
  fi

  "$PYTHON_BIN" - "$SERVER_NAME" "$root" "$uv_bin" "$apple_id" "$app_password" "$sync_on_start" <<'PY'
import json
import sys

name, root, uv_bin, apple_id, app_password, sync_on_start = sys.argv[1:]
env = {
    "ICLOUD_APPLE_ID": apple_id,
    "ICLOUD_MCP_SYNC_ON_START": sync_on_start,
}
if app_password:
    env["ICLOUD_APP_PASSWORD"] = app_password

print(json.dumps({
    "name": name,
    "command": uv_bin,
    "args": ["run", "--project", root, "icloud-mcp"],
    "cwd": root,
    "env": env,
}))
PY
}

build_docker_payload_json() {
  "$PYTHON_BIN" - "$SERVER_NAME" <<'PY'
import json
import sys

name = sys.argv[1]
print(json.dumps({
    "name": name,
    "command": "docker",
    "args": ["exec", "-i", "icloud-mcp-server", "icloud-mcp"],
    "env": {},
}))
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
    escaped = (
        value
        .replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("$", "\\$")
        .replace("`", "\\`")
    )
    lines.append(f'{key}="{escaped}"')

path.write_text("\n".join(lines).rstrip() + "\n")
PY
}

client_paths() {
  local client="$1"
  local root="$2"
  local home="${HOME:-}"
  local config_path=""
  local env_path=""

  if [ -z "$home" ]; then
    fail "HOME is not set; cannot locate global client config."
  fi

  case "$client" in
    codex)
      if [ "$ICLOUD_SETUP_SCOPE" = "project" ]; then
        config_path="$root/.codex/config.toml"
        add_git_exclude "$root" ".codex/config.toml"
      else
        config_path="$home/.codex/config.toml"
      fi
      mkdir -p "$(dirname "$config_path")"
      ;;

    claude-code)
      if [ "$ICLOUD_SETUP_SCOPE" = "project" ]; then
        config_path="$root/.mcp.json"
        add_git_exclude "$root" ".mcp.json"
      else
        config_path="$home/.claude.json"
      fi
      mkdir -p "$(dirname "$config_path")"
      ;;

    hermes-agent)
      if [ "$ICLOUD_SETUP_SCOPE" = "project" ]; then
        config_path="$root/.hermes/config.yaml"
        env_path="$root/.hermes/.env"
        add_git_exclude "$root" ".hermes/"
      else
        config_path="$home/.hermes/config.yaml"
        env_path="$home/.hermes/.env"
      fi
      mkdir -p "$(dirname "$config_path")"
      touch "$env_path"
      chmod 600 "$env_path"
      ;;

    *) fail "Unknown MCP client: $client" ;;
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
name = payload["name"]


def remove_toml_server_block(lines, server_name):
    kept = []
    i = 0
    header = f"[mcp_servers.{server_name}]"
    nested_prefix = f"[mcp_servers.{server_name}."

    while i < len(lines):
        stripped = lines[i].strip()
        if stripped == header or stripped.startswith(nested_prefix):
            i += 1
            while i < len(lines):
                next_stripped = lines[i].strip()
                if (
                    next_stripped.startswith("[")
                    and next_stripped.endswith("]")
                    and not next_stripped.startswith(nested_prefix)
                ):
                    break
                i += 1
            continue

        kept.append(lines[i])
        i += 1

    return kept


def write_codex_config():
    lines = path.read_text().splitlines() if path.exists() else []
    kept = remove_toml_server_block(lines, name)

    if kept and kept[-1].strip():
        kept.append("")

    kept.extend([
        f"[mcp_servers.{name}]",
        f"command = {json.dumps(payload['command'])}",
        f"args = {json.dumps(payload['args'])}",
    ])

    if payload.get("cwd"):
        kept.append(f"cwd = {json.dumps(payload['cwd'])}")

    kept.extend([
        "enabled = true",
        "startup_timeout_sec = 30",
        "tool_timeout_sec = 120",
    ])

    if payload["env"]:
        kept.append(f"[mcp_servers.{name}.env]")
        for key, value in payload["env"].items():
            kept.append(f"{key} = {json.dumps(value)}")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(kept).rstrip() + "\n")


def write_claude_code_config():
    if path.exists() and path.read_text().strip():
        data = json.loads(path.read_text())
    else:
        data = {}

    data.setdefault("mcpServers", {})[name] = {
        "type": "stdio",
        "command": payload["command"],
        "args": payload["args"],
        "env": payload["env"],
    }

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def remove_hermes_server_block(lines, server_name):
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

        if in_mcp and line.startswith(f"  {server_name}:"):
            i += 1
            while i < len(lines):
                next_line = lines[i]
                if not next_line.strip():
                    i += 1
                    continue
                if not next_line.startswith((" ", "\t")) or (
                    next_line.startswith("  ") and not next_line.startswith("    ")
                ):
                    break
                i += 1
            continue

        kept.append(line)
        i += 1

    return kept, mcp_seen


def hermes_server_block():
    block = [
        f"  {name}:",
        f"    command: {json.dumps(payload['command'])}",
        "    args:",
        *[f"      - {json.dumps(arg)}" for arg in payload["args"]],
    ]

    if payload["env"]:
        block.extend(["    env:"])
        block.extend(f"      {key}: {json.dumps(value)}" for key, value in payload["env"].items())

    block.extend([
        "    enabled: true",
        "    timeout: 120",
        "    connect_timeout: 60",
        "    tools:",
        "      resources: true",
        "      prompts: true",
    ])
    return block


def write_hermes_config():
    lines = path.read_text().splitlines() if path.exists() else []
    kept, mcp_seen = remove_hermes_server_block(lines, name)
    block = hermes_server_block()

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
                while insert_at < len(kept) and (
                    kept[insert_at].startswith((" ", "\t")) or not kept[insert_at].strip()
                ):
                    insert_at += 1
                break
        kept[insert_at:insert_at] = block

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(kept).rstrip() + "\n")

    if env_path:
        env_file = Path(env_path)
        env_file.parent.mkdir(parents=True, exist_ok=True)
        env_file.touch(mode=0o600, exist_ok=True)


writers = {
    "codex": write_codex_config,
    "claude-code": write_claude_code_config,
    "hermes-agent": write_hermes_config,
}

try:
    writer = writers[client]
except KeyError as exc:
    raise SystemExit(f"unsupported client: {client}") from exc

writer()
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

# ---------- Setup flows ----------------------------------------------------

setup_standard() {
  local client="$1"
  local label root uv_bin config_path

  label="$(client_label "$client")"
  setup_title "$label MCP setup" 6

  setup_step "Finding project and runtimes"
  root="$(repo_root)"
  uv_bin="$(find_uv)"
  PYTHON_BIN="$(find_python)"
  export PYTHON_BIN
  ok "Using project: $root"
  note "uv: $uv_bin"
  note "python: $PYTHON_BIN"

  setup_step "Collecting setup choices"
  prompt_credentials
  prompt_scope
  prompt_password_storage
  prompt_sync_on_start
  ok "Scope: $ICLOUD_SETUP_SCOPE"

  setup_step "Checking iCloud MCP server"
  ensure_project_ready "$root" "$uv_bin"
  ok "Server imports successfully"

  setup_step "Saving credentials"
  store_keychain_credentials "$root" "$uv_bin"
  if [ "${ICLOUD_SETUP_PERSIST_APP_PASSWORD:-false}" = "true" ]; then
    ok "Password will be provided through the client environment"
  else
    ok "Password stored in the OS keychain"
  fi

  setup_step "Writing $label config"
  config_path="$(write_standard_client_config "$client" "$root" "$uv_bin")"
  ok "$label config updated"
  note "$config_path"

  setup_step "Verifying credentials"
  verify_runtime_credentials "$root" "$uv_bin"
  ok "Credentials load successfully"

  printf '\n%sDone.%s Restart %s so it can pick up the MCP server.\n' \
    "$COLOR_GREEN" "$COLOR_RESET" "$label" >&2
}

setup_docker() {
  local root docker_agent config_path env_path

  setup_title "Docker Compose MCP setup" 7 "Builds the iCloud MCP container and points one MCP client at it."

  setup_step "Finding project and Docker"
  root="$(repo_root)"
  PYTHON_BIN="$(find_python)"
  export PYTHON_BIN
  require_command docker
  docker compose version >/dev/null 2>&1 || fail "Docker is installed, but the 'docker compose' plugin is not available."
  require_compose_file "$root"
  ok "Using project: $root"
  note "python: $PYTHON_BIN"

  setup_step "Choosing MCP client"
  docker_agent="$(pick_docker_agent)"
  prompt_scope
  ok "Client: $(client_label "$docker_agent")"
  ok "Scope: $ICLOUD_SETUP_SCOPE"

  setup_step "Collecting Docker credentials"
  prompt_credentials
  ICLOUD_SETUP_SYNC_ON_START="${ICLOUD_SETUP_SYNC_ON_START:-true}"
  ICLOUD_SETUP_PERSIST_APP_PASSWORD="true"
  export ICLOUD_SETUP_SYNC_ON_START ICLOUD_SETUP_PERSIST_APP_PASSWORD
  ok "Docker will read credentials from the Compose env file"

  setup_step "Writing Compose .env"
  env_path="$root/.env"
  write_env_file "$env_path"
  add_git_exclude "$root" ".env"
  ok "Compose env updated"
  note "$env_path"

  setup_step "Writing Docker MCP client config"
  config_path="$(write_docker_client_config "$docker_agent" "$root")"
  ok "Docker MCP client config updated"
  note "$config_path"

  setup_step "Building Docker image"
  (cd "$root" && docker compose build)
  ok "Docker image built"

  setup_step "Starting Docker Compose"
  (cd "$root" && docker compose up -d)
  ok "Docker container is running"

  printf '\n%sDone.%s Restart %s so it can pick up the Docker-backed MCP server.\n' \
    "$COLOR_GREEN" "$COLOR_RESET" "$(client_label "$docker_agent")" >&2
}

main() {
  local target="${1:-}"

  case "$target" in
    -h|--help|help)
      usage
      return 0
      ;;
    "")
      if ! is_interactive; then
        usage
        fail "Target is required when running non-interactively."
      fi
      target="$(pick_agent)"
      ;;
  esac

  case "$target" in
    codex) setup_standard codex ;;
    claude|claude-code) setup_standard claude-code ;;
    hermes|hermes-agent) setup_standard hermes-agent ;;
    docker) setup_docker ;;
    *) usage; fail "Unknown target: $target" ;;
  esac
}

main "$@"
