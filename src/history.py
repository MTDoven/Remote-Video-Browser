"""Persistent video opens and confirmed playback intervals in the cache database."""
import math
import secrets
import time

from .errors import BrowserError


class History:
    def __init__(self, library):
        self.library = library
        with library.transaction() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS viewing_history (
                    id TEXT PRIMARY KEY, video_id TEXT, version TEXT, path TEXT, name TEXT,
                    opened REAL, closed REAL, duration REAL, watched REAL NOT NULL DEFAULT 0);
                CREATE INDEX IF NOT EXISTS history_video ON viewing_history(video_id,version);
                CREATE INDEX IF NOT EXISTS history_opened ON viewing_history(opened);
                CREATE TABLE IF NOT EXISTS viewing_ranges (
                    open_id TEXT, sequence INTEGER, part INTEGER, start REAL, end REAL,
                    watched REAL, recorded REAL, PRIMARY KEY(open_id,sequence,part));
            """)

    def open(self, video):
        id = secrets.token_hex(16)
        with self.library.transaction() as db:
            db.execute("INSERT INTO viewing_history (id,video_id,version,path,name,opened) VALUES (?,?,?,?,?,?)",
                       (id, video['id'], video['version'], video['path'], video['name'], time.time()))
        return id

    def bind(self, id, video, duration):
        if not isinstance(id, str) or len(id) != 32:
            raise BrowserError('Invalid viewing record ID.')
        with self.library.transaction() as db:
            row = db.execute("SELECT * FROM viewing_history WHERE id=?", (id,)).fetchone()
            if not row or row['video_id'] != video['id'] or row['version'] != video['version']:
                raise BrowserError('The viewing record does not match this video.', 409)
            db.execute("UPDATE viewing_history SET duration=? WHERE id=?", (duration, id))

    def record(self, id, payload):
        sequence, ranges = payload.get('sequence'), payload.get('ranges', [])
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
            raise BrowserError('Invalid viewing sequence.')
        if not isinstance(ranges, list) or len(ranges) > 128:
            raise BrowserError('Invalid viewing intervals.')
        now = time.time()
        with self.library.transaction() as db:
            row = db.execute("SELECT * FROM viewing_history WHERE id=?", (id,)).fetchone()
            if not row:
                raise BrowserError('Viewing record not found.', 404)
            allowance = max(0, now - row['opened'] - row['watched'])
            for part, interval in enumerate(ranges):
                if not isinstance(interval, list) or len(interval) != 3 or any(isinstance(x, bool) or not isinstance(x, (float, int)) or not math.isfinite(x) for x in interval):
                    raise BrowserError('Invalid viewing interval.')
                start, end, watched = interval
                if start < 0 or end <= start or watched <= 0 or watched > 30:
                    raise BrowserError('Invalid viewing interval.')
                end = min(end, row['duration'] or end)
                if end <= start:
                    continue
                watched = min(watched, allowance)
                inserted = db.execute("INSERT OR IGNORE INTO viewing_ranges VALUES (?,?,?,?,?,?,?)",
                                      (id, sequence, part, start, end, watched, now)).rowcount
                if inserted:
                    allowance -= watched
                    db.execute("UPDATE viewing_history SET watched=watched+? WHERE id=?", (watched, id))
            if payload.get('closed') is True:
                db.execute("UPDATE viewing_history SET closed=max(coalesce(closed,0),?) WHERE id=?", (now, id))

    def close(self, id):
        with self.library.transaction() as db:
            db.execute("UPDATE viewing_history SET closed=max(coalesce(closed,0),?) WHERE id=?", (time.time(), id))

    def list(self, page):
        entries = self.library.rows("SELECT * FROM viewing_history ORDER BY opened DESC LIMIT 48 OFFSET ?", ((page - 1) * 48,))
        for row in entries:
            row['ranges'] = self.library.rows("SELECT start,end,watched,recorded FROM viewing_ranges WHERE open_id=? ORDER BY recorded,sequence,part", (row['id'],))
            for interval in row['ranges']:
                interval.update(start_minute=interval['start']/60, end_minute=interval['end']/60)
        return {'entries': entries, 'page': page}
