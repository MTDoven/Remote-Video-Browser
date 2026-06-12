# Remote Video Browser

A lightweight Flask app for browsing a video folder in the browser and playing videos with native controls.

## Requirements

- Python 3.11+
- FFmpeg. If no system FFmpeg is available, `imageio-ffmpeg` from `requirements.txt` is used as a fallback.
- A modern browser

## Install

```bash
uv venv .venv
uv pip install -r requirements.txt
```

## Run

```bash
.venv/bin/python app.py /path/to/videos
```

Default port: `8000`

To use the helper script:

```bash
./launch.sh
```

## Test

```bash
.venv/bin/python -m pytest -q
```

## Notes

- Supported files include: `.mp4`, `.m4v`, `.webm`, `.ogg`, `.ogv`, `.mkv`, `.mov`, `.avi`, `.wmv`, `.ts`, `.m2ts`
- Some formats are remuxed to MP4 for browser playback
