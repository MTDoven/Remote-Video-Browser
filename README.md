# Video Library

A read-only video browser for an HDD-backed library, with item search, folder navigation, covers, contact sheets, and on-demand HLS playback. Desktop, tablet, and mobile share one responsive interface.

## Run

```bash
/root/server-videos/launch.sh
```

Open `http://SERVER_IP:18483`. The launcher uses the project's independent `.venv/bin/python`, reports occupied ports without terminating other processes, and stops its own media tasks on exit.

The existing `.venv` must be present. Install runtime dependencies only into this environment:

```bash
.venv/bin/python -m pip install -r requirements.txt
```

FFmpeg and FFprobe 7.0.2 are standalone executables in `bin`. Restore them with `bin/install_tools.sh`; the downloaded archive must match the pinned SHA-256. Versions, source, and license information are in `bin/TOOLS.json`. hls.js 1.7.3 and its license are vendored in `static/vendor`. No system installation, conda changes, uv, or external player CDN is needed.

## Configuration

| Environment variable | Default | Purpose |
| --- | --- | --- |
| `VIDEO_DIR` | `/data/file/AAASSSSS/` | Read-only source library |
| `CACHE_DIR` | Project `cache` | Writable SSD cache; existing symlinks are preserved |
| `PORT` | `18483` | HTTP port |
| `CACHE_GIB` | `32` | Persistent cache budget in GiB |
| `CACHE_MIN_FREE_GIB` | `16` | Minimum free space before eviction |

The source and cache directories must not overlap. Only one application instance may own a cache directory.

```bash
VIDEO_DIR=/path/to/videos CACHE_DIR=/path/to/ssd/cache PORT=18484 ./launch.sh
```

## Browsing and playback

- **Browse folders** is the default view. An **item** is a leaf directory containing videos. Search matches item names using case-insensitive substring matching. Relative paths distinguish duplicate names. Folder browsing also exposes videos in non-leaf directories. Scrolling loads successive batches of 48 entries until the list ends; there is no displayed page limit.
- Breadcrumb navigation and browser Back restore the previous list position. Loaded lists remain in browser memory, and positions are saved in the tab's session storage.
- **Recommendations** randomly orders unopened videos. Once every video has been opened, videos watched for less than 30 seconds come first, followed by fewer opens and shorter viewing durations. Rankings use the library's shared history, across devices, and a stable random seed while navigating a list.
- **Refresh recommendations** starts a new recommendation list with a new random seed. Opening a video, returning to the page, or refreshing the library keeps the already loaded recommendation cards; recommendations are not automatically reshuffled.
- SQLite stores the catalog. Startup checks file metadata in the background; **Refresh library** synchronizes additions, removals, replacements, and renames. Source symlinks are excluded.
- Playback copies video packets into keyframe-aligned fMP4/HLS windows. It never re-encodes video or changes resolution or bit depth. Supported Opus audio is copied; other playback sessions use AAC when audio conversion is needed.
- Native HLS is preferred when available; otherwise hls.js uses MSE. The device must support the video's actual codec and dimensions. Unsupported devices receive an error; no H.264 fallback is generated.
- Video and item cards display four frames at 20%, 40%, 60%, and 80% of the video in a 2-by-2 contact sheet, without cropping the composite. Item cards use the first video in the item. The detailed preview reuses the same image. Images are requested near visible cards and released when they leave the viewport. Seeking preserves active remux and player initialization, cancels queued obsolete prefetch, and reuses existing cache entries. The video element sizes itself to keep native controls inside the player for different aspect ratios.

## Viewing history

`cache/library.sqlite3` retains video opens, timestamps, source versions, viewing durations, and the actual watched intervals. History survives application restarts, library refreshes, and media-cache eviction. Repeated playback of the same interval counts toward viewing time; seeking past an interval does not. Open counts include videos selected even if playback is unsupported.

The browser submits progress every five seconds and on pause, seek, end, and close. Failed requests are retried while the page remains open; duplicate submissions are ignored. Abrupt browser termination or a lost network connection can lose progress that has not reached the server. Records are shared across devices without user accounts. `GET /api/history?page=1` returns recent opens with interval endpoints in seconds and minutes; timestamps are Unix seconds.

## Storage and scheduling

All application output stays in the cache: the catalog, images, source blocks, and playback windows. Source requests verify path boundaries, size, modification time, change time, device, and inode. Changed or missing files invalidate old media access and require a library refresh.

One prioritized reader handles HDD data access through shared 4MiB SSD blocks, with at most two adjacent blocks prefetched. Two media workers prioritize playback over previews. LRU eviction protects active playback windows and accounts for published files, the database, and reserved output space.

Published cache survives restart. Startup removes incomplete temporary output; normal shutdown stops work and retains valid objects. Insufficient capacity stops new cache work. Cold reads can still require HDD seeks, and files without usable MKV Cues need a packet scan before playback.

After ten seconds without media requests and with no active playback sessions, the server automatically generates missing four-frame previews, one video at a time. It pauses during playback or requested media work; a requested preview shares or promotes an existing task. Completed images persist across restarts. Idle caching stops at 80% of the total cache budget or when free space approaches the configured minimum, leaving room for playback. It does not pre-generate playback segments. Videos that fail preview generation are skipped and revisited on a later sweep.

## Code and validation

`app.py` is the entry point. `src` separates configuration, catalog, cache, viewing history, source reading, job scheduling, Matroska parsing, media processing, sessions, HTTP routes, and server lifecycle. `templates` and `static` contain the shared interface.

See [VALIDATION.md](VALIDATION.md) for measured results and device coverage. Actual Windows, iOS, iPadOS, and Android Chrome playback, touch seeking, and fullscreen still require verification on those devices. Long GOPs with software decoding can exceed the one-second seek recovery target.
