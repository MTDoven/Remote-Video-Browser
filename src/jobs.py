"""Two shared media workers with cancellation and foreground priority."""
from concurrent.futures import Future
from dataclasses import dataclass, field
import heapq
import itertools
import threading
import time

from .errors import BrowserError


@dataclass
class Job:
    key: str
    function: object
    priority: int
    future: Future = field(default_factory=Future)
    cancel: threading.Event = field(default_factory=threading.Event)
    wanted: float = field(default_factory=time.monotonic)
    owners: set = field(default_factory=set)


class Jobs:
    def __init__(self, source):
        self.source = source
        self.condition = threading.Condition()
        self.queue, self.jobs = [], {}
        self.serial = itertools.count()
        self.closed = False
        self.background = False
        self.last_request = time.monotonic()
        self.workers = [threading.Thread(target=self._run, name=f"media-worker-{i}", daemon=True) for i in range(2)]
        for worker in self.workers:
            worker.start()

    def submit(self, key, function, priority=0, owner=None):
        with self.condition:
            if priority < 30:
                self.last_request = time.monotonic()
                for pending in self.jobs.values():
                    if pending.priority >= 30 and pending.key != key:
                        pending.cancel.set()
            if priority < 10:
                self.source.demand_until = time.monotonic() + 2
            job = self.jobs.get(key)
            if job and not job.cancel.is_set() and not job.future.done():
                job.wanted = time.monotonic()
                if owner:
                    job.owners.add(owner)
                if priority < job.priority:
                    job.priority = priority
                    self.queue = [(priority if queued is job else p, serial, queued) for p, serial, queued in self.queue]
                    heapq.heapify(self.queue)
                    self.condition.notify_all()
                return job
            if self.closed:
                raise BrowserError("The service has stopped.", 503)
            job = Job(key, function, priority)
            if owner:
                job.owners.add(owner)
            self.jobs[key] = job
            heapq.heappush(self.queue, (priority, next(self.serial), job))
            self.condition.notify()
            return job

    def _run(self):
        while True:
            with self.condition:
                while not self.queue and not self.closed:
                    self.condition.wait()
                if self.closed:
                    return
                priority, serial, job = heapq.heappop(self.queue)
                if priority >= 10 and (self.background or time.monotonic() < self.source.demand_until):
                    heapq.heappush(self.queue, (priority, serial, job))
                    self.condition.wait(timeout=0.2)
                    continue
                if priority >= 10:
                    self.background = True
            try:
                if job.cancel.is_set() or (10 <= priority < 30 and time.monotonic() - job.wanted > 15):
                    if not job.future.done():
                        job.future.set_exception(BrowserError("The task was cancelled.", 410))
                    job.cancel.set()
                else:
                    job.future.set_running_or_notify_cancel()
                    job.future.set_result(job.function(job.cancel))
            except BaseException as error:
                if not job.future.done():
                    job.future.set_exception(error)
            finally:
                with self.condition:
                    if priority >= 10:
                        self.background = False
                    if self.jobs.get(job.key) is job and job.future.done():
                        self.jobs.pop(job.key, None)
                    self.condition.notify_all()

    def cancel_matching(self, text, prefetch_only=False):
        with self.condition:
            for job in list(self.jobs.values()):
                if text in job.key and (not prefetch_only or (job.priority > 0 and not job.future.running())):
                    job.cancel.set()

    def close(self):
        with self.condition:
            self.closed = True
            for job in self.jobs.values():
                job.cancel.set()
            for _, _, job in self.queue:
                if not job.future.done():
                    job.future.set_exception(BrowserError("The service has stopped.", 503))
            self.condition.notify_all()
        for worker in self.workers:
            worker.join(timeout=10)
