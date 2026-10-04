# Validation record

Recorded on 2026-10-04 using temporary fixtures and direct integration checks. Application source access was read-only; test tools were isolated inside the project. No system packages or conda environments were modified.

The results below describe the earlier implementation. The subsequent infinite scrolling, position restoration, four-frame card previews, idle preview caching, persistent history, manual recommendation refresh, early-seek handling, and player sizing changes have received source and syntax checks only. They have not been retested in a browser or on the listed client devices.

## Coverage

- Catalog: 2,526 directories, 2,518 items, and 8,330 videos; metadata-only indexing.
- Search: case folding, Unicode, literal special characters, duplicate item names, empty directories, non-leaf videos, and 48-entry pagination.
- Source integrity: fixture content hashes, sizes, modification times, and change times remained unchanged by the application. External additions, replacements, deletions, and renames were reflected after refresh.
- Version isolation: stale media URLs were rejected without invalidating a fresh version's cache.
- Playback: landscape and portrait AV1 10-bit, differing resolutions, absent audio, Opus copy, Opus-to-AAC conversion, AAC copy, end-of-video playback, repeated seeking, and rapid seek cancellation.
- Packet integrity: remuxed AV1 packet SHA-256 hashes matched the corresponding source packets. Audio/video start timestamps differed by less than 100ms.
- Browsers: real MSE playback in Linux Chromium 140 and Chrome for Testing 140; SPA navigation during playback, visible-card covers, contact sheets, a 390px touch layout, and unsupported-device feedback.
- Concurrency and cache: shared fragments across two sessions, one source read for eight identical concurrent block requests, foreground work during a preview, LRU eviction, active leases, capacity reservations, and disk pressure.
- Recovery: SIGKILL during an active preview followed by SQLite recovery, temporary-output cleanup, and preservation of published covers.
- Lifecycle: default launcher operation on port 18483, occupied-port reporting without killing the existing listener, and shutdown of owned media processes.

## Observed performance

Measurements used server loopback and headless software decoding without GPU acceleration. They do not guarantee LAN or client-device performance.

| Scenario | Observation |
| --- | --- |
| Cached cover, temporary sample | 3–4ms complete HTTP fetch |
| Seek within browser-buffered sample | 0.33–0.35s recovery |
| Cached first frame, real 58-minute 1266×720 AV1 10-bit video | About 0.4s |
| Server-cached positions not buffered in the browser, same video | 0.63–1.42s recovery; some positions exceeded the 1s target |
| Initial MKV Cues timeline, same video | About 0.1s; no full packet scan |
| Cold first, middle, and final windows, same video | About 0.13s, 4.52s, and 0.16s per window; about 41.5MiB total source reads including neighboring prefetch, versus a 529MiB source file |

Real source GOPs were typically about 20 seconds. Lossless playback still requires decoding from the preceding keyframe to the requested position. Cold HDD waits are reported separately from cache hits.

## Device coverage still required

Actual Windows, iOS, iPadOS, and Android Chrome devices have not been tested. Native HLS, hardware AV1 decoding, touch seeking, fullscreen, and perceived audio/video synchronization need device checks. Mobile layout emulation does not establish device compatibility.
