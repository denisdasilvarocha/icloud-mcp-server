#!/usr/bin/env bash

set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)/lib/icloud_mcp_setup.sh"

ROOT="$(repo_root)"
PYTHON_BIN="$(find_python)"
export PYTHON_BIN

setup_title "All MCP client setup" 5

setup_step "Finding project"
setup_ok "Using project: $ROOT"

setup_step "Collecting shared setup choices"
prompt_credentials
prompt_scope
prompt_password_storage
prompt_sync_on_start
setup_ok "Scope: $ICLOUD_SETUP_SCOPE"

export ICLOUD_SETUP_APPLE_ID ICLOUD_SETUP_APP_PASSWORD ICLOUD_SETUP_SCOPE ICLOUD_SETUP_SYNC_ON_START
export ICLOUD_SETUP_PERSIST_APP_PASSWORD

setup_step "Installing Codex MCP config"
"$ROOT/scripts/setup-codex-mcp.sh"

setup_step "Installing Claude Code MCP config"
"$ROOT/scripts/setup-claude-code-mcp.sh"

setup_step "Installing Hermes Agent MCP config"
"$ROOT/scripts/setup-hermes-agent-mcp.sh"

setup_ok "All requested MCP client configs updated"
printf '\n%sNext steps%s\n' "$COLOR_BOLD" "$COLOR_RESET"
printf '  1. Restart each MCP client, or reload MCP where supported.\n'
printf '  2. Ask each client: "Which MCP tools are available?"\n'
printf '  3. Use the sync tool if you chose not to sync on start.\n'
