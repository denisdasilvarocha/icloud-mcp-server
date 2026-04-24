#!/usr/bin/env bash

set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)/lib/icloud_mcp_setup.sh"

ROOT="$(repo_root)"
PYTHON_BIN="$(find_python)"
export PYTHON_BIN

prompt_credentials
prompt_scope
prompt_sync_on_start

export ICLOUD_SETUP_APPLE_ID ICLOUD_SETUP_APP_PASSWORD ICLOUD_SETUP_SCOPE ICLOUD_SETUP_SYNC_ON_START

"$ROOT/scripts/setup-codex-mcp.sh"
"$ROOT/scripts/setup-claude-code-mcp.sh"
"$ROOT/scripts/setup-hermes-agent-mcp.sh"

printf 'All requested MCP client configs updated.\n'
