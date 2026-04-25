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
  PAYLOAD_JSON="$PAYLOAD" "$UV_BIN" run --with pyyaml python - "$CONFIG_PATH" <<'PY'
import json
import os
import sys
from pathlib import Path

import yaml

path = Path(sys.argv[1])
payload = json.loads(os.environ["PAYLOAD_JSON"])
if path.exists() and path.read_text().strip():
    data = yaml.safe_load(path.read_text()) or {}
else:
    data = {}

data.setdefault("mcp_servers", {})[payload["name"]] = {
    "command": payload["command"],
    "args": payload["args"],
    "env": payload["env"],
    "enabled": True,
    "timeout": 120,
    "connect_timeout": 60,
    "tools": {
        "resources": True,
        "prompts": True,
    },
}
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(yaml.safe_dump(data, sort_keys=False))
PY

  print_done "Hermes Agent" "$CONFIG_PATH"
  printf 'Hermes secrets written: %s\n' "$ENV_PATH"
  if [ "$ICLOUD_SETUP_SCOPE" = "project" ]; then
    printf 'Project Hermes config uses HERMES_HOME. Run Hermes with:\n'
    printf '  HERMES_HOME="%s" hermes\n' "$HERMES_HOME_DIR"
  fi
}

main "$@"
