# Remote Video Browser

A lightweight Flask app for browsing a video folder in the browser and playing videos with native controls.

## Requirements

- Python 3.11+
- FFmpeg and `ffprobe`
- A modern browser

## Install

```bash
conda activate videos
python -m pip install -r requirements.txt
```

## Run

```bash
conda run -n videos python app.py /path/to/videos
```

Default port: `8000`

To use the helper script:

```bash
./launch.sh
```

## Test

```bash
conda run -n videos python -m pytest -q
```

## Notes

- Supported files include: `.mp4`, `.m4v`, `.webm`, `.ogg`, `.ogv`, `.mkv`, `.mov`, `.avi`, `.wmv`, `.ts`, `.m2ts`
- Some formats are remuxed to MP4 for browser playback
