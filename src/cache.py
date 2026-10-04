"""Bounded persistent cache with reservations, LRU eviction and leases."""
from collections import Counter
from contextlib import contextmanager
import shutil
import threading
import time
import uuid

from .errors import CapacityError


def allocated(path):
    if path.is_file():
        return path.stat().st_blocks * 512
    return sum(p.stat().st_blocks * 512 for p in path.rglob("*") if p.is_file() and not p.is_symlink())


class Reservation:
    def __init__(self, cache, size):
        self.cache, self.size = cache, 0
        self.resize(size)

    def resize(self, size):
        with self.cache.lock:
            extra = max(0, size - self.size)
            self.cache._ensure(extra)
            self.cache.reserved += size - self.size
            self.size = size

    def close(self):
        with self.cache.lock:
            self.cache.reserved -= self.size
            self.size = 0


class Cache:
    def __init__(self, settings, library):
        self.settings, self.library = settings, library
        self.root = settings.cache
        self.lock = threading.RLock()
        self.pins = Counter()
        self.reserved = 0
        self.evictions = 0
        (self.root / "objects").mkdir(exist_ok=True)
        work = self.root / ".work"
        if work.exists():
            shutil.rmtree(work)
        work.mkdir()
        self._recover()
        with self.lock:
            self._ensure(0)

    def path(self, key):
        path = self.root / "objects" / key
        if not path.resolve().is_relative_to(self.root / "objects"):
            raise ValueError("Invalid cache key")
        return path

    def _recover(self):
        records = self.library.rows("SELECT * FROM cache_entries")
        registered = set()
        for record in records:
            path = self.path(record["key"])
            if path.exists():
                registered.add(path)
                with self.library.transaction() as db:
                    db.execute("UPDATE cache_entries SET bytes=? WHERE key=?", (allocated(path), record["key"]))
            else:
                with self.library.transaction() as db:
                    db.execute("DELETE FROM cache_entries WHERE key=?", (record["key"],))
        # Only unpublished files under our object store are removed.
        for path in (self.root / "objects").rglob("*"):
            if path.is_file() and path not in registered and not any(parent in registered for parent in path.parents):
                path.unlink()

    def _usage(self):
        objects = self.library.rows("SELECT coalesce(sum(bytes),0) AS n FROM cache_entries")[0]["n"]
        database = sum(p.stat().st_blocks * 512 for p in self.root.glob("library.sqlite3*") if p.is_file())
        return objects + database

    def _ensure(self, extra):
        usage = self._usage()
        free = shutil.disk_usage(self.root).free
        if usage + self.reserved + extra <= self.settings.max_bytes and free - self.reserved - extra >= self.settings.min_free:
            return
        records = self.library.rows("SELECT * FROM cache_entries ORDER BY accessed")
        for record in records:
            if usage + self.reserved + extra <= self.settings.max_bytes and free - self.reserved - extra >= self.settings.min_free:
                return
            if self.pins[record["key"]]:
                continue
            self._remove(record)
            usage -= record["bytes"]
            free = shutil.disk_usage(self.root).free
        if usage + self.reserved + extra > self.settings.max_bytes or free - self.reserved - extra < self.settings.min_free:
            raise CapacityError()

    def _remove(self, record):
        path = self.path(record["key"])
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)
        with self.library.transaction() as db:
            db.execute("DELETE FROM cache_entries WHERE key=?", (record["key"],))
        self.evictions += 1

    @contextmanager
    def pin(self, key):
        self.acquire(key)
        try:
            yield
        finally:
            self.release(key)

    def acquire(self, key):
        with self.lock:
            self.pins[key] += 1

    def release(self, key):
        with self.lock:
            if self.pins[key] <= 1:
                self.pins.pop(key, None)
            else:
                self.pins[key] -= 1

    def get(self, key):
        with self.lock:
            rows = self.library.rows("SELECT key FROM cache_entries WHERE key=?", (key,))
            if not rows:
                return None
            path = self.path(key)
            if not path.exists():
                with self.library.transaction() as db:
                    db.execute("DELETE FROM cache_entries WHERE key=?", (key,))
                return None
            with self.library.transaction() as db:
                db.execute("UPDATE cache_entries SET accessed=? WHERE key=?", (time.time(), key))
            return path

    @contextmanager
    def workspace(self, size):
        reservation = Reservation(self, size)
        path = self.root / ".work" / uuid.uuid4().hex
        try:
            path.mkdir()
            yield path, reservation
        finally:
            shutil.rmtree(path, ignore_errors=True)
            reservation.close()

    def publish(self, key, temporary, reservation):
        size = allocated(temporary)
        reservation.resize(max(size, reservation.size))
        with self.lock:
            target = self.path(key)
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                if target.is_dir():
                    shutil.rmtree(target)
                else:
                    target.unlink()
            temporary.replace(target)
            with self.library.transaction() as db:
                db.execute("INSERT OR REPLACE INTO cache_entries (key,bytes,accessed) VALUES (?,?,?)",
                           (key, size, time.time()))
            reservation.close()
            return target

    def put(self, key, data):
        with self.pin(key), self.workspace(len(data) + 4096) as (work, reservation):
            path = work / "result"
            path.write_bytes(data)
            return self.publish(key, path, reservation)

    def invalidate(self, video_ids):
        with self.lock:
            for id in video_ids:
                for record in self.library.rows("SELECT * FROM cache_entries WHERE instr(key,?) > 0", (f"/{id}/",)):
                    if not self.pins[record["key"]]:
                        self._remove(record)

    def stats(self):
        with self.lock:
            pressure = False
            try:
                self._ensure(0)
            except CapacityError:
                pressure = True
            usage = self._usage()
            return {"bytes": usage, "reserved": self.reserved, "limit": self.settings.max_bytes,
                    "free": shutil.disk_usage(self.root).free, "evictions": self.evictions, "pressure": pressure}
