# Remote Video Browser

A local Flask video browser for native playback. The server scans one directory level at a time and serves supported video files directly to the browser's built-in player.

## Features

- Directory browsing with breadcrumbs and one-level-at-a-time navigation.
- Native browser playback with standard `<video>` controls.
- Common browser-playable video containers are supported directly.
- Non-native containers such as MKV, MOV, AVI, WMV, TS, and M2TS are remuxed to a temporary MP4 when needed.
- Each video gets a content-aware thumbnail chosen from an informative frame rather than a blank black or white frame.
- A dedicated preview button opens a randomly assembled 3x3 contact sheet of video frames.
- Thumbnail generation is cached and lightly prewarmed in the background so browsing stays responsive.
- Range responses for streaming-friendly seeking.
- Mobile browsers are automatically routed to a dedicated mobile layout.

## Requirements

- Python 3.11 or newer.
- A modern browser with video playback support.
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

`.mp4`, `.m4v`, `.webm`, `.ogg`, `.ogv`, `.mkv`, `.mov`, `.avi`, `.wmv`, `.ts`, `.m2ts`.

Direct-play containers are served as-is. Containers that Chrome usually does not handle directly are remuxed to a temporary MP4, so the underlying video/audio codecs still need to be browser-decodable.
