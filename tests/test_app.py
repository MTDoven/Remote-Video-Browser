from pathlib import Path

import pytest

from app import (
    create_app,
    list_directory,
    parse_range,
    remux_audio_codec_args,
    remux_cache_path,
    remux_ready_path,
)


def make_video(root: Path, relative_path: str, content: bytes = b"0123456789") -> Path:
    path = root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def test_list_directory_returns_only_current_level(tmp_path: Path) -> None:
    make_video(tmp_path, "clips/demo.mp4")
    make_video(tmp_path, "movie.mkv")
    make_video(tmp_path, "extra.mov")
    make_video(tmp_path, "root.mp4")
    (tmp_path / "notes.txt").write_text("ignore me")

    app = create_app(tmp_path)
    with app.test_request_context():
        listing = list_directory(tmp_path, tmp_path)

    assert [folder["relative_path"] for folder in listing["folders"]] == ["clips"]
    assert [video["relative_path"] for video in listing["videos"]] == ["extra.mov", "movie.mkv", "root.mp4"]
    assert listing["videos"][0]["folder"] == "."
    assert listing["videos"][0]["size"] == 10
    assert "duration_label" in listing["videos"][0]
    assert listing["videos"][0]["page_url"].endswith("/?dir=&v=extra.mov")
    assert listing["videos"][0]["media_url"].endswith("/media/extra.mov")


def test_index_route_loads_requested_directory_and_selected_video(tmp_path: Path) -> None:
    make_video(tmp_path, "clips/demo.mp4")
    make_video(tmp_path, "clips/deeper/nested.mp4")
    client = create_app(tmp_path).test_client()

    response = client.get("/?dir=clips&v=clips/demo.mp4")

    assert response.status_code == 200
    assert b"clips/demo.mp4" in response.data
    assert b"Native playback" in response.data
    assert b"/media/clips/demo.mp4" in response.data
    assert b"/media-av1/clips/demo.mp4" in response.data
    assert b'preload="auto"' in response.data
    assert b'data-force-encode-av1="0"' in response.data
    assert b'data-source-duration-seconds=""' in response.data


def test_index_route_exposes_force_encode_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    make_video(tmp_path, "clips/demo.mp4")
    monkeypatch.setattr("app.FORCE_ENCODE_AV1", True)
    client = create_app(tmp_path).test_client()

    response = client.get("/?dir=clips&v=clips/demo.mp4")

    assert response.status_code == 200
    assert b'data-force-encode-av1="1"' in response.data


def test_mobile_user_agent_gets_mobile_template(tmp_path: Path) -> None:
    make_video(tmp_path, "clips/demo.mp4")
    client = create_app(tmp_path).test_client()

    response = client.get("/", headers={"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)"})

    assert response.status_code == 200
    assert b"Mobile playback" in response.data
    assert b"mobile.css" in response.data


def test_desktop_user_agent_keeps_desktop_template(tmp_path: Path) -> None:
    make_video(tmp_path, "clips/demo.mp4")
    client = create_app(tmp_path).test_client()

    response = client.get("/", headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64)"})

    assert response.status_code == 200
    assert b"Native playback" in response.data
    assert b"styles.css" in response.data


def test_media_route_serves_file_with_ranges(tmp_path: Path) -> None:
    video = make_video(tmp_path, "demo.mp4")
    client = create_app(tmp_path).test_client()

    response = client.get("/media/demo.mp4", headers={"Range": "bytes=1-3"})

    assert response.status_code == 206
    assert response.data == b"123"
    assert response.headers["Content-Range"] == "bytes 1-3/10"
    assert response.headers["Accept-Ranges"] == "bytes"
    assert response.headers["Cache-Control"] == "public, max-age=3600"


def test_media_route_reports_unsatisfied_range_size(tmp_path: Path) -> None:
    make_video(tmp_path, "demo.mp4")
    client = create_app(tmp_path).test_client()

    response = client.get("/media/demo.mp4", headers={"Range": "bytes=100-101"})

    assert response.status_code == 416
    assert response.headers["Content-Range"] == "bytes */10"
    assert response.headers["Accept-Ranges"] == "bytes"


def test_media_route_serves_empty_video_without_negative_range(tmp_path: Path) -> None:
    make_video(tmp_path, "empty.mp4", b"")
    client = create_app(tmp_path).test_client()

    response = client.get("/media/empty.mp4")

    assert response.status_code == 200
    assert response.data == b""
    assert response.headers["Content-Length"] == "0"
    assert response.headers["Accept-Ranges"] == "bytes"


def test_media_route_remuxes_mkv_files_to_temp_mp4(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = make_video(tmp_path, "movie.mkv", b"mkv-bytes")
    fake_ffmpeg = tmp_path / "ffmpeg"
    fake_ffmpeg.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "input=\"\"\n"
        "output=\"${@: -1}\"\n"
        "while (($#)); do\n"
        "  if [[ \"$1\" == \"-i\" ]]; then\n"
        "    input=\"$2\"\n"
        "    shift 2\n"
        "    continue\n"
        "  fi\n"
        "  shift\n"
        "done\n"
        "cp \"$input\" \"$output\"\n"
    )
    fake_ffmpeg.chmod(0o755)
    monkeypatch.setenv("FFMPEG_BIN", str(fake_ffmpeg))

    client = create_app(tmp_path).test_client()
    response = client.get("/media/movie.mkv")

    assert response.status_code == 200
    assert response.data == b"mkv-bytes"
    assert response.headers["Content-Type"].startswith("video/mp4")


def test_media_route_remuxes_mov_files_to_temp_mp4(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = make_video(tmp_path, "clip.mov", b"mov-bytes")
    fake_ffmpeg = tmp_path / "ffmpeg"
    fake_ffmpeg.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "input=\"\"\n"
        "output=\"${@: -1}\"\n"
        "while (($#)); do\n"
        "  if [[ \"$1\" == \"-i\" ]]; then\n"
        "    input=\"$2\"\n"
        "    shift 2\n"
        "    continue\n"
        "  fi\n"
        "  shift\n"
        "done\n"
        "cp \"$input\" \"$output\"\n"
    )
    fake_ffmpeg.chmod(0o755)
    monkeypatch.setenv("FFMPEG_BIN", str(fake_ffmpeg))

    client = create_app(tmp_path).test_client()
    response = client.get("/media/clip.mov")

    assert response.status_code == 200
    assert response.data == b"mov-bytes"
    assert response.headers["Content-Type"].startswith("video/mp4")


def test_media_route_rebuilds_unmarked_remux_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = make_video(tmp_path, "movie.mkv", b"fresh-mkv-bytes")
    fake_ffmpeg = tmp_path / "ffmpeg"
    fake_ffmpeg.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "input=\"\"\n"
        "output=\"${@: -1}\"\n"
        "while (($#)); do\n"
        "  if [[ \"$1\" == \"-i\" ]]; then\n"
        "    input=\"$2\"\n"
        "    shift 2\n"
        "    continue\n"
        "  fi\n"
        "  shift\n"
        "done\n"
        "cp \"$input\" \"$output\"\n"
    )
    fake_ffmpeg.chmod(0o755)
    monkeypatch.setenv("FFMPEG_BIN", str(fake_ffmpeg))
    monkeypatch.setattr("app.tempfile.gettempdir", lambda: str(tmp_path))

    cache_path = remux_cache_path(tmp_path / "videos-mkv-remux", source)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_bytes(b"stale-broken-cache")

    client = create_app(tmp_path).test_client()
    response = client.get("/media/movie.mkv")

    assert response.status_code == 200
    assert response.data == b"fresh-mkv-bytes"
    assert cache_path.read_bytes() == b"fresh-mkv-bytes"
    assert remux_ready_path(cache_path).is_file()


def test_opus_audio_is_remuxed_to_aac_without_reencoding_video(tmp_path: Path) -> None:
    source = make_video(tmp_path, "movie.mkv")
    fake_ffmpeg = tmp_path / "ffmpeg"
    fake_ffmpeg.write_text(
        "#!/usr/bin/env bash\n"
        "printf 'Stream #0:1: Audio: opus, 48000 Hz, mono\\n' >&2\n"
        "exit 1\n"
    )
    fake_ffmpeg.chmod(0o755)

    assert remux_audio_codec_args(str(fake_ffmpeg), source) == ["-c:a", "aac", "-b:a", "128k"]


def test_switching_videos_deletes_previous_temp_remux_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    make_video(tmp_path, "movie.mkv", b"mkv-bytes")
    make_video(tmp_path, "next.mp4", b"next-bytes")
    fake_ffmpeg = tmp_path / "ffmpeg"
    fake_ffmpeg.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "input=\"\"\n"
        "output=\"${@: -1}\"\n"
        "while (($#)); do\n"
        "  if [[ \"$1\" == \"-i\" ]]; then\n"
        "    input=\"$2\"\n"
        "    shift 2\n"
        "    continue\n"
        "  fi\n"
        "  shift\n"
        "done\n"
        "cp \"$input\" \"$output\"\n"
    )
    fake_ffmpeg.chmod(0o755)
    monkeypatch.setenv("FFMPEG_BIN", str(fake_ffmpeg))
    monkeypatch.setattr("app.tempfile.gettempdir", lambda: str(tmp_path))

    client = create_app(tmp_path).test_client()
    first_response = client.get("/media/movie.mkv")
    assert first_response.status_code == 200

    cache_dir = tmp_path / "videos-mkv-remux"
    cache_files = list(cache_dir.glob("*.mp4"))
    assert len(cache_files) == 1
    cached_path = cache_files[0]
    assert cached_path.is_file()

    client.set_cookie("current_video", "movie.mkv")
    response = client.get("/?v=next.mp4")

    assert response.status_code == 200
    assert not cached_path.exists()


def test_index_route_ignores_missing_previous_video_cookie(tmp_path: Path) -> None:
    make_video(tmp_path, "present.mp4")
    client = create_app(tmp_path).test_client()
    client.set_cookie("current_video", "missing.mkv")

    response = client.get("/")

    assert response.status_code == 200
    assert b"present.mp4" in response.data


def test_av1_media_route_streams_realtime_transcode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_video(tmp_path, "movie.mp4", b"source-bytes")
    args_file = tmp_path / "ffmpeg-args.txt"
    fake_ffmpeg = tmp_path / "ffmpeg"
    fake_ffmpeg.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "printf '%s\\n' \"$*\" > \"$FFMPEG_ARGS_FILE\"\n"
        "printf 'FAKE-AV1-STREAM'\n"
    )
    fake_ffmpeg.chmod(0o755)
    monkeypatch.setenv("FFMPEG_BIN", str(fake_ffmpeg))
    monkeypatch.setenv("FFMPEG_ARGS_FILE", str(args_file))
    monkeypatch.setattr("app.select_av1_encoder", lambda _ffmpeg_path: "libsvtav1")
    monkeypatch.setattr("app.AV1_PRELOAD_MIN_BYTES", 1)
    monkeypatch.setattr("app.AV1_PRELOAD_TIMEOUT_SECONDS", 1.0)

    client = create_app(tmp_path).test_client()
    preload_response = client.get("/media-av1/movie.mp4?bandwidth_bps=1000000&start_seconds=12.5&preload=1")
    response = client.get("/media-av1/movie.mp4?bandwidth_bps=1000000&start_seconds=12.5")

    assert preload_response.status_code == 200
    assert preload_response.is_json
    assert preload_response.get_json()["ready"] is True
    assert response.status_code == 200
    assert response.data == b"FAKE-AV1-STREAM"

    args = args_file.read_text()
    assert "-ss 12.500" in args
    assert "-c:v libsvtav1" in args
    assert "-svtav1-params rc=1" in args
    assert "-preset 5" in args
    assert "-b:v 675000" in args
    assert "-b:a 75000" in args


@pytest.mark.parametrize(
    "header",
    ["items=0-1", "bytes=abc-def", "bytes=9-2", "bytes=100-101", "bytes=-0"],
)
def test_parse_range_rejects_invalid_ranges(header: str) -> None:
    app = create_app(Path.cwd())
    with app.test_request_context():
        with pytest.raises(Exception):
            parse_range(header, 10)
