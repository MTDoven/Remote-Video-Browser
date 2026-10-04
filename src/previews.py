"""Generate missing contact sheets one at a time while the service is idle."""
import logging
import threading
import time

from .errors import BrowserError


class PreviewCache:
    def __init__(self, library, cache, media, jobs, sessions):
        self.library, self.cache, self.media, self.jobs, self.sessions = library, cache, media, jobs, sessions
        with library.transaction() as db:
            db.execute('CREATE TABLE IF NOT EXISTS preview_failures (video_id TEXT, version TEXT, error TEXT, retry REAL, PRIMARY KEY(video_id,version))')
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._run, name='idle-preview-cache', daemon=True)
        self.thread.start()

    def _idle(self):
        with self.sessions.lock:
            playing = bool(self.sessions.entries)
        with self.jobs.condition:
            busy = time.monotonic() - self.jobs.last_request < 10
        with self.library.lock:
            ready = self.library.status['state'] == 'ready'
        return ready and bool(self.media.base_url) and not playing and not busy

    def _run(self):
        cursor, previous, updated, job, active_video, retry = '', '', None, None, None, 0
        while not self.stop.wait(2):
            try:
                idle = self._idle()
                if job:
                    with self.jobs.condition:
                        if not idle and job.priority >= 30:
                            job.cancel.set()
                    if not job.future.done():
                        continue
                    if job.cancel.is_set():
                        cursor = previous
                    else:
                        try:
                            job.future.result()
                            with self.library.transaction() as db:
                                db.execute('DELETE FROM preview_failures WHERE video_id=? AND version=?', (active_video['id'], active_video['version']))
                        except BrowserError as error:
                            if error.status not in (409, 410, 503, 507):
                                with self.library.transaction() as db:
                                    db.execute('INSERT OR REPLACE INTO preview_failures VALUES (?,?,?,?)',
                                               (active_video['id'], active_video['version'], str(error), time.time() + 86400))
                                logging.getLogger(__name__).warning('Idle preview skipped for %s; retry in 24 hours: %s', active_video['path'], error)
                    job = None
                if not idle or time.monotonic() < retry:
                    continue
                stats = self.cache.stats()
                if stats['pressure'] or stats['bytes'] + stats['reserved'] >= stats['limit'] * 0.8 or stats['free'] < self.cache.settings.min_free + 256 * 1024**2:
                    continue
                with self.library.lock:
                    current = self.library.status['updated']
                if current != updated:
                    updated, cursor = current, ''
                rows = self.library.rows('SELECT * FROM videos WHERE path>? ORDER BY path LIMIT 48', (cursor,))
                if not rows:
                    cursor, retry = '', time.monotonic() + 300
                    continue
                for video in rows:
                    if self.stop.is_set() or not self._idle():
                        break
                    previous, cursor = cursor, video['path']
                    if self.library.rows('SELECT 1 FROM preview_failures WHERE video_id=? AND version=? AND retry>?', (video['id'], video['version'], time.time())):
                        continue
                    key = self.media.image_key(video, 'preview')
                    with self.cache.pin(key):
                        if self.cache.get(key):
                            continue
                    self.library.video(video['id'], video['version'])
                    job = self.media.image(video, 'preview', priority=30)
                    if job:
                        active_video = video
                        break
            except Exception as error:
                logging.getLogger(__name__).warning('Idle preview caching paused: %s', error)
                job, retry = None, time.monotonic() + 60

    def close(self):
        self.stop.set()
        self.thread.join(timeout=5)
