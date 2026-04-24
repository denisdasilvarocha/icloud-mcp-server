#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [ -z "${ICLOUD_APPLE_ID:-}" ] || [ -z "${ICLOUD_APP_PASSWORD:-}" ]; then
  printf 'Missing ICLOUD_APPLE_ID or ICLOUD_APP_PASSWORD in environment.\n' >&2
  printf 'Add exported values to ~/.zshrc, then open a new terminal or run: source ~/.zshrc\n' >&2
  exit 1
fi

export ICLOUD_APPLE_ID
export ICLOUD_APP_PASSWORD
export ICLOUD_MCP_LIVE_TESTS=1
export ICLOUD_MCP_SYNC_ON_START=false
export ICLOUD_MCP_MAIL_SYNC_DAYS="${ICLOUD_MCP_MAIL_SYNC_DAYS:-7}"
export ICLOUD_MCP_MAIL_SYNC_LIMIT_PER_MAILBOX="${ICLOUD_MCP_MAIL_SYNC_LIMIT_PER_MAILBOX:-25}"
export ICLOUD_MCP_CALENDAR_PAST_MONTHS="${ICLOUD_MCP_CALENDAR_PAST_MONTHS:-1}"
export ICLOUD_MCP_CALENDAR_FUTURE_MONTHS="${ICLOUD_MCP_CALENDAR_FUTURE_MONTHS:-3}"

mkdir -p "${ROOT_DIR}/.tmp"
if [ -z "${ICLOUD_MCP_DATABASE_PATH:-}" ]; then
  tmp_db_base="$(mktemp "${ROOT_DIR}/.tmp/icloud-live-smoke-XXXXXX")"
  rm -f "$tmp_db_base"
  ICLOUD_MCP_DATABASE_PATH="${tmp_db_base}.sqlite3"
fi
export ICLOUD_MCP_DATABASE_PATH

mkdir -p "$(dirname "$ICLOUD_MCP_DATABASE_PATH")"

cd "$ROOT_DIR"
printf 'Using live smoke database: %s\n' "$ICLOUD_MCP_DATABASE_PATH"
uv run python -m unittest discover -s tests/integration -p 'test_live_*.py'
