#!/usr/bin/env bash
set -euo pipefail
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VIDEO_DIR="${VIDEO_DIR:-/data/file/AAASSSSS/}"
PORT="${PORT:-18483}"
PYTHON_BIN="${APP_DIR}/.venv/bin/python"
unset PYTHONHOME PYTHONPATH PYTHONUSERBASE
export PYTHONNOUSERSITE=1
if [ ! -x "$PYTHON_BIN" ]; then
  echo "Project Python is missing: ${PYTHON_BIN}" >&2
  exit 1
fi
if ! "$PYTHON_BIN" -c 'import flask, waitress'; then
  printf 'Install project dependencies: "%s" -m pip install -r "%s/requirements.txt"\n' "$PYTHON_BIN" "$APP_DIR" >&2
  exit 1
fi
cd "$APP_DIR"
exec "$PYTHON_BIN" app.py --port "$PORT" "$VIDEO_DIR"
