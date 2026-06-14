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
import time
from pathlib import Path
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Callable

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
BROWSER_PLAYABLE_MP4_AUDIO_CODECS = {None, "aac", "mp3", "opus", "flac", "alac"}
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
FORCE_ENCODE_AV1 = os.environ.get("FORCE_ENCODE_AV1") == "1"
AV1_TRANSCODE_PRESET = int(os.environ.get("AV1_TRANSCODE_PRESET", "5"))
AV1_TRANSCODE_TARGET_RATIO = 0.75
AV1_PRELOAD_MIN_BYTES = int(os.environ.get("AV1_PRELOAD_MIN_BYTES", str(512 * 1024)))
AV1_PRELOAD_TIMEOUT_SECONDS = float(os.environ.get("AV1_PRELOAD_TIMEOUT_SECONDS", "2.0"))
AV1_SESSION_TTL_SECONDS = float(os.environ.get("AV1_SESSION_TTL_SECONDS", "300"))
VIDEO_STREAM_CHUNK_SIZE = int(os.environ.get("VIDEO_STREAM_CHUNK_SIZE", str(256 * 1024)))

_thumbnail_executor = ThreadPoolExecutor(max_workers=THUMBNAIL_WORKERS, thread_name_prefix="video-thumb")
_thumbnail_futures: dict[str, Future[dict[str, Any]]] = {}
_thumbnail_futures_lock = threading.Lock()
_ffmpeg_lookup_cache: tuple[str | None, str | None] | None = None
_ffprobe_lookup_cache: tuple[str | None, str | None] | None = None
_ffmpeg_encoder_cache: dict[tuple[str, str], bool] = {}
_video_duration_cache: dict[str, float | None] = {}
_video_duration_cache_lock = threading.Lock()
_av1_sessions: dict[str, "Av1TranscodeSession"] = {}
_av1_sessions_lock = threading.Lock()
_media_prepare_jobs: dict[str, "MediaPrepareJob"] = {}
_media_prepare_jobs_lock = threading.Lock()


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
            selected_video["average_bitrate_bps"] = estimate_average_bitrate_bps(selected_path)
            selected_video["av1_stream_url"] = url_for("av1_media_file", relative_path=selected_relative)

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
            force_encode_av1=FORCE_ENCODE_AV1,
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

    @app.route("/prepare/<path:relative_path>")
    def prepare_media(relative_path: str) -> Response:
        file_path = safe_resolve(app.config["VIDEO_ROOT"], relative_path)
        if not file_path.is_file() or file_path.suffix.lower() not in VIDEO_EXTENSIONS:
            abort(404)

        status = prepare_media_status(file_path)
        return Response(json.dumps(status), mimetype="application/json", headers={"Cache-Control": "no-store, max-age=0"})

    @app.route("/media-av1/<path:relative_path>")
    def av1_media_file(relative_path: str) -> Response:
        file_path = safe_resolve(app.config["VIDEO_ROOT"], relative_path)
        if not file_path.is_file() or file_path.suffix.lower() not in VIDEO_EXTENSIONS:
            abort(404)

        bandwidth_bps = parse_optional_int(request.args.get("bandwidth_bps"))
        start_seconds = parse_optional_float(request.args.get("start_seconds")) or 0.0
        if request.args.get("preload") == "1":
            session = get_or_create_av1_session(file_path, bandwidth_bps, start_seconds)
            ready = session.wait_for_preload(AV1_PRELOAD_MIN_BYTES, AV1_PRELOAD_TIMEOUT_SECONDS)
            payload = {
                "ready": ready,
                "bytes_written": session.bytes_written,
                "preload_threshold": AV1_PRELOAD_MIN_BYTES,
            }
            return Response(json.dumps(payload), mimetype="application/json", headers={"Cache-Control": "no-store, max-age=0"})
        return av1_transcode_response(file_path, bandwidth_bps)

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
    duration_seconds = estimate_video_duration_seconds(file_path)
    return {
        "type": "video",
        "name": file_path.name,
        "relative_path": relative_path,
        "page_url": url_for("index", dir=folder if folder != "." else "", v=relative_path),
        "media_url": url_for("media_file", relative_path=relative_path),
        "prepare_url": url_for("prepare_media", relative_path=relative_path),
        "thumbnail_url": thumbnail_url,
        "preview_url": url_for("preview_sheet", relative_path=relative_path) + f"?v={cache_token}",
        "folder": folder,
        "size": stat.st_size,
        "size_label": format_filesize(stat.st_size),
        "duration_seconds": duration_seconds,
        "duration_label": format_duration_label(duration_seconds),
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


def estimate_average_bitrate_bps(file_path: Path) -> int | None:
    ffprobe_path = find_ffprobe()
    if ffprobe_path:
        command = [
            ffprobe_path,
            "-v",
            "error",
            "-show_entries",
            "format=duration,bit_rate",
            "-of",
            "json",
            str(file_path),
        ]
        try:
            result = subprocess.run(command, check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            payload = json.loads((result.stdout or b"{}").decode("utf-8", errors="replace"))
            format_info = payload.get("format", {})
            bit_rate = format_info.get("bit_rate")
            if bit_rate not in (None, "", "N/A"):
                return int(float(bit_rate))

            duration_value = format_info.get("duration")
            if duration_value not in (None, "", "N/A"):
                duration = float(duration_value)
                if duration > 0:
                    return int(file_path.stat().st_size * 8 / duration)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            pass

    ffmpeg_path = find_ffmpeg()
    if not ffmpeg_path:
        return None

    duration = probe_video_duration(ffmpeg_path, file_path)
    if not duration or duration <= 0:
        return None
    return int(file_path.stat().st_size * 8 / duration)


def estimate_video_duration_seconds(file_path: Path) -> float | None:
    cache_key = video_duration_cache_key(file_path)
    with _video_duration_cache_lock:
        if cache_key in _video_duration_cache:
            return _video_duration_cache[cache_key]

    ffprobe_path = find_ffprobe()
    if ffprobe_path:
        command = [
            ffprobe_path,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "json",
            str(file_path),
        ]
        try:
            result = subprocess.run(command, check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            payload = json.loads((result.stdout or b"{}").decode("utf-8", errors="replace"))
            format_info = payload.get("format", {})
            duration_value = format_info.get("duration")
            if duration_value not in (None, "", "N/A"):
                duration = float(duration_value)
                if duration > 0:
                    with _video_duration_cache_lock:
                        _video_duration_cache[cache_key] = duration
                    return duration
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            pass

    ffmpeg_path = find_ffmpeg()
    if not ffmpeg_path:
        with _video_duration_cache_lock:
            _video_duration_cache[cache_key] = None
        return None
    duration = probe_video_duration(ffmpeg_path, file_path)
    with _video_duration_cache_lock:
        _video_duration_cache[cache_key] = duration
    return duration


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


def parse_optional_int(value: str | None) -> int | None:
    if value is None or value == "":
        return None
    try:
        parsed = int(float(value))
    except ValueError:
        return None
    return parsed if parsed > 0 else None


def parse_optional_float(value: str | None) -> float | None:
    if value is None or value == "":
        return None
    try:
        parsed = float(value)
    except ValueError:
        return None
    return parsed if parsed >= 0 else None


def video_duration_cache_key(file_path: Path) -> str:
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


def format_duration_label(seconds: float | None) -> str:
    if seconds is None or seconds <= 0:
        return "Unknown"

    total_seconds = int(round(seconds))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours > 0:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


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


class MediaPrepareJob:
    def __init__(self, file_path: Path) -> None:
        self.file_path = file_path
        self.state = "running"
        self.phase = "Queued"
        self.progress = 0
        self.detail = ""
        self.error = ""
        self.lock = threading.Lock()
        self.thread = threading.Thread(target=self._run, name=f"media-prepare-{file_path.name}", daemon=True)
        self.thread.start()

    def update(self, phase: str, progress: int, detail: str = "") -> None:
        with self.lock:
            self.phase = phase
            self.progress = max(0, min(100, int(progress)))
            self.detail = detail

    def snapshot(self) -> dict[str, object]:
        with self.lock:
            return {
                "state": self.state,
                "phase": self.phase,
                "progress": self.progress,
                "detail": self.detail,
                "error": self.error,
            }

    def _run(self) -> None:
        try:
            if self.file_path.suffix.lower() in DIRECT_PLAY_EXTENSIONS:
                self.update("Ready for browser playback", 100)
            elif self.file_path.suffix.lower() in REMUX_TO_MP4_EXTENSIONS:
                remux_video_to_mp4(self.file_path, self.update)
            else:
                abort(404)
        except Exception as error:
            with self.lock:
                self.state = "error"
                self.phase = "Preparation failed"
                self.error = str(error)
                self.progress = max(self.progress, 1)
            return

        with self.lock:
            self.state = "complete"
            self.phase = "Ready for playback"
            self.progress = 100


def prepare_media_status(file_path: Path) -> dict[str, object]:
    if file_path.suffix.lower() in DIRECT_PLAY_EXTENSIONS:
        return {"state": "complete", "phase": "Ready for browser playback", "progress": 100, "detail": "", "error": ""}
    if file_path.suffix.lower() in REMUX_TO_MP4_EXTENSIONS and remux_cache_is_ready(file_path):
        return {"state": "complete", "phase": "Ready cached MP4", "progress": 100, "detail": "", "error": ""}

    job_key = media_prepare_job_key(file_path)
    with _media_prepare_jobs_lock:
        job = _media_prepare_jobs.get(job_key)
        if job is None or job.snapshot()["state"] == "error":
            job = MediaPrepareJob(file_path)
            _media_prepare_jobs[job_key] = job
        return job.snapshot()


def media_prepare_job_key(file_path: Path) -> str:
    stat = file_path.stat()
    fingerprint = "\n".join([file_path.as_posix(), str(stat.st_size), str(stat.st_mtime_ns), file_path.suffix.lower()])
    return hashlib.sha1(fingerprint.encode("utf-8")).hexdigest()


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


def find_ffprobe() -> str | None:
    global _ffprobe_lookup_cache

    configured_ffprobe = os.environ.get("FFPROBE_BIN")
    if _ffprobe_lookup_cache and _ffprobe_lookup_cache[0] == configured_ffprobe:
        return _ffprobe_lookup_cache[1]

    if configured_ffprobe and Path(configured_ffprobe).is_file():
        _ffprobe_lookup_cache = (configured_ffprobe, configured_ffprobe)
        return configured_ffprobe

    system_ffprobe = shutil.which("ffprobe")
    if system_ffprobe:
        _ffprobe_lookup_cache = (configured_ffprobe, system_ffprobe)
        return system_ffprobe

    ffmpeg_path = find_ffmpeg()
    if ffmpeg_path:
        candidate = Path(ffmpeg_path).with_name("ffprobe")
        if candidate.is_file():
            resolved = str(candidate)
            _ffprobe_lookup_cache = (configured_ffprobe, resolved)
            return resolved

    _ffprobe_lookup_cache = (configured_ffprobe, None)
    return None


def has_ffmpeg_encoder(ffmpeg_path: str, encoder_name: str) -> bool:
    cache_key = (ffmpeg_path, encoder_name)
    cached = _ffmpeg_encoder_cache.get(cache_key)
    if cached is not None:
        return cached

    try:
        result = subprocess.run(
            [ffmpeg_path, "-hide_banner", "-encoders"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError:
        _ffmpeg_encoder_cache[cache_key] = False
        return False

    encoders = (result.stdout or b"") + (result.stderr or b"")
    available = encoder_name.encode("utf-8") in encoders
    _ffmpeg_encoder_cache[cache_key] = available
    return available


def select_av1_encoder(ffmpeg_path: str) -> str | None:
    if has_ffmpeg_encoder(ffmpeg_path, "libsvtav1"):
        return "libsvtav1"
    if has_ffmpeg_encoder(ffmpeg_path, "libaom-av1"):
        return "libaom-av1"
    return None


def av1_target_bitrates(file_path: Path, bandwidth_bps: int | None) -> tuple[int, int]:
    reference_bps = bandwidth_bps or estimate_average_bitrate_bps(file_path) or 1_000_000
    target_total_bps = max(int(reference_bps * AV1_TRANSCODE_TARGET_RATIO), 128_000)
    audio_bps = max(min(target_total_bps // 10, 128_000), 32_000)
    if target_total_bps - audio_bps < 80_000:
        audio_bps = max(16_000, min(audio_bps, max(target_total_bps // 4, 16_000)))
    video_bps = max(target_total_bps - audio_bps, 80_000)
    return video_bps, audio_bps


def build_av1_transcode_command(file_path: Path, bandwidth_bps: int | None, start_seconds: float = 0.0) -> list[str]:
    ffmpeg_path = find_ffmpeg()
    if not ffmpeg_path:
        abort(503, description="ffmpeg is required to stream AV1 video")

    encoder = select_av1_encoder(ffmpeg_path)
    if not encoder:
        abort(503, description="A supported AV1 encoder is required for realtime transcoding")

    video_bps, audio_bps = av1_target_bitrates(file_path, bandwidth_bps)
    command = [
        ffmpeg_path,
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        f"{max(start_seconds, 0.0):.3f}",
        "-i",
        str(file_path),
        "-map",
        "0:v:0",
        "-map",
        "0:a:0?",
        "-dn",
        "-sn",
        "-c:v",
        encoder,
        "-pix_fmt",
        "yuv420p",
        "-tag:v",
        "av01",
        "-c:a",
        "aac",
        "-b:a",
        str(audio_bps),
        "-movflags",
        "+frag_keyframe+empty_moov+default_base_moof",
        "-f",
        "mp4",
        "pipe:1",
    ]

    if encoder == "libsvtav1":
        command[command.index("-c:v") + 2 : command.index("-pix_fmt")] = [
            "-preset",
            str(max(min(AV1_TRANSCODE_PRESET, 13), -2)),
            "-svtav1-params",
            "rc=1",
            "-b:v",
            str(video_bps),
        ]
    else:
        command[command.index("-c:v") + 2 : command.index("-pix_fmt")] = [
            "-cpu-used",
            str(max(min(AV1_TRANSCODE_PRESET, 8), 0)),
            "-b:v",
            str(video_bps),
            "-maxrate",
            str(video_bps),
            "-bufsize",
            str(max(video_bps * 2, video_bps + audio_bps)),
        ]

    return command


class Av1TranscodeSession:
    def __init__(self, cache_key: str, file_path: Path, bandwidth_bps: int | None, start_seconds: float) -> None:
        self.cache_key = cache_key
        self.command = build_av1_transcode_command(file_path, bandwidth_bps, max(start_seconds, 0.0))
        self.cache_dir = Path(tempfile.gettempdir()) / "videos-av1-transcode"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.output_path = self.cache_dir / f"{cache_key}.mp4"
        self.output_path.unlink(missing_ok=True)
        self.output_path.touch(exist_ok=True)
        self.condition = threading.Condition()
        self.last_access = time.monotonic()
        self.bytes_written = 0
        self.completed = False
        self.failed = False
        self.active_readers = 0
        self.process: subprocess.Popen[bytes] | None = None
        self.writer_thread = threading.Thread(target=self._pump_stdout, name=f"av1-transcode-{cache_key[:8]}", daemon=True)
        self._start()

    def _start(self) -> None:
        try:
            self.process = subprocess.Popen(
                self.command,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
            )
        except OSError:
            self.failed = True
            self.completed = True
            self._notify_waiters()
            return

        self.writer_thread.start()

    def _pump_stdout(self) -> None:
        assert self.process is not None
        try:
            assert self.process.stdout is not None
            with self.output_path.open("wb") as output_file:
                while True:
                    chunk = self.process.stdout.read(64 * 1024)
                    if not chunk:
                        break
                    output_file.write(chunk)
                    output_file.flush()
                    with self.condition:
                        self.bytes_written += len(chunk)
                        self.last_access = time.monotonic()
                        self.condition.notify_all()
        finally:
            exit_code = self.process.wait()
            with self.condition:
                self.completed = True
                self.failed = exit_code != 0 and self.bytes_written == 0
                self.last_access = time.monotonic()
                self.condition.notify_all()

    def _notify_waiters(self) -> None:
        with self.condition:
            self.condition.notify_all()

    def wait_for_preload(self, threshold_bytes: int, timeout_seconds: float) -> bool:
        deadline = time.monotonic() + timeout_seconds
        with self.condition:
            while self.bytes_written < threshold_bytes and not self.completed and not self.failed:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self.condition.wait(timeout=remaining)
            return self.bytes_written >= threshold_bytes and not self.failed

    def open_reader(self):
        with self.condition:
            self.active_readers += 1
            self.last_access = time.monotonic()

    def close_reader(self) -> None:
        with self.condition:
            self.active_readers = max(self.active_readers - 1, 0)
            self.last_access = time.monotonic()
            self.condition.notify_all()

    def stream(self):
        def generate():
            offset = 0
            self.open_reader()
            try:
                with self.output_path.open("rb") as input_file:
                    while True:
                        with self.condition:
                            available = self.bytes_written
                            completed = self.completed
                            failed = self.failed
                        if failed:
                            abort(503, description="Unable to build AV1 stream")
                        if offset < available:
                            input_file.seek(offset)
                            chunk = input_file.read(min(64 * 1024, available - offset))
                            if chunk:
                                offset += len(chunk)
                                yield chunk
                                continue
                        if completed:
                            break
                        with self.condition:
                            self.condition.wait(timeout=0.2)
            finally:
                self.close_reader()

        return generate()

    def expired(self) -> bool:
        age = time.monotonic() - self.last_access
        return self.completed and self.active_readers == 0 and age > AV1_SESSION_TTL_SECONDS


def prune_expired_av1_sessions() -> None:
    with _av1_sessions_lock:
        expired_keys = [key for key, session in _av1_sessions.items() if session.expired()]
        for key in expired_keys:
            session = _av1_sessions.pop(key)
            session.output_path.unlink(missing_ok=True)


def get_or_create_av1_session(file_path: Path, bandwidth_bps: int | None, start_seconds: float = 0.0) -> Av1TranscodeSession:
    prune_expired_av1_sessions()
    cache_key = av1_session_key_for(file_path, bandwidth_bps, start_seconds)
    with _av1_sessions_lock:
        session = _av1_sessions.get(cache_key)
        if session is not None and not session.failed:
            return session

        session = Av1TranscodeSession(cache_key, file_path, bandwidth_bps, start_seconds)
        _av1_sessions[cache_key] = session
        return session


def av1_session_key_for(file_path: Path, bandwidth_bps: int | None, start_seconds: float) -> str:
    stat = file_path.stat()
    bandwidth_bucket = 0 if bandwidth_bps is None else int(round(bandwidth_bps / 100_000.0) * 100_000)
    start_bucket = int(round(max(start_seconds, 0.0) * 2.0))
    fingerprint = "\n".join(
        [
            file_path.as_posix(),
            str(stat.st_size),
            str(int(stat.st_mtime_ns)),
            str(bandwidth_bucket),
            str(start_bucket),
            str(AV1_TRANSCODE_PRESET),
        ]
    )
    return hashlib.sha1(fingerprint.encode("utf-8")).hexdigest()


def av1_transcode_response(file_path: Path, bandwidth_bps: int | None) -> Response:
    session = get_or_create_av1_session(file_path, bandwidth_bps, parse_optional_float(request.args.get("start_seconds")) or 0.0)
    if session.failed and session.bytes_written == 0:
        abort(503, description="Unable to build AV1 stream")
    return Response(
        session.stream(),
        mimetype="video/mp4",
        direct_passthrough=True,
        headers={"Cache-Control": "no-store, max-age=0"},
    )


def remux_video_to_mp4(file_path: Path, progress_callback: Callable[[str, int, str], None] | None = None) -> Path:
    ffmpeg_path = find_ffmpeg()
    if not ffmpeg_path:
        abort(503, description="ffmpeg is required to remux video files")

    cache_dir = Path(tempfile.gettempdir()) / "videos-mkv-remux"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = remux_cache_path(cache_dir, file_path)
    ready_path = remux_ready_path(cache_path)
    if cache_path.is_file() and ready_path.is_file():
        if progress_callback:
            progress_callback("Ready cached MP4", 100, "")
        return cache_path
    cache_path.unlink(missing_ok=True)
    ready_path.unlink(missing_ok=True)

    thread_id = threading.get_ident()
    temp_path = cache_path.with_name(f"{cache_path.stem}.{os.getpid()}.{thread_id}.tmp.mp4")
    if progress_callback:
        progress_callback("Inspecting audio stream", 8, "")
    audio_codec = detect_first_audio_codec(ffmpeg_path, file_path)
    audio_codec_args = remux_audio_codec_args_for_codec(audio_codec)
    audio_detail = "copying audio" if audio_codec_args == ["-c:a", "copy"] else "transcoding audio to AAC"
    if progress_callback:
        progress_callback("Measuring video duration", 12, audio_detail)
    duration = estimate_video_duration_seconds(file_path) if progress_callback else None
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
        *audio_codec_args,
        "-movflags",
        "+faststart",
        "-f",
        "mp4",
        str(temp_path),
    ]
    if progress_callback:
        command[5:5] = ["-nostats", "-progress", "pipe:1"]
        progress_callback("Preparing MP4 for browser playback", 15, audio_detail)
    try:
        if progress_callback:
            run_remux_command_with_progress(command, duration, progress_callback, audio_detail)
        else:
            subprocess.run(command, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except subprocess.CalledProcessError as error:
        temp_path.unlink(missing_ok=True)
        message = (error.stderr or b"remux failed").decode("utf-8", errors="replace").strip()
        abort(500, description=message or "remux failed")

    temp_path.replace(cache_path)
    ready_path.write_text(
        json.dumps({"source": file_path.as_posix(), "cache": cache_path.name, "created": time.time()}),
        encoding="utf-8",
    )
    return cache_path


def remux_audio_codec_args(ffmpeg_path: str, file_path: Path) -> list[str]:
    return remux_audio_codec_args_for_codec(detect_first_audio_codec(ffmpeg_path, file_path))


def remux_audio_codec_args_for_codec(audio_codec: str | None) -> list[str]:
    if audio_codec in BROWSER_PLAYABLE_MP4_AUDIO_CODECS:
        return ["-c:a", "copy"]
    return ["-c:a", "aac", "-b:a", "128k"]


def run_remux_command_with_progress(
    command: list[str],
    duration_seconds: float | None,
    progress_callback: Callable[[str, int, str], None],
    detail: str,
) -> None:
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        text=True,
        bufsize=1,
    )
    output_lines = []
    assert process.stdout is not None
    for line in process.stdout:
        stripped = line.strip()
        key, _, value = stripped.partition("=")
        if key == "out_time_ms" and duration_seconds and duration_seconds > 0:
            try:
                out_seconds = int(value) / 1_000_000
            except ValueError:
                continue
            progress = 15 + int(min(out_seconds / duration_seconds, 1.0) * 80)
            progress_callback("Preparing MP4 for browser playback", progress, detail)
        elif key == "progress" and value == "end":
            progress_callback("Finalizing prepared media", 96, detail)
        elif stripped:
            output_lines.append(stripped)

    exit_code = process.wait()
    if exit_code != 0:
        stderr = "\n".join(output_lines).encode("utf-8")
        raise subprocess.CalledProcessError(exit_code, command, stderr=stderr)


def detect_first_audio_codec(ffmpeg_path: str, file_path: Path) -> str | None:
    command = [ffmpeg_path, "-hide_banner", "-i", str(file_path)]
    try:
        result = subprocess.run(command, check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except OSError:
        return None

    output = ((result.stdout or b"") + (result.stderr or b"")).decode("utf-8", errors="replace")
    match = re.search(r"Audio:\s*([^,\s]+)", output)
    if not match:
        return None
    return match.group(1).strip().lower()


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


def remux_cache_is_ready(file_path: Path) -> bool:
    cache_path = remux_cache_path(Path(tempfile.gettempdir()) / "videos-mkv-remux", file_path)
    return cache_path.is_file() and remux_ready_path(cache_path).is_file()


def remux_ready_path(cache_path: Path) -> Path:
    return cache_path.with_suffix(f"{cache_path.suffix}.ready")


def cleanup_stale_video_cache(root: Path, previous_relative: str | None, current_relative: str) -> None:
    if not previous_relative or previous_relative == current_relative:
        return

    try:
        previous_path = safe_resolve(root, previous_relative)
    except Exception:
        return

    if previous_path.suffix.lower() not in REMUX_TO_MP4_EXTENSIONS:
        return
    if not previous_path.is_file():
        return

    cache_path = remux_cache_path(Path(tempfile.gettempdir()) / "videos-mkv-remux", previous_path)
    cache_path.unlink(missing_ok=True)
    remux_ready_path(cache_path).unlink(missing_ok=True)


def parse_range(range_header: str, file_size: int) -> tuple[int, int]:
    unit, _, range_value = range_header.partition("=")
    if unit.strip().lower() != "bytes" or "-" not in range_value:
        abort_range_not_satisfiable(file_size)

    start_value, _, end_value = range_value.partition("-")
    try:
        if start_value == "":
            suffix_length = int(end_value)
            if suffix_length <= 0:
                abort_range_not_satisfiable(file_size)
            start = max(file_size - suffix_length, 0)
            end = file_size - 1
        else:
            start = int(start_value)
            end = int(end_value) if end_value else file_size - 1
    except ValueError:
        abort_range_not_satisfiable(file_size)

    if start < 0 or end >= file_size or start > end:
        abort_range_not_satisfiable(file_size)
    return start, end


def abort_range_not_satisfiable(file_size: int) -> None:
    abort(Response(status=416, headers={"Content-Range": f"bytes */{file_size}", "Accept-Ranges": "bytes"}))


def stream_file(file_path: Path, start: int, end: int, chunk_size: int = VIDEO_STREAM_CHUNK_SIZE):
    with file_path.open("rb") as file:
        file.seek(start)
        remaining = end - start + 1
        while remaining > 0:
            data = file.read(min(chunk_size, remaining))
            if not data:
                break
            remaining -= len(data)
            yield data


def video_stream_headers(file_size: int) -> dict[str, str]:
    return {
        "Accept-Ranges": "bytes",
        "Cache-Control": "public, max-age=3600",
        "Content-Disposition": "inline",
        "X-Content-Type-Options": "nosniff",
        "Content-Length": str(file_size),
    }


def ranged_file_response(file_path: Path, content_type: str) -> Response:
    file_size = file_path.stat().st_size
    range_header = request.headers.get("Range")
    headers = video_stream_headers(file_size)

    if file_size == 0:
        return Response(b"", mimetype=content_type, headers=headers)

    if not range_header:
        return Response(
            stream_file(file_path, 0, file_size - 1),
            mimetype=content_type,
            headers=headers,
            direct_passthrough=True,
        )

    start, end = parse_range(range_header, file_size)
    length = end - start + 1
    headers.update(
        {
            "Content-Range": f"bytes {start}-{end}/{file_size}",
            "Content-Length": str(length),
        }
    )
    return Response(
        stream_file(file_path, start, end),
        status=206,
        mimetype=content_type,
        headers=headers,
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
