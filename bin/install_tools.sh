#!/usr/bin/env bash
set -euo pipefail
BIN_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ARCHIVE="${BIN_DIR}/ffmpeg-download.tar.xz"
trap 'rm -f "$ARCHIVE"' EXIT
curl -fL --retry 5 --retry-all-errors -o "$ARCHIVE" https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz
printf '%s  %s\n' abda8d77ce8309141f83ab8edf0596834087c52467f6badf376a6a2a4c87cf67 "$ARCHIVE" | sha256sum -c -
tar -xJf "$ARCHIVE" -C "$BIN_DIR" --strip-components=1 ffmpeg-7.0.2-amd64-static/ffmpeg ffmpeg-7.0.2-amd64-static/ffprobe ffmpeg-7.0.2-amd64-static/GPLv3.txt
