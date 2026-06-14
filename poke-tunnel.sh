#!/usr/bin/env bash
# Exposes Beeper Desktop's local MCP to Poke so Poke can read your chats and
# draft replies. Requires the Poke CLI (npm i -g poke, then `poke login`).
# Override the endpoint with POKE_MCP_URL if your Beeper version differs.
set -euo pipefail
cd "$(dirname "$0")"
: "${POKE_MCP_URL:=http://localhost:23375/v0/mcp}"
exec poke tunnel "$POKE_MCP_URL" -n "Beeper Desktop"
