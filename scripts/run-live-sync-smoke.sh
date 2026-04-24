#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [ -z "$ICLOUD_APPLE_ID" ] || [ -z "$ICLOUD_APP_PASSWORD" ]; then
  printf 'Missing ICLOUD_APPLE_ID or ICLOUD_APP_PASSWORD in environment.\n' >&2
  printf 'Add exported values to ~/.zshrc, then open a new terminal or run: source ~/.zshrc\n' >&2
  exit 1
fi

export ICLOUD_APPLE_ID
export ICLOUD_APP_PASSWORD
export ICLOUD_MCP_SYNC_ON_START=false
export ICLOUD_MCP_DATABASE_PATH="${ICLOUD_MCP_DATABASE_PATH:-${ROOT_DIR}/.tmp/icloud-live-smoke.sqlite3}"

mkdir -p "$(dirname "$ICLOUD_MCP_DATABASE_PATH")"

cd "$ROOT_DIR"
uv run python - <<'PY'
from __future__ import annotations

import json

from icloud_mcp.config import Settings
from icloud_mcp.db.connection import open_db
from icloud_mcp.db.repositories import ensure_defaults, sync_status
from icloud_mcp.sync.scheduler import SyncScheduler

settings = Settings.from_env()
db = open_db(settings.database_path)
ensure_defaults(db, settings)

try:
    result = SyncScheduler(db, settings).sync_now()
    status = sync_status(db, settings.stale_after_seconds)
    print("sync_now:")
    print(json.dumps(result, indent=2, sort_keys=True))
    print("sync_status:")
    print(json.dumps(status, indent=2, sort_keys=True))
finally:
    db.close()
PY
