#!/usr/bin/env bash

set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)/lib/icloud_mcp_setup.sh"

main() {
  setup_title "Claude Code MCP setup" 6

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
    setup_ok "Password will be provided through client env"
  else
    setup_ok "Password stored in OS keychain"
  fi

  setup_step "Writing Claude Code config"
  if [ "$ICLOUD_SETUP_SCOPE" = "project" ]; then
    CONFIG_PATH="$ROOT/.mcp.json"
    add_git_exclude "$ROOT" ".mcp.json"
  else
    CONFIG_PATH="$HOME/.claude.json"
  fi

  PAYLOAD="$(build_payload_json "$ROOT" "$UV_BIN" "$ICLOUD_SETUP_APPLE_ID" "$ICLOUD_SETUP_APP_PASSWORD" "$ICLOUD_SETUP_SYNC_ON_START")"
  PAYLOAD_JSON="$PAYLOAD" "$PYTHON_BIN" - "$CONFIG_PATH" <<'PY'
import json
import os
import sys
from pathlib import Path

path = Path(sys.argv[1])
payload = json.loads(os.environ["PAYLOAD_JSON"])
if path.exists() and path.read_text().strip():
    data = json.loads(path.read_text())
else:
    data = {}

server = {
    "type": "stdio",
    "command": payload["command"],
    "args": payload["args"],
    "env": payload["env"],
}
data.setdefault("mcpServers", {})[payload["name"]] = server
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
PY

  setup_step "Verifying Claude Code config and credentials"
  verify_claude_config "$CONFIG_PATH"
  verify_runtime_credentials "$ROOT" "$UV_BIN"
  setup_ok "Claude Code config contains the icloud MCP server"
  print_done "Claude Code" "$CONFIG_PATH"
  if command -v claude >/dev/null 2>&1; then
    claude mcp list >/dev/null 2>&1 || true
  fi
  print_next_steps "Claude Code" "$CONFIG_PATH"
}

main "$@"
