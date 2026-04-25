#!/usr/bin/env bash

set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)/lib/icloud_mcp_setup.sh"

main() {
  ROOT="$(repo_root)"
  UV_BIN="$(find_uv)"
  PYTHON_BIN="$(find_python)"
  export PYTHON_BIN

  prompt_credentials
  prompt_scope
  prompt_sync_on_start
  ensure_project_ready "$ROOT" "$UV_BIN"
  store_keychain_credentials "$ROOT" "$UV_BIN"

  if [ "$ICLOUD_SETUP_SCOPE" = "project" ]; then
    CONFIG_PATH="$ROOT/.codex/config.toml"
    mkdir -p "$(dirname "$CONFIG_PATH")"
    add_git_exclude "$ROOT" ".codex/config.toml"
  else
    CONFIG_PATH="$HOME/.codex/config.toml"
    mkdir -p "$(dirname "$CONFIG_PATH")"
  fi

  PAYLOAD="$(build_payload_json "$ROOT" "$UV_BIN" "$ICLOUD_SETUP_APPLE_ID" "$ICLOUD_SETUP_APP_PASSWORD" "$ICLOUD_SETUP_SYNC_ON_START")"
  PAYLOAD_JSON="$PAYLOAD" "$UV_BIN" run --with tomlkit python - "$CONFIG_PATH" <<'PY'
import json
import os
import sys
from pathlib import Path

import tomlkit

path = Path(sys.argv[1])
payload = json.loads(os.environ["PAYLOAD_JSON"])
if path.exists() and path.read_text().strip():
    doc = tomlkit.parse(path.read_text())
else:
    doc = tomlkit.document()

mcp_servers = doc.get("mcp_servers")
if mcp_servers is None:
    mcp_servers = tomlkit.table()
    doc["mcp_servers"] = mcp_servers

server = tomlkit.table()
server["command"] = payload["command"]
server["args"] = payload["args"]
server["cwd"] = payload["cwd"]
server["enabled"] = True
server["startup_timeout_sec"] = 30
server["tool_timeout_sec"] = 120

env = tomlkit.table()
for key, value in payload["env"].items():
    env[key] = value
server["env"] = env

mcp_servers[payload["name"]] = server
path.write_text(tomlkit.dumps(doc))
PY

  print_done "Codex" "$CONFIG_PATH"
}

main "$@"
