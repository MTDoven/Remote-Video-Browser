"""Short-lived playback leases over shared, persistent media segments."""
from dataclasses import dataclass, field
import math
import secrets
import threading
import time

from .errors import BrowserError


@dataclass
class Session:
    id: str
    video: dict
    info: dict
    timeline: dict
    audio: str
    accessed: float = field(default_factory=time.monotonic)
    index: int = 0
    sequence: int = -1
    pins: set = field(default_factory=set)
    open_id: str = ''


class Sessions:
    def __init__(self, library, cache, media, jobs, history):
        self.library, self.cache, self.media, self.jobs = library, cache, media, jobs
        self.history = history
        self.lock = threading.RLock()
        self.entries = {}
        self.stop = threading.Event()
        self.cleaner = threading.Thread(target=self._cleanup, name="playback-leases", daemon=True)
        self.cleaner.start()

    def create(self, video, info, timeline, opus=False, open_id=None):
        audio = "copy" if not info["audio"] or info["audio"] == "aac" or (info["audio"] == "opus" and opus) else "aac"
        session = Session(secrets.token_hex(16), video, info, timeline, audio)
        session.open_id = open_id or self.history.open(video)
        self.history.bind(session.open_id, video, info['duration'])
        with self.lock:
            self.entries[session.id] = session
            self.touch(session.id, 0)
        return session

    def get(self, id):
        with self.lock:
            session = self.entries.get(id)
            if not session:
                raise BrowserError("The playback session expired. Open the video again.", 410)
            session.accessed = time.monotonic()
        self.library.video(session.video["id"], session.video["version"])
        return session

    def touch(self, id, index=None, sequence=None):
        with self.lock:
            session = self.get(id)
            if index is None:
                return session
            if sequence is not None and sequence < session.sequence:
                return session
            count = len(session.timeline["boundaries"]) - 1
            if not 0 <= index < count:
                raise BrowserError("Invalid segment index.", 404)
            if sequence is not None:
                session.sequence = sequence
            session.index = index
            wanted = {self.media.segment_key(session.video, session.audio, i)
                      for i in range(max(0, index - 1), min(count, index + 3))}
            for key in wanted - session.pins:
                self.cache.acquire(key)
            for key in session.pins - wanted:
                self.cache.release(key)
                if not any(key in other.pins for other in self.entries.values() if other is not session):
                    # Requested init/fragment jobs must survive a seek, even before a worker starts them.
                    self.jobs.cancel_matching(key, prefetch_only=True)
            session.pins = wanted
            return session

    def remove(self, id):
        with self.lock:
            session = self.entries.pop(id, None)
            if session:
                self.history.close(session.open_id)
                for key in session.pins:
                    self.cache.release(key)
                    if not any(key in other.pins for other in self.entries.values()):
                        self.jobs.cancel_matching(key)

    def invalidate(self, ids):
        with self.lock:
            for id, session in list(self.entries.items()):
                if session.video["id"] in ids:
                    self.remove(id)

    @staticmethod
    def manifest(session):
        boundaries = session.timeline["boundaries"]
        target = math.ceil(max(b - a for a, b in zip(boundaries, boundaries[1:])))
        prefix = f"/api/sessions/{session.id}"
        lines = ["#EXTM3U", "#EXT-X-VERSION:7", f"#EXT-X-TARGETDURATION:{target}",
                 "#EXT-X-MEDIA-SEQUENCE:0", "#EXT-X-PLAYLIST-TYPE:VOD", "#EXT-X-INDEPENDENT-SEGMENTS"]
        for i, (start, end) in enumerate(zip(boundaries, boundaries[1:])):
            # Each remux window has its own init and timestamp origin. HLS explicitly
            # maps these onto the complete VOD timeline; no fake tfdt offsets are used.
            if i:
                lines.append("#EXT-X-DISCONTINUITY")
            lines += [f'#EXT-X-MAP:URI="{prefix}/segments/{i}/init.mp4"', f"#EXTINF:{end-start:.6f},",
                      f"{prefix}/segments/{i}/segment.m4s"]
        return "\n".join(lines + ["#EXT-X-ENDLIST", ""])

    def _cleanup(self):
        while not self.stop.wait(10):
            with self.lock:
                expired = [id for id, session in self.entries.items() if time.monotonic() - session.accessed > 60]
            for id in expired:
                self.remove(id)

    def close(self):
        self.stop.set()
        self.cleaner.join(timeout=2)
        for id in list(self.entries):
            self.remove(id)
