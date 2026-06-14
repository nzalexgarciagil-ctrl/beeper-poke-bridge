#!/usr/bin/env bash
# Run the Beeper -> Poke bridge on macOS/Linux.
# Prefers uv if available; otherwise falls back to a local .venv.
set -euo pipefail
cd "$(dirname "$0")"

if command -v uv >/dev/null 2>&1; then
  exec uv run --with-requirements requirements.txt python bridge.py "$@"
fi

if [ ! -d .venv ]; then
  python3 -m venv .venv
  ./.venv/bin/pip install -q -r requirements.txt
fi
exec ./.venv/bin/python bridge.py "$@"
