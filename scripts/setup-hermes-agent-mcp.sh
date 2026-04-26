#!/usr/bin/env bash

set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)/lib/icloud_mcp_setup.sh"

main() {
  setup_title "Hermes Agent MCP setup" 6

  setup_step "Finding project and runtimes"
  ROOT="$(repo_root)"
  UV_BIN="$(find_uv)"
  PYTHON_BIN="$(find_python)"
  export PYTHON_BIN
  setup_ok "Using project: $ROOT"
  setup_path "uv: $UV_BIN"

  setup_step "Collecting setup choices"
  prompt_credentials
  prompt_scope
  prompt_password_storage
  prompt_sync_on_start
  setup_ok "Scope: $ICLOUD_SETUP_SCOPE"

  setup_step "Checking iCloud MCP server"
  ensure_project_ready "$ROOT" "$UV_BIN"
  setup_ok "Server imports successfully"

  setup_step "Saving credentials"
  store_keychain_credentials "$ROOT" "$UV_BIN"
  if [ "${ICLOUD_SETUP_PERSIST_APP_PASSWORD:-false}" = "true" ]; then
    setup_ok "Password will be written to Hermes .env"
  else
    setup_ok "Password stored in OS keychain"
  fi

  setup_step "Writing Hermes config"
  if [ "$ICLOUD_SETUP_SCOPE" = "project" ]; then
    HERMES_HOME_DIR="$ROOT/.hermes"
    CONFIG_PATH="$HERMES_HOME_DIR/config.yaml"
    ENV_PATH="$HERMES_HOME_DIR/.env"
    add_git_exclude "$ROOT" ".hermes/"
  else
    HERMES_HOME_DIR="$HOME/.hermes"
    CONFIG_PATH="$HERMES_HOME_DIR/config.yaml"
    ENV_PATH="$HERMES_HOME_DIR/.env"
  fi
  mkdir -p "$HERMES_HOME_DIR"
  write_env_file "$ENV_PATH"

  PAYLOAD="$(build_payload_json "$ROOT" "$UV_BIN" '${ICLOUD_APPLE_ID}' '${ICLOUD_APP_PASSWORD}' '${ICLOUD_MCP_SYNC_ON_START}')"
  PAYLOAD_JSON="$PAYLOAD" "$PYTHON_BIN" - "$CONFIG_PATH" <<'PY'
import json
import os
import sys
from pathlib import Path

path = Path(sys.argv[1])
payload = json.loads(os.environ["PAYLOAD_JSON"])
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
    "    env:",
    *[f"      {key}: {json.dumps(value)}" for key, value in payload["env"].items()],
    "    enabled: true",
    "    timeout: 120",
    "    connect_timeout: 60",
    "    tools:",
    "      resources: true",
    "      prompts: true",
]

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
PY

  setup_step "Verifying Hermes config and credentials"
  verify_hermes_config "$CONFIG_PATH" "$ENV_PATH" "$UV_BIN"
  verify_runtime_credentials "$ROOT" "$UV_BIN"
  setup_ok "Hermes config contains the icloud MCP server"
  print_done "Hermes Agent" "$CONFIG_PATH"
  setup_ok "Hermes env file updated"
  setup_path "$ENV_PATH"
  if [ "$ICLOUD_SETUP_SCOPE" = "project" ]; then
    print_next_steps "Hermes Agent" "$CONFIG_PATH" "$ENV_PATH"
    printf '  Project config selected. Start Hermes with:\n'
    printf '  %sHERMES_HOME="%s" hermes%s\n' "$COLOR_BOLD" "$HERMES_HOME_DIR" "$COLOR_RESET"
  else
    print_next_steps "Hermes Agent" "$CONFIG_PATH" "$ENV_PATH"
  fi
}

main "$@"
