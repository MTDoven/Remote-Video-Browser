from __future__ import annotations

import argparse
import hashlib
from datetime import datetime
import mimetypes
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from functools import lru_cache

from flask import Flask, Response, abort, render_template, request, url_for


VIDEO_EXTENSIONS = {".mp4", ".m4v", ".webm", ".ogg", ".ogv", ".mkv"}
DIRECT_PLAY_EXTENSIONS = {".mp4", ".m4v", ".webm", ".ogg", ".ogv"}
MOBILE_USER_AGENT_PATTERN = re.compile(r"android|iphone|ipod|ipad|mobile|windows phone|blackberry", re.IGNORECASE)


def create_app(video_root: Path) -> Flask:
    app = Flask(__name__)
    app.config["VIDEO_ROOT"] = video_root.resolve()

    @app.route("/")
    def index() -> str:
        root = app.config["VIDEO_ROOT"]
        directory = safe_resolve(root, request.args.get("dir", ""))
        if not directory.is_dir():
            abort(404)

        selected_video = None
        selected_url = None
        selected_relative = request.args.get("v", "")
        if selected_relative:
            selected_path = safe_resolve(root, selected_relative)
            if not selected_path.is_file() or selected_path.suffix.lower() not in VIDEO_EXTENSIONS:
                abort(404)
            directory = selected_path.parent
            selected_video = video_entry(root, selected_path)
            selected_url = url_for("media_file", relative_path=selected_relative)

        listing = list_directory(root, directory)
        template_name = "mobile.html" if is_mobile_request(request) else "index.html"
        return render_template(
            template_name,
            root=root,
            listing=listing,
            selected_video=selected_video,
            selected_video_url=selected_url,
        )

    @app.route("/media/<path:relative_path>")
    def media_file(relative_path: str) -> Response:
        file_path = safe_resolve(app.config["VIDEO_ROOT"], relative_path)
        if not file_path.is_file() or file_path.suffix.lower() not in VIDEO_EXTENSIONS:
            abort(404)

        prepared_path, content_type = prepare_media_file(app, file_path)
        return ranged_file_response(prepared_path, content_type)

    return app


def list_directory(root: Path, directory: Path) -> dict[str, object]:
    relative_directory = directory.relative_to(root).as_posix()
    if relative_directory == ".":
        relative_directory = ""

    folders = []
    videos = []
    for child in sorted(directory.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower())):
        if child.is_dir():
            folders.append(folder_entry(root, child))
        elif child.is_file() and child.suffix.lower() in VIDEO_EXTENSIONS:
            videos.append(video_entry(root, child))

    return {
        "path": relative_directory,
        "parent": parent_path(relative_directory),
        "breadcrumbs": breadcrumbs(relative_directory),
        "folders": folders,
        "videos": videos,
        "total_size": sum(int(video["size"]) for video in videos),
    }


def folder_entry(root: Path, folder_path: Path) -> dict[str, object]:
    stat = folder_path.stat()
    modified = datetime.fromtimestamp(stat.st_mtime)
    relative_path = folder_path.relative_to(root).as_posix()
    return {
        "type": "folder",
        "name": folder_path.name,
        "relative_path": relative_path,
        "page_url": url_for("index", dir=relative_path),
        "modified": int(stat.st_mtime),
        "modified_label": modified.strftime("%Y-%m-%d %H:%M"),
    }


def video_entry(root: Path, file_path: Path) -> dict[str, object]:
    stat = file_path.stat()
    modified = datetime.fromtimestamp(stat.st_mtime)
    relative_path = file_path.relative_to(root).as_posix()
    folder = file_path.parent.relative_to(root).as_posix() if file_path.parent != root else "."
    return {
        "type": "video",
        "name": file_path.name,
        "relative_path": relative_path,
        "page_url": url_for("index", dir=folder if folder != "." else "", v=relative_path),
        "media_url": url_for("media_file", relative_path=relative_path),
        "folder": folder,
        "size": stat.st_size,
        "size_label": format_filesize(stat.st_size),
        "modified": int(stat.st_mtime),
        "modified_label": modified.strftime("%Y-%m-%d %H:%M"),
    }


def parent_path(relative_path: str) -> str | None:
    if not relative_path:
        return None
    parent = Path(relative_path).parent.as_posix()
    return "" if parent == "." else parent


def breadcrumbs(relative_path: str) -> list[dict[str, str]]:
    items = [{"name": "Root", "path": ""}]
    current = Path()
    for part in Path(relative_path).parts if relative_path else []:
        current /= part
        items.append({"name": part, "path": current.as_posix()})
    return items


def format_filesize(value: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(value)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} {unit}"
        size /= 1024
    return f"{value} B"


def safe_resolve(root: Path, relative_path: str) -> Path:
    candidate = (root / relative_path).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        abort(403)
    return candidate


def content_type_for(file_path: Path) -> str:
    content_type, _ = mimetypes.guess_type(file_path.name)
    return content_type or "application/octet-stream"


def is_mobile_request(req) -> bool:
    user_agent = req.headers.get("User-Agent", "")
    return bool(MOBILE_USER_AGENT_PATTERN.search(user_agent))


def prepare_media_file(app: Flask, file_path: Path) -> tuple[Path, str]:
    if file_path.suffix.lower() in DIRECT_PLAY_EXTENSIONS:
        return file_path, content_type_for(file_path)
    if file_path.suffix.lower() == ".mkv":
        remuxed_path = remux_mkv_to_mp4(app, file_path)
        return remuxed_path, "video/mp4"
    abort(404)


@lru_cache(maxsize=128)
def find_ffmpeg() -> str | None:
    configured_ffmpeg = os.environ.get("FFMPEG_BIN")
    if configured_ffmpeg and Path(configured_ffmpeg).is_file():
        return configured_ffmpeg

    system_ffmpeg = shutil.which("ffmpeg")
    if system_ffmpeg:
        return system_ffmpeg

    try:
        import imageio_ffmpeg
    except ImportError:
        return None

    return imageio_ffmpeg.get_ffmpeg_exe()


def remux_mkv_to_mp4(app: Flask, file_path: Path) -> Path:
    ffmpeg_path = find_ffmpeg()
    if not ffmpeg_path:
        abort(503, description="ffmpeg is required to remux MKV files")

    cache_dir = Path(tempfile.gettempdir()) / "videos-mkv-remux"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = remux_cache_path(cache_dir, file_path)
    if cache_path.is_file():
        return cache_path

    temp_path = cache_path.with_suffix(".tmp.mp4")
    command = [
        ffmpeg_path,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(file_path),
        "-map",
        "0:v:0",
        "-map",
        "0:a:0?",
        "-dn",
        "-sn",
        "-c:v",
        "copy",
        "-c:a",
        "copy",
        "-movflags",
        "+faststart",
        "-f",
        "mp4",
        str(temp_path),
    ]
    try:
        subprocess.run(command, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except subprocess.CalledProcessError as error:
        temp_path.unlink(missing_ok=True)
        message = (error.stderr or b"remux failed").decode("utf-8", errors="replace").strip()
        abort(500, description=message or "remux failed")

    temp_path.replace(cache_path)
    return cache_path


def remux_cache_path(cache_dir: Path, file_path: Path) -> Path:
    stat = file_path.stat()
    fingerprint = "\n".join(
        [
            file_path.as_posix(),
            str(stat.st_size),
            str(int(stat.st_mtime)),
            file_path.suffix.lower(),
        ]
    )
    digest = hashlib.sha1(fingerprint.encode("utf-8")).hexdigest()
    return cache_dir / f"{digest}.mp4"


def parse_range(range_header: str, file_size: int) -> tuple[int, int]:
    unit, _, range_value = range_header.partition("=")
    if unit.strip().lower() != "bytes" or "-" not in range_value:
        abort(416)

    start_value, _, end_value = range_value.partition("-")
    try:
        if start_value == "":
            suffix_length = int(end_value)
            if suffix_length <= 0:
                abort(416)
            start = max(file_size - suffix_length, 0)
            end = file_size - 1
        else:
            start = int(start_value)
            end = int(end_value) if end_value else file_size - 1
    except ValueError:
        abort(416)

    if start < 0 or end >= file_size or start > end:
        abort(416)
    return start, end


def stream_file(file_path: Path, start: int, end: int, chunk_size: int = 1024 * 1024):
    with file_path.open("rb") as file:
        file.seek(start)
        remaining = end - start + 1
        while remaining > 0:
            data = file.read(min(chunk_size, remaining))
            if not data:
                break
            remaining -= len(data)
            yield data


def ranged_file_response(file_path: Path, content_type: str) -> Response:
    file_size = file_path.stat().st_size
    range_header = request.headers.get("Range")

    if not range_header:
        return Response(
            stream_file(file_path, 0, file_size - 1),
            mimetype=content_type,
            headers={"Content-Length": str(file_size), "Accept-Ranges": "bytes"},
            direct_passthrough=True,
        )

    start, end = parse_range(range_header, file_size)
    length = end - start + 1
    return Response(
        stream_file(file_path, start, end),
        status=206,
        mimetype=content_type,
        headers={
            "Content-Range": f"bytes {start}-{end}/{file_size}",
            "Accept-Ranges": "bytes",
            "Content-Length": str(length),
        },
        direct_passthrough=True,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Browse and play videos from a directory.")
    parser.add_argument("directory", type=Path, help="Video root directory to scan.")
    parser.add_argument("--host", default="0.0.0.0", help="Host to bind. Default: 0.0.0.0")
    parser.add_argument("--port", type=int, default=8000, help="Port to bind. Default: 8000")
    parser.add_argument("--debug", action="store_true", help="Enable Flask debug mode.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    video_root = args.directory.expanduser().resolve()
    if not video_root.is_dir():
        raise SystemExit(f"Directory does not exist: {video_root}")

    app = create_app(video_root)
    app.run(host=args.host, port=args.port, debug=args.debug)


if __name__ == "__main__":
    main()
