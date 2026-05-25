from pathlib import Path

import pytest

from app import (
    create_app,
    list_directory,
    parse_range,
)


def make_video(root: Path, relative_path: str, content: bytes = b"0123456789") -> Path:
    path = root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def test_list_directory_returns_only_current_level(tmp_path: Path) -> None:
    make_video(tmp_path, "clips/demo.mp4")
    make_video(tmp_path, "movie.mkv")
    make_video(tmp_path, "root.mp4")
    (tmp_path / "notes.txt").write_text("ignore me")

    app = create_app(tmp_path)
    with app.test_request_context():
        listing = list_directory(tmp_path, tmp_path)

    assert [folder["relative_path"] for folder in listing["folders"]] == ["clips"]
    assert [video["relative_path"] for video in listing["videos"]] == ["movie.mkv", "root.mp4"]
    assert listing["videos"][0]["folder"] == "."
    assert listing["videos"][0]["size"] == 10
    assert "modified_label" in listing["videos"][0]
    assert listing["videos"][0]["page_url"].endswith("/?dir=&v=movie.mkv")
    assert listing["videos"][0]["media_url"].endswith("/media/movie.mkv")


def test_index_route_loads_requested_directory_and_selected_video(tmp_path: Path) -> None:
    make_video(tmp_path, "clips/demo.mp4")
    make_video(tmp_path, "clips/deeper/nested.mp4")
    client = create_app(tmp_path).test_client()

    response = client.get("/?dir=clips&v=clips/demo.mp4")

    assert response.status_code == 200
    assert b"clips/demo.mp4" in response.data
    assert b"Native playback" in response.data
    assert b"/media/clips/demo.mp4" in response.data


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


@pytest.mark.parametrize(
    "header",
    ["items=0-1", "bytes=abc-def", "bytes=9-2", "bytes=100-101", "bytes=-0"],
)
def test_parse_range_rejects_invalid_ranges(header: str) -> None:
    app = create_app(Path.cwd())
    with app.test_request_context():
        with pytest.raises(Exception):
            parse_range(header, 10)
