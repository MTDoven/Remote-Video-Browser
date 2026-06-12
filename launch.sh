#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VIDEO_DIR="/data/temp/datasets/UltraVideo/"
PORT="${PORT:-18483}"
VENV_DIR="${VENV_DIR:-${APP_DIR}/.venv}"
PYTHON_BIN="${VENV_DIR}/bin/python"
LOCAL_FFMPEG="${APP_DIR}/bin/ffmpeg"
export AV1_TRANSCODE_PRESET=3

port_pids() {
  local port="$1"

  command -v lsof >/dev/null 2>&1 && { lsof -ti "tcp:${port}" 2>/dev/null || true; return; }
  command -v fuser >/dev/null 2>&1 && { fuser "${port}/tcp" 2>/dev/null || true; return; }
  [ -x "$PYTHON_BIN" ] || return

  "$PYTHON_BIN" - "$port" <<'PY'
import os
import sys
from pathlib import Path

target_port = int(sys.argv[1])
target_inodes = set()

for table in (Path("/proc/net/tcp"), Path("/proc/net/tcp6")):
    if not table.exists():
        continue
    for line in table.read_text().splitlines()[1:]:
        fields = line.split()
        if len(fields) >= 10 and fields[3] == "0A" and int(fields[1].rsplit(":", 1)[1], 16) == target_port:
            target_inodes.add(fields[9])

pids = set()
for proc in Path("/proc").iterdir():
    if not proc.name.isdigit():
        continue
    fd_dir = proc / "fd"
    for fd in fd_dir.iterdir() if fd_dir.exists() else ():
        try:
            link = os.readlink(fd)
        except OSError:
            continue
        if link.startswith("socket:[") and link[8:-1] in target_inodes:
            pids.add(proc.name)
            break

print(" ".join(sorted(pids, key=int)))
PY
}

kill_port() {
  local port="$1"
  local pids
  pids="$(port_pids "$port")"

  if [ -z "$pids" ]; then
    return
  fi

  echo "Stopping process on port ${port}: ${pids}"
  kill $pids 2>/dev/null || true
  sleep 1

  pids="$(port_pids "$port")"

  if [ -n "$pids" ]; then
    echo "Force stopping process on port ${port}: ${pids}"
    kill -9 $pids 2>/dev/null || true
  fi
}

if [ ! -x "$PYTHON_BIN" ]; then
  echo "Virtual environment not found at ${VENV_DIR}" >&2
  echo "Create it with: uv venv .venv && uv pip install -r requirements.txt" >&2
  exit 1
fi

kill_port "$PORT"

cd "$APP_DIR"
[ -n "${FFMPEG_BIN:-}" ] || [ ! -x "$LOCAL_FFMPEG" ] || export FFMPEG_BIN="$LOCAL_FFMPEG"

exec "$PYTHON_BIN" app.py --port "$PORT" "$VIDEO_DIR"
