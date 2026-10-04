"""One prioritized HDD reader; all media tools read through cached HTTP ranges."""
from concurrent.futures import Future
import heapq
import itertools
import os
import threading
import time

from .config import BLOCK_SIZE
from .errors import BrowserError, Changed, CapacityError
from .library import version


class Source:
    def __init__(self, library, cache):
        self.library, self.cache = library, cache
        self.condition = threading.Condition()
        self.queue, self.inflight = [], {}
        self.serial = itertools.count()
        self.closed = False
        self.demand_until = 0
        self.bytes_read = self.reads = self.hits = 0
        self.capacity_failures = {}
        self.worker = threading.Thread(target=self._run, name="hdd-reader", daemon=True)
        self.worker.start()

    def block(self, video, block, priority=0, wait=True):
        key = f"blocks/{video['id']}/{video['version']}/{block}"
        self.library.video(video["id"], video["version"])
        with self.cache.pin(key):
            path = self.cache.get(key)
            if path:
                self.hits += 1
                return path.read_bytes() if wait else None
        with self.condition:
            if self.closed:
                raise BrowserError("The service has stopped.", 503)
            if priority == 0:
                self.demand_until = time.monotonic() + 2
            future = self.inflight.get(key)
            if future is None:
                future = Future()
                self.inflight[key] = future
                heapq.heappush(self.queue, (priority, next(self.serial), video, block, key, future))
                self.condition.notify()
            elif priority == 0:
                # A shared preview miss must immediately become a playback request.
                self.queue = [(min(p, priority) if queued_key == key else p, serial, item, number, queued_key, queued_future)
                              for p, serial, item, number, queued_key, queued_future in self.queue]
                heapq.heapify(self.queue)
                self.condition.notify()
        return future.result() if wait else None

    def _run(self):
        while True:
            with self.condition:
                while not self.queue and not self.closed:
                    self.condition.wait()
                if self.closed:
                    for *_, future in self.queue:
                        if not future.done():
                            future.set_exception(BrowserError("The service has stopped.", 503))
                    return
                priority, _, video, block, key, future = heapq.heappop(self.queue)
                if priority >= 10 and time.monotonic() < self.demand_until:
                    heapq.heappush(self.queue, (priority, next(self.serial), video, block, key, future))
                    self.condition.wait(timeout=0.1)
                    continue
            try:
                _, path = self.library.video(video["id"], video["version"])
                with self.cache.pin(key):
                    cached = self.cache.get(key)
                    if cached:
                        future.set_result(cached.read_bytes())
                        self.hits += 1
                        continue
                # O_RDONLY is the only mode ever used for source video data.
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
                try:
                    if version(video["path"], os.fstat(fd)) != video["version"]:
                        raise Changed()
                    data = os.pread(fd, BLOCK_SIZE, block * BLOCK_SIZE)
                    if version(video["path"], os.fstat(fd)) != video["version"]:
                        raise Changed()
                finally:
                    os.close(fd)
                self.bytes_read += len(data)
                self.reads += 1
                self.library.video(video["id"], video["version"])
                self.cache.put(key, data)
                future.set_result(data)
            except Exception as error:
                if isinstance(error, CapacityError):
                    with self.condition:
                        self.capacity_failures[(video['id'], video['version'])] = time.monotonic()
                        if len(self.capacity_failures) > 256:
                            self.capacity_failures.pop(next(iter(self.capacity_failures)))
                future.set_exception(error)
            finally:
                with self.condition:
                    self.inflight.pop(key, None)

    def stream(self, video, start, end, priority=0, disconnected=lambda: False):
        while start <= end:
            if disconnected():
                return
            block, within = divmod(start, BLOCK_SIZE)
            data = self.block(video, block, priority)
            if priority == 0:
                for next_block in (block + 1, block + 2):
                    if next_block * BLOCK_SIZE < video["size"]:
                        self.block(video, next_block, 30, wait=False)
            while within < len(data) and start <= end:
                if disconnected():
                    return
                length = min(256*1024, len(data) - within, end - start + 1)
                self.library.video(video["id"], video["version"])
                yield data[within:within + length]
                start += length
                within += length

    def close(self):
        with self.condition:
            self.closed = True
            self.condition.notify_all()
        self.worker.join(timeout=10)
