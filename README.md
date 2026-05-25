# AV1 Video Browser

A local Flask video browser for native playback. The server scans one directory level at a time and serves AV1-encoded video files directly to the browser's built-in player.

## Features

- Directory browsing with breadcrumbs and one-level-at-a-time navigation.
- Native browser playback with standard `<video>` controls.
- MKV files are remuxed to a temporary MP4 when needed.
- Range responses for streaming-friendly seeking.
- Mobile browsers are automatically routed to a dedicated mobile layout.

## Requirements

- Python 3.11 or newer.
- A modern browser with AV1 playback support.
- FFmpeg available on `PATH`, or via `FFMPEG_BIN`, for MKV remuxing.

## Installation

```bash
conda activate videos
python -m pip install -r requirements.txt
```

## Running

```bash
conda run -n videos python app.py /path/to/videos
```

The default address is `http://127.0.0.1:8000`. To choose a different port:

```bash
conda run -n videos python app.py /path/to/videos --port 9000
```

The convenience launcher can be used after editing its default directory if needed:

```bash
./launch.sh
```

## Testing

```bash
conda run -n videos python -m pytest -q
```

## Supported Files

The browser lists files with these extensions:

`.mp4`, `.m4v`, `.webm`, `.ogg`, `.ogv`.
`.mkv` files are accepted too and are remuxed to a temporary MP4 for playback.

The files are served as-is, so the video container and codec must already be supported by the browser.
