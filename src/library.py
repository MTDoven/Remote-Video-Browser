"""SQLite catalog. Refresh reads directory metadata, never video payloads."""
from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path
import os
import json
import math
import sqlite3
import threading
import time

from .config import PAGE_SIZE, VIDEO_EXTENSIONS
from .errors import BrowserError, Changed


def opaque(path):
    return sha256(path.encode()).hexdigest()[:24]


def version(path, stat):
    return opaque(f"{path}\0{stat.st_size}\0{stat.st_mtime_ns}\0{stat.st_ctime_ns}\0{stat.st_dev}\0{stat.st_ino}")


class Library:
    def __init__(self, settings):
        self.settings = settings
        self.lock = threading.RLock()
        self.db = sqlite3.connect(settings.cache / "library.sqlite3", check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.create_function('recommend_rank', 2, lambda seed, id: int(sha256(f'{seed}\0{id}'.encode()).hexdigest()[:15], 16), deterministic=True)
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS directories (
                id TEXT PRIMARY KEY, path TEXT UNIQUE, parent TEXT, name TEXT,
                search TEXT, item INTEGER, video_count INTEGER);
            CREATE TABLE IF NOT EXISTS videos (
                id TEXT PRIMARY KEY, path TEXT UNIQUE, parent TEXT, name TEXT,
                size INTEGER, version TEXT);
            CREATE INDEX IF NOT EXISTS videos_parent ON videos(parent);
            CREATE INDEX IF NOT EXISTS dirs_parent ON directories(parent);
            CREATE TABLE IF NOT EXISTS cache_entries (
                key TEXT PRIMARY KEY, bytes INTEGER, accessed REAL);
        """)
        self.status = {"state": "idle", "directories": 0, "videos": 0, "error": "", "updated": None}
        self.refresh_thread = None
        self.on_change = lambda ids: None
        self.closing = threading.Event()

    @contextmanager
    def transaction(self):
        with self.lock:
            with self.db:
                yield self.db

    def rows(self, sql, params=()):
        with self.lock:
            return [dict(r) for r in self.db.execute(sql, params)]

    def refresh(self):
        with self.lock:
            if self.refresh_thread and self.refresh_thread.is_alive():
                return dict(self.status)
            self.status.update(state="scanning", directories=0, videos=0, error="")
            self.refresh_thread = threading.Thread(target=self._scan, name="catalog-refresh", daemon=True)
            self.refresh_thread.start()
            return dict(self.status)

    def _scan(self):
        try:
            directories, videos = [], []
            pending = [self.settings.source]
            while pending:
                if self.closing.is_set():
                    return
                directory = pending.pop()
                relative = directory.relative_to(self.settings.source).as_posix()
                relative = "" if relative == "." else relative
                parent_path = Path(relative).parent.as_posix()
                parent = opaque("" if parent_path == "." else parent_path) if relative else None
                with os.scandir(directory) as scan:
                    entries = sorted(scan, key=lambda e: e.name.casefold())
                children = [Path(e.path) for e in entries if e.is_dir(follow_symlinks=False)]
                pending.extend(reversed(children))
                count = 0
                for entry in entries:
                    if not entry.is_file(follow_symlinks=False) or Path(entry.name).suffix.lower() not in VIDEO_EXTENSIONS:
                        continue
                    stat = entry.stat(follow_symlinks=False)
                    path = Path(entry.path).relative_to(self.settings.source).as_posix()
                    videos.append((opaque(path), path, opaque(relative), entry.name, stat.st_size, version(path, stat)))
                    count += 1
                directories.append((opaque(relative), relative, parent, directory.name if relative else "Library",
                                    directory.name.casefold(), int(not children and count > 0), count))
                with self.lock:
                    self.status.update(directories=len(directories), videos=len(videos))
            old = {r["id"]: r["version"] for r in self.rows("SELECT id, version FROM videos")}
            new = {r[0]: r[5] for r in videos}
            with self.transaction() as db:
                db.execute("DELETE FROM directories")
                db.execute("DELETE FROM videos")
                db.executemany("INSERT INTO directories VALUES (?,?,?,?,?,?,?)", directories)
                db.executemany("INSERT INTO videos VALUES (?,?,?,?,?,?)", videos)
            self.on_change([key for key, value in old.items() if new.get(key) != value])
            with self.lock:
                self.status.update(state="ready", updated=time.time())
        except Exception as error:
            with self.lock:
                self.status.update(state="error", error=f"The index could not be refreshed: {error}")

    def video(self, id, expected=None):
        rows = self.rows("SELECT * FROM videos WHERE id=?", (id,))
        if not rows:
            raise Changed()
        video = rows[0]
        path = self.settings.source / video["path"]
        try:
            resolved = path.resolve(strict=True)
            if not resolved.is_relative_to(self.settings.source) or path.is_symlink():
                raise Changed()
            actual = version(video["path"], resolved.stat())
        except OSError:
            self.on_change([id])
            raise Changed() from None
        if actual != video["version"]:
            self.on_change([id])
            raise Changed()
        if expected and actual != expected:
            raise Changed()
        return video, resolved

    def items(self, query="", page=1):
        page = max(1, page)
        params = [query.casefold()]
        total = self.rows("SELECT count(*) AS n FROM directories WHERE item=1 AND instr(search,?) > 0", params)[0]["n"]
        pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
        page = min(page, pages)
        rows = self.rows("SELECT * FROM directories WHERE item=1 AND instr(search,?) > 0 ORDER BY name COLLATE NOCASE,path LIMIT ? OFFSET ?",
                         (*params, PAGE_SIZE, (page - 1) * PAGE_SIZE))
        self._covers(rows)
        return {"entries": rows, "page": page, "pages": pages, "total": total}

    def _covers(self, rows):
        for row in rows:
            first = self.rows("SELECT id FROM videos WHERE parent=? ORDER BY name COLLATE NOCASE,path LIMIT 1", (row["id"],))
            row["cover"] = first[0]["id"] if first else None

    def recommendations(self, seed, cursor=None):
        base = """WITH stats AS (
            SELECT video_id,version,count(*) AS opens,sum(watched) AS watched FROM viewing_history GROUP BY video_id,version
        ), candidates AS (
            SELECT v.*,coalesce(s.opens,0) AS opens,coalesce(s.watched,0) AS watched,
                CASE WHEN coalesce(s.watched,0)<30 THEN 0 ELSE 1 END AS light,
                recommend_rank(?,v.id) AS rank FROM videos v LEFT JOIN stats s ON s.video_id=v.id AND s.version=v.version
        ) """
        unseen = bool(self.rows(base + 'SELECT count(*) AS n FROM candidates WHERE opens=0', (seed,))[0]['n'])
        params = [seed]
        where = 'opens=0' if unseen else '1=1'
        if cursor:
            try:
                values = json.loads(cursor)
                if not isinstance(values, list) or len(values) != 6 or type(values[0]) is not bool or any(type(x) not in (int,float) or not math.isfinite(x) for x in values[1:5]) or not isinstance(values[5], str):
                    raise ValueError()
                unseen = values[0]
                where = 'opens=0' if unseen else '1=1'
                where += ' AND (light,opens,watched,rank,id)>(?,?,?,?,?)'
                params.extend(values[1:])
            except (ValueError, TypeError):
                raise BrowserError('Invalid recommendation cursor.') from None
        rows = self.rows(base + f'SELECT * FROM candidates WHERE {where} ORDER BY light,opens,watched,rank,id LIMIT ?', (*params, PAGE_SIZE + 1))
        more = len(rows) > PAGE_SIZE
        rows = rows[:PAGE_SIZE]
        next_cursor = json.dumps([unseen, *[rows[-1][field] for field in ('light','opens','watched','rank','id')]]) if more else None
        return {'entries': rows, 'next': next_cursor, 'unseen': unseen}

    def directory(self, id):
        rows = self.rows("SELECT * FROM directories WHERE id=?", (id,))
        if not rows:
            raise BrowserError("Directory not found. Refresh the library.", 404)
        directory = rows[0]
        parts = Path(directory["path"]).parts if directory["path"] else ()
        crumbs = [{"id": opaque(""), "name": "Library"}]
        for i in range(len(parts)):
            crumbs.append({"id": opaque(Path(*parts[:i+1]).as_posix()), "name": parts[i]})
        return {**directory, "breadcrumbs": crumbs}

    def contents(self, id, page):
        counts = {table: self.rows(f"SELECT count(*) AS n FROM {table} WHERE parent=?", (id,))[0]["n"]
                  for table in ("directories", "videos")}
        pages = max(1, (sum(counts.values()) + PAGE_SIZE - 1) // PAGE_SIZE)
        page = min(max(1, page), pages)
        offset, remaining, result = (page - 1) * PAGE_SIZE, PAGE_SIZE, {}
        for table, count in counts.items():
            entries = self.rows(f"SELECT * FROM {table} WHERE parent=? ORDER BY name COLLATE NOCASE,path LIMIT ? OFFSET ?",
                                (id, remaining, offset)) if offset < count else []
            if table == "directories":
                self._covers(entries)
            result[table] = {"entries": entries, "total": count, "page": page, "pages": pages}
            remaining -= len(entries)
            offset = max(0, offset - count)
        return result

    def close(self):
        self.closing.set()
        if self.refresh_thread:
            self.refresh_thread.join(timeout=5)
        with self.lock:
            self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self.db.close()
