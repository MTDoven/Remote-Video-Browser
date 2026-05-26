from __future__ import annotations

import argparse
import hashlib
import io
import json
from datetime import datetime
import mimetypes
import os
import random
import re
import shlex
import shutil
import subprocess
import threading
import tempfile
from pathlib import Path
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any

from flask import Flask, Response, abort, render_template, request, send_file, url_for


VIDEO_EXTENSIONS = {
    ".mp4",
    ".m4v",
    ".webm",
    ".ogg",
    ".ogv",
    ".mkv",
    ".mov",
    ".avi",
    ".wmv",
    ".ts",
    ".m2ts",
}
DIRECT_PLAY_EXTENSIONS = {".mp4", ".m4v", ".webm", ".ogg", ".ogv"}
REMUX_TO_MP4_EXTENSIONS = {".mkv", ".mov", ".avi", ".wmv", ".ts", ".m2ts"}
MOBILE_USER_AGENT_PATTERN = re.compile(r"android|iphone|ipod|ipad|mobile|windows phone|blackberry", re.IGNORECASE)
CURRENT_VIDEO_COOKIE = "current_video"
THUMBNAIL_CACHE_DIR_NAME = "videos-thumbnail-cache"
THUMBNAIL_WORKERS = max(1, int(os.environ.get("VIDEO_THUMBNAIL_WORKERS", "2")))
THUMBNAIL_PREFETCH_LIMIT = 6
THUMBNAIL_CANDIDATE_COUNT = 15
THUMBNAIL_MIN_LUMA = 20
THUMBNAIL_MAX_LUMA = 235
THUMBNAIL_MIN_CONTRAST = 14
THUMBNAIL_CELL_WIDTH = 320
THUMBNAIL_CELL_HEIGHT = 180
THUMBNAIL_GRID_GAP = 10

_thumbnail_executor = ThreadPoolExecutor(max_workers=THUMBNAIL_WORKERS, thread_name_prefix="video-thumb")
_thumbnail_futures: dict[str, Future[dict[str, Any]]] = {}
_thumbnail_futures_lock = threading.Lock()
_ffmpeg_lookup_cache: tuple[str | None, str | None] | None = None


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

        cleanup_stale_video_cache(root, request.cookies.get(CURRENT_VIDEO_COOKIE), selected_relative)
        listing = list_directory(root, directory)
        prefetch_thumbnail_generation(root, listing["videos"], selected_video)
        template_name = "mobile.html" if is_mobile_request(request) else "index.html"
        response = render_template(
            template_name,
            root=root,
            listing=listing,
            selected_video=selected_video,
            selected_video_url=selected_url,
        )
        response = app.make_response(response)
        response.set_cookie(CURRENT_VIDEO_COOKIE, selected_relative or "", max_age=60 * 60 * 24 * 7, samesite="Lax")
        return response

    @app.route("/media/<path:relative_path>")
    def media_file(relative_path: str) -> Response:
        file_path = safe_resolve(app.config["VIDEO_ROOT"], relative_path)
        if not file_path.is_file() or file_path.suffix.lower() not in VIDEO_EXTENSIONS:
            abort(404)

        prepared_path, content_type = prepare_media_file(file_path)
        return ranged_file_response(prepared_path, content_type)

    @app.route("/thumb/<path:relative_path>")
    def thumbnail_file(relative_path: str) -> Response:
        file_path = safe_resolve(app.config["VIDEO_ROOT"], relative_path)
        if not file_path.is_file() or file_path.suffix.lower() not in VIDEO_EXTENSIONS:
            abort(404)

        artifact = ensure_thumbnail_artifacts(file_path)
        response = send_file(
            artifact["thumbnail_path"],
            mimetype="image/jpeg",
            conditional=False,
            max_age=60 * 60 * 24 * 30,
        )
        response.headers["Cache-Control"] = "public, max-age=2592000, immutable"
        return response

    @app.route("/preview/<path:relative_path>")
    def preview_sheet(relative_path: str) -> Response:
        file_path = safe_resolve(app.config["VIDEO_ROOT"], relative_path)
        if not file_path.is_file() or file_path.suffix.lower() not in VIDEO_EXTENSIONS:
            abort(404)

        seed = request.args.get("seed", "")
        artifact = ensure_thumbnail_artifacts(file_path)
        sheet = render_preview_contact_sheet(artifact, seed)
        response = send_file(sheet, mimetype="image/jpeg", conditional=False, max_age=0)
        response.headers["Cache-Control"] = "no-store, max-age=0"
        return response

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
    cache_token = f"{stat.st_size}-{stat.st_mtime_ns}"
    thumbnail_url = url_for("thumbnail_file", relative_path=relative_path) + f"?v={cache_token}"
    return {
        "type": "video",
        "name": file_path.name,
        "relative_path": relative_path,
        "page_url": url_for("index", dir=folder if folder != "." else "", v=relative_path),
        "media_url": url_for("media_file", relative_path=relative_path),
        "thumbnail_url": thumbnail_url,
        "preview_url": url_for("preview_sheet", relative_path=relative_path) + f"?v={cache_token}",
        "folder": folder,
        "size": stat.st_size,
        "size_label": format_filesize(stat.st_size),
        "modified": int(stat.st_mtime),
        "modified_label": modified.strftime("%Y-%m-%d %H:%M"),
    }


def prefetch_thumbnail_generation(root: Path, videos: list[dict[str, object]], selected_video: dict[str, object] | None) -> None:
    candidate_paths: list[Path] = []
    seen: set[str] = set()

    if selected_video:
        selected_relative = str(selected_video["relative_path"])
        if selected_relative not in seen:
            candidate_paths.append(safe_resolve(root, selected_relative))
            seen.add(selected_relative)

    for video in videos[:THUMBNAIL_PREFETCH_LIMIT]:
        relative_path = str(video["relative_path"])
        if relative_path in seen:
            continue
        candidate_paths.append(safe_resolve(root, relative_path))
        seen.add(relative_path)

    for file_path in candidate_paths:
        schedule_thumbnail_generation(file_path)


def schedule_thumbnail_generation(file_path: Path) -> None:
    cache_key = thumbnail_cache_key(file_path)
    with _thumbnail_futures_lock:
        future = _thumbnail_futures.get(cache_key)
        if future is not None and not future.done():
            return
        if future is not None and future.done():
            _thumbnail_futures.pop(cache_key, None)
        _thumbnail_futures[cache_key] = _thumbnail_executor.submit(generate_thumbnail_artifacts, file_path)


def ensure_thumbnail_artifacts(file_path: Path) -> dict[str, Any]:
    cache_key = thumbnail_cache_key(file_path)
    cached = load_thumbnail_artifacts(file_path)
    if cached is not None:
        return cached

    with _thumbnail_futures_lock:
        future = _thumbnail_futures.get(cache_key)
        if future is not None and future.done():
            _thumbnail_futures.pop(cache_key, None)
            future = None
        if future is None:
            future = _thumbnail_executor.submit(generate_thumbnail_artifacts, file_path)
            _thumbnail_futures[cache_key] = future

    try:
        return future.result()
    finally:
        if future.done():
            with _thumbnail_futures_lock:
                _thumbnail_futures.pop(cache_key, None)


def load_thumbnail_artifacts(file_path: Path) -> dict[str, Any] | None:
    cache_dir = thumbnail_cache_dir(file_path)
    manifest_path = cache_dir / "manifest.json"
    thumbnail_path = cache_dir / "thumbnail.jpg"
    if not manifest_path.is_file() or not thumbnail_path.is_file():
        return None

    try:
        manifest = json.loads(manifest_path.read_text())
    except (OSError, json.JSONDecodeError):
        return None

    candidates: list[dict[str, Any]] = []
    for entry in manifest.get("candidates", []):
        candidate_name = entry.get("image")
        if not candidate_name:
            return None
        candidate_path = cache_dir / candidate_name
        if not candidate_path.is_file():
            return None
        candidates.append(
            {
                "timestamp": float(entry.get("timestamp", 0.0)),
                "score": float(entry.get("score", 0.0)),
                "luminance": float(entry.get("luminance", 0.0)),
                "contrast": float(entry.get("contrast", 0.0)),
                "image_path": candidate_path,
            }
        )

    if not candidates:
        return None

    return {
        "cache_dir": cache_dir,
        "manifest_path": manifest_path,
        "thumbnail_path": thumbnail_path,
        "candidates": candidates,
    }


def generate_thumbnail_artifacts(file_path: Path) -> dict[str, Any]:
    cache_dir = thumbnail_cache_dir(file_path)
    cache_dir.mkdir(parents=True, exist_ok=True)

    candidate_frames = extract_candidate_frames(file_path, cache_dir)
    if not candidate_frames:
        abort(503, description="Unable to extract thumbnail frames from this video")

    best_candidate = max(candidate_frames, key=lambda item: (item["score"], -item["timestamp"]))
    thumbnail_path = cache_dir / "thumbnail.jpg"
    shutil.copyfile(best_candidate["image_path"], thumbnail_path)

    manifest = {
        "thumbnail": thumbnail_path.name,
        "candidates": [
            {
                "image": candidate["image_path"].name,
                "timestamp": candidate["timestamp"],
                "score": candidate["score"],
                "luminance": candidate["luminance"],
                "contrast": candidate["contrast"],
            }
            for candidate in sorted(candidate_frames, key=lambda item: (item["score"], item["timestamp"]), reverse=True)
        ],
    }
    (cache_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2))

    return {
        "cache_dir": cache_dir,
        "manifest_path": cache_dir / "manifest.json",
        "thumbnail_path": thumbnail_path,
        "candidates": sorted(candidate_frames, key=lambda item: (item["score"], item["timestamp"]), reverse=True),
    }


def extract_candidate_frames(file_path: Path, cache_dir: Path) -> list[dict[str, Any]]:
    ffmpeg_path = find_ffmpeg()
    if not ffmpeg_path:
        abort(503, description="ffmpeg is required to build thumbnail previews")

    duration = probe_video_duration(ffmpeg_path, file_path)
    timestamps = select_candidate_timestamps(duration)
    candidate_frames: list[dict[str, Any]] = []

    for index, timestamp in enumerate(timestamps):
        image_path = cache_dir / f"frame_{index:02d}.jpg"
        if not image_path.is_file():
            metrics = extract_frame_image(ffmpeg_path, file_path, timestamp, image_path)
            if metrics is None:
                continue

        else:
            metrics = score_thumbnail_image(image_path)
        if metrics is None:
            continue

        candidate_frames.append(
            {
                "timestamp": timestamp,
                "score": metrics["score"],
                "luminance": metrics["luminance"],
                "contrast": metrics["contrast"],
                "image_path": image_path,
            }
        )

    if len(candidate_frames) < 9:
        fallback_timestamps = [0.5, 1.0, 2.0, 3.0, 5.0, 8.0]
        for timestamp in fallback_timestamps:
            image_path = cache_dir / f"fallback_{int(timestamp * 10):02d}.jpg"
            if not image_path.is_file():
                metrics = extract_frame_image(ffmpeg_path, file_path, timestamp, image_path)
                if metrics is None:
                    continue
            else:
                metrics = score_thumbnail_image(image_path, relaxed=True)
            if metrics is None:
                continue

            candidate_frames.append(
                {
                    "timestamp": timestamp,
                    "score": metrics["score"],
                    "luminance": metrics["luminance"],
                    "contrast": metrics["contrast"],
                    "image_path": image_path,
                }
            )

    unique_frames: list[dict[str, Any]] = []
    seen_paths: set[Path] = set()
    for candidate in sorted(candidate_frames, key=lambda item: (item["score"], item["timestamp"]), reverse=True):
        if candidate["image_path"] in seen_paths:
            continue
        unique_frames.append(candidate)
        seen_paths.add(candidate["image_path"])

    return unique_frames


def probe_video_duration(ffmpeg_path: str, file_path: Path) -> float | None:
    command = [ffmpeg_path, "-hide_banner", "-i", str(file_path)]
    try:
        result = subprocess.run(command, check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except OSError:
        return None

    stderr = (result.stderr or b"").decode("utf-8", errors="replace")
    match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", stderr)
    if not match:
        return None

    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def select_candidate_timestamps(duration: float | None) -> list[float]:
    if duration is None or duration <= 0:
        return [0.5, 1.0, 2.0, 3.0, 5.0, 8.0, 12.0, 18.0, 24.0, 32.0, 45.0, 60.0, 90.0, 120.0, 180.0]

    if duration < 6:
        return sorted({max(min(duration * ratio, max(duration - 0.1, 0.1)), 0.1) for ratio in (0.18, 0.36, 0.54, 0.72, 0.88)})

    start = max(duration * 0.08, 0.5)
    stop = max(duration * 0.92, start + 0.5)
    step = (stop - start) / max(THUMBNAIL_CANDIDATE_COUNT - 1, 1)
    timestamps = [round(start + step * index, 3) for index in range(THUMBNAIL_CANDIDATE_COUNT)]
    return timestamps


def extract_frame_image(ffmpeg_path: str, file_path: Path, timestamp: float, image_path: Path) -> dict[str, float] | None:
    command = [
        ffmpeg_path,
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        f"{max(timestamp, 0.1):.3f}",
        "-i",
        str(file_path),
        "-frames:v",
        "1",
        "-vf",
        (
            f"scale={THUMBNAIL_CELL_WIDTH}:-2:force_original_aspect_ratio=decrease,"
            "signalstats,metadata=print:file=-"
        ),
        "-q:v",
        "4",
        "-y",
        str(image_path),
    ]
    try:
        result = subprocess.run(command, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except (subprocess.CalledProcessError, OSError):
        image_path.unlink(missing_ok=True)
        return None

    if not image_path.is_file():
        return None

    metadata = parse_signalstats((result.stdout or b"") + (result.stderr or b""))
    if metadata:
        metrics = metrics_from_signalstats(metadata)
        if metrics is not None:
            return metrics

    return score_thumbnail_image(image_path)


def score_thumbnail_image(image_path: Path, *, relaxed: bool = False) -> dict[str, float] | None:
    ffmpeg_path = find_ffmpeg()
    if not ffmpeg_path:
        return None

    command = [
        ffmpeg_path,
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(image_path),
        "-frames:v",
        "1",
        "-vf",
        "signalstats,metadata=print:file=-",
        "-f",
        "null",
        "-",
    ]
    try:
        result = subprocess.run(command, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except (subprocess.CalledProcessError, OSError):
        return None

    metadata = parse_signalstats((result.stdout or b"") + (result.stderr or b""))
    return metrics_from_signalstats(metadata, relaxed=relaxed)


def metrics_from_signalstats(metadata: dict[str, float], *, relaxed: bool = False) -> dict[str, float] | None:
    if not metadata:
        return None

    luminance = metadata.get("YAVG")
    if luminance is None:
        return None

    y_min = metadata.get("YMIN", luminance)
    y_max = metadata.get("YMAX", luminance)
    saturation = metadata.get("SATAVG", 0.0)
    contrast = max(y_max - y_min, 0.0)

    if not relaxed and (luminance < THUMBNAIL_MIN_LUMA or luminance > THUMBNAIL_MAX_LUMA or contrast < THUMBNAIL_MIN_CONTRAST):
        return None

    score = contrast * 1.8 + saturation * 35.0 + min(luminance, 255.0 - luminance) * 0.18
    if relaxed:
        score *= 0.75

    return {
        "luminance": float(luminance),
        "contrast": float(contrast),
        "score": float(score),
    }


def render_preview_contact_sheet(artifact: dict[str, Any], seed: str) -> io.BytesIO:
    candidates = artifact["candidates"]
    if not candidates:
        abort(404)

    rng = random.Random(seed or None)
    selected = candidates if len(candidates) <= 9 else rng.sample(candidates, 9)
    selected = list(selected)
    while len(selected) < 9:
        selected.append(rng.choice(candidates))

    ffmpeg_path = find_ffmpeg()
    if not ffmpeg_path:
        abort(503, description="ffmpeg is required to build thumbnail previews")

    with tempfile.TemporaryDirectory(prefix="videos-preview-") as temp_dir_name:
        temp_dir = Path(temp_dir_name)
        list_path = temp_dir / "frames.txt"
        list_path.write_text(
            "\n".join(f"file {shlex.quote(str(candidate['image_path']))}" for candidate in selected[:9]),
            encoding="utf-8",
        )

        command = [
            ffmpeg_path,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(list_path),
            "-vf",
            (
                f"scale={THUMBNAIL_CELL_WIDTH}:{THUMBNAIL_CELL_HEIGHT}:"
                "force_original_aspect_ratio=decrease,"
                f"pad={THUMBNAIL_CELL_WIDTH}:{THUMBNAIL_CELL_HEIGHT}:(ow-iw)/2:(oh-ih)/2:color=black,"
                f"tile=3x3:margin={THUMBNAIL_GRID_GAP}:padding={THUMBNAIL_GRID_GAP}"
            ),
            "-frames:v",
            "1",
            "-f",
            "image2pipe",
            "-vcodec",
            "mjpeg",
            "pipe:1",
        ]
        try:
            result = subprocess.run(command, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except (subprocess.CalledProcessError, OSError):
            abort(503, description="Unable to build preview contact sheet")

    output = io.BytesIO(result.stdout or b"")
    output.seek(0)
    return output


def parse_signalstats(raw_output: bytes) -> dict[str, float]:
    text = raw_output.decode("utf-8", errors="replace")
    matches = re.findall(r"lavfi\.signalstats\.([A-Z]+)=([0-9]+(?:\.[0-9]+)?)", text)
    metadata: dict[str, float] = {}
    for key, value in matches:
        try:
            metadata[key] = float(value)
        except ValueError:
            continue
    return metadata


def thumbnail_cache_key(file_path: Path) -> str:
    stat = file_path.stat()
    fingerprint = "\n".join(
        [
            file_path.as_posix(),
            str(stat.st_size),
            str(int(stat.st_mtime_ns)),
            file_path.suffix.lower(),
        ]
    )
    return hashlib.sha1(fingerprint.encode("utf-8")).hexdigest()


def thumbnail_cache_dir(file_path: Path) -> Path:
    return Path(tempfile.gettempdir()) / THUMBNAIL_CACHE_DIR_NAME / thumbnail_cache_key(file_path)


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


def prepare_media_file(file_path: Path) -> tuple[Path, str]:
    if file_path.suffix.lower() in DIRECT_PLAY_EXTENSIONS:
        return file_path, content_type_for(file_path)
    if file_path.suffix.lower() in REMUX_TO_MP4_EXTENSIONS:
        remuxed_path = remux_video_to_mp4(file_path)
        return remuxed_path, "video/mp4"
    abort(404)


def find_ffmpeg() -> str | None:
    global _ffmpeg_lookup_cache

    configured_ffmpeg = os.environ.get("FFMPEG_BIN")
    if _ffmpeg_lookup_cache and _ffmpeg_lookup_cache[0] == configured_ffmpeg:
        return _ffmpeg_lookup_cache[1]

    if configured_ffmpeg and Path(configured_ffmpeg).is_file():
        _ffmpeg_lookup_cache = (configured_ffmpeg, configured_ffmpeg)
        return configured_ffmpeg

    system_ffmpeg = shutil.which("ffmpeg")
    if system_ffmpeg:
        _ffmpeg_lookup_cache = (configured_ffmpeg, system_ffmpeg)
        return system_ffmpeg

    try:
        import imageio_ffmpeg
    except ImportError:
        _ffmpeg_lookup_cache = (configured_ffmpeg, None)
        return None

    resolved = imageio_ffmpeg.get_ffmpeg_exe()
    _ffmpeg_lookup_cache = (configured_ffmpeg, resolved)
    return resolved


def remux_video_to_mp4(file_path: Path) -> Path:
    ffmpeg_path = find_ffmpeg()
    if not ffmpeg_path:
        abort(503, description="ffmpeg is required to remux video files")

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


def cleanup_stale_video_cache(root: Path, previous_relative: str | None, current_relative: str) -> None:
    if not previous_relative or previous_relative == current_relative:
        return

    try:
        previous_path = safe_resolve(root, previous_relative)
    except Exception:
        return

    if previous_path.suffix.lower() not in REMUX_TO_MP4_EXTENSIONS:
        return

    cache_path = remux_cache_path(Path(tempfile.gettempdir()) / "videos-mkv-remux", previous_path)
    cache_path.unlink(missing_ok=True)


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
