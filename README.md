# Remote Video Browser

A local Flask video browser for native playback. The server scans one directory level at a time and serves supported video files directly to the browser's built-in player.

## Features

- Directory browsing with breadcrumbs and one-level-at-a-time navigation.
- Native browser playback with standard `<video>` controls.
- Common browser-playable video containers are supported directly.
- Non-native containers such as MKV, MOV, AVI, WMV, TS, and M2TS are remuxed to a temporary MP4 when needed.
- When the selected video's bitrate and the estimated network bandwidth satisfy the adaptive trigger, playback can switch to a realtime AV1 transcode at a lower bitrate while keeping the same resolution.
- The AV1 path preloads a short buffer before the browser switches sources so startup stays smooth.
- Each video gets a content-aware thumbnail chosen from an informative frame rather than a blank black or white frame.
- A dedicated preview button opens a randomly assembled 3x3 contact sheet of video frames.
- Thumbnail generation is cached and lightly prewarmed in the background so browsing stays responsive.
- Range responses for streaming-friendly seeking.
- Mobile browsers are automatically routed to a dedicated mobile layout.

## Requirements

- Python 3.11 or newer.
- A modern browser with video playback support.
- FFmpeg available on `PATH`, or via `FFMPEG_BIN`, for MKV remuxing and AV1 realtime transcode output.
- `ffprobe` available on `PATH`, or via `FFPROBE_BIN`, for bitrate estimation. It is bundled with the same FFmpeg build in the `videos` conda environment.
- The default AV1 encoder preset is tuned for this machine class and can be overridden with `AV1_TRANSCODE_PRESET`.

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

## Adaptive AV1 Playback

- The browser estimates available bandwidth and compares it to the selected video's average bitrate.
- The browser preloads a short AV1 buffer first, then switches to the realtime stream when it is ready.
- The adaptive trigger follows the requested rule: AV1 is used when the selected video's bitrate is below 75% of the estimated bandwidth, or when `FORCE_ENCODE_AV1=1` is set.
- The AV1 stream targets about `75%` of the estimated network bandwidth.
- Set `FORCE_ENCODE_AV1=1` to force the adaptive AV1 path whenever the browser reports AV1 support, regardless of bandwidth.
- If the browser cannot play AV1, the app keeps the direct playback path so the video still works.
