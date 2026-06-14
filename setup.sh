#!/usr/bin/env bash
# Interactive setup: collects your credentials and writes .env.
set -euo pipefail
cd "$(dirname "$0")"

if command -v uv >/dev/null 2>&1; then
  exec uv run --with-requirements requirements.txt python configure.py
fi

if [ ! -d .venv ]; then
  python3 -m venv .venv
  ./.venv/bin/pip install -q -r requirements.txt
fi
exec ./.venv/bin/python configure.py
