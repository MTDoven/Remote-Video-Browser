#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VIDEO_DIR="/data/file/AAASSSSS/JapaneseVideos/"
PORT="${PORT:-18483}"
CONDA_ENV="${CONDA_ENV:-videos}"
LOCAL_FFMPEG="${APP_DIR}/bin/ffmpeg"

find_conda() {
  if command -v conda >/dev/null 2>&1; then
    command -v conda
    return
  fi

  for candidate in "$HOME/miniconda3/bin/conda" "$HOME/anaconda3/bin/conda" "/opt/conda/bin/conda"; do
    if [ -x "$candidate" ]; then
      printf '%s\n' "$candidate"
      return
    fi
  done

  return 1
}

CONDA_BIN="$(find_conda)" || {
  echo "Unable to find conda. Install conda or add it to PATH." >&2
  exit 1
}

kill_port() {
  local port="$1"
  local pids=""

  if command -v lsof >/dev/null 2>&1; then
    pids="$(lsof -ti "tcp:${port}" 2>/dev/null || true)"
  elif command -v fuser >/dev/null 2>&1; then
    pids="$(fuser "${port}/tcp" 2>/dev/null || true)"
  elif command -v python >/dev/null 2>&1; then
    pids="$(python - "$port" <<'PY'
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
        if len(fields) < 10 or fields[3] != "0A":
            continue
        port = int(fields[1].rsplit(":", 1)[1], 16)
        if port == target_port:
            target_inodes.add(fields[9])

pids = set()
for proc in Path("/proc").iterdir():
    if not proc.name.isdigit():
        continue
    fd_dir = proc / "fd"
    if not fd_dir.exists():
        continue
    for fd in fd_dir.iterdir():
        try:
            link = os.readlink(fd)
        except OSError:
            continue
        if link.startswith("socket:[") and link[8:-1] in target_inodes:
            pids.add(proc.name)
            break

print(" ".join(sorted(pids, key=int)))
PY
)"
  fi

  if [ -z "$pids" ]; then
    return
  fi

  echo "Stopping process on port ${port}: ${pids}"
  kill $pids 2>/dev/null || true
  sleep 1

  if command -v lsof >/dev/null 2>&1; then
    pids="$(lsof -ti "tcp:${port}" 2>/dev/null || true)"
  elif command -v fuser >/dev/null 2>&1; then
    pids="$(fuser "${port}/tcp" 2>/dev/null || true)"
  elif command -v python >/dev/null 2>&1; then
    pids="$(python - "$port" <<'PY'
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
        if len(fields) < 10 or fields[3] != "0A":
            continue
        port = int(fields[1].rsplit(":", 1)[1], 16)
        if port == target_port:
            target_inodes.add(fields[9])

pids = set()
for proc in Path("/proc").iterdir():
    if not proc.name.isdigit():
        continue
    fd_dir = proc / "fd"
    if not fd_dir.exists():
        continue
    for fd in fd_dir.iterdir():
        try:
            link = os.readlink(fd)
        except OSError:
            continue
        if link.startswith("socket:[") and link[8:-1] in target_inodes:
            pids.add(proc.name)
            break

print(" ".join(sorted(pids, key=int)))
PY
)"
  else
    pids=""
  fi

  if [ -n "$pids" ]; then
    echo "Force stopping process on port ${port}: ${pids}"
    kill -9 $pids 2>/dev/null || true
  fi
}

eval "$("$CONDA_BIN" shell.bash hook)"
conda activate "$CONDA_ENV"
kill_port "$PORT"

cd "$APP_DIR"
if [ -z "${FFMPEG_BIN:-}" ] && [ -x "$LOCAL_FFMPEG" ]; then
  export FFMPEG_BIN="$LOCAL_FFMPEG"
fi

exec python app.py --port "$PORT" "$VIDEO_DIR"
