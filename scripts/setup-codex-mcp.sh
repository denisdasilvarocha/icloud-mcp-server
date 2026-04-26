#!/usr/bin/env bash

set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)/lib/icloud_mcp_setup.sh"

main() {
  setup_title "Codex MCP setup" 6

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

  setup_step "Writing Codex config"
  if [ "$ICLOUD_SETUP_SCOPE" = "project" ]; then
    CONFIG_PATH="$ROOT/.codex/config.toml"
    mkdir -p "$(dirname "$CONFIG_PATH")"
    add_git_exclude "$ROOT" ".codex/config.toml"
  else
    CONFIG_PATH="$HOME/.codex/config.toml"
    mkdir -p "$(dirname "$CONFIG_PATH")"
  fi

  PAYLOAD="$(build_payload_json "$ROOT" "$UV_BIN" "$ICLOUD_SETUP_APPLE_ID" "$ICLOUD_SETUP_APP_PASSWORD" "$ICLOUD_SETUP_SYNC_ON_START")"
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
skip = False
for line in lines:
    if line.strip() == "[mcp_servers.icloud]":
        skip = True
        continue
    if skip and line.startswith("[") and line.strip().endswith("]"):
        skip = False
    if not skip:
        kept.append(line)

if kept and kept[-1].strip():
    kept.append("")
kept.extend(
    [
        "[mcp_servers.icloud]",
        f"command = {json.dumps(payload['command'])}",
        f"args = {json.dumps(payload['args'])}",
        f"cwd = {json.dumps(payload['cwd'])}",
        "enabled = true",
        "startup_timeout_sec = 30",
        "tool_timeout_sec = 120",
        "[mcp_servers.icloud.env]",
    ]
)
for key, value in payload["env"].items():
    kept.append(f"{key} = {json.dumps(value)}")
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text("\n".join(kept).rstrip() + "\n")
PY

  setup_step "Verifying Codex config and credentials"
  verify_codex_config "$CONFIG_PATH"
  verify_runtime_credentials "$ROOT" "$UV_BIN"
  setup_ok "Codex config contains the icloud MCP server"
  print_done "Codex" "$CONFIG_PATH"
  print_next_steps "Codex" "$CONFIG_PATH"
}

main "$@"
