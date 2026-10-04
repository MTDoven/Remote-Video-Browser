"""Probe and remux AV1, never encode video. Tasks publish complete cache objects."""
from pathlib import Path
import json
import math
import secrets
import subprocess
import threading
import time

from .cache import allocated
from .errors import BrowserError, CapacityError
from .matroska import Matroska, split_mp4


class Media:
    def __init__(self, settings, library, cache, source, jobs):
        self.settings, self.library, self.cache, self.source, self.jobs = settings, library, cache, source, jobs
        self.token = secrets.token_hex(32)
        self.base_url = None
        self.processes = set()
        self.lock = threading.Lock()

    def url(self, video, priority):
        if not self.base_url:
            raise BrowserError("The service is starting.", 503)
        return f"{self.base_url}/internal/source/{video['id']}/{video['version']}?token={self.token}&priority={priority}"

    @staticmethod
    def key(kind, video, suffix=""):
        return f"{kind}/{video['id']}/{video['version']}" + (f"/{suffix}" if suffix else "")

    def run(self, command, work, reservation, cancel, video, timeout=90):
        self.library.video(video["id"], video["version"])
        stdout, stderr = work / "stdout.tmp", work / "stderr.tmp"
        with stdout.open("wb") as out, stderr.open("wb") as err:
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=out, stderr=err)
            with self.lock:
                self.processes.add(process)
            started, checked = time.monotonic(), 0
            try:
                while process.poll() is None:
                    if cancel.is_set():
                        raise BrowserError("The task was cancelled.", 410)
                    if time.monotonic() - started > timeout:
                        raise BrowserError("Media processing timed out. Try again.", 504)
                    if time.monotonic() - checked > 0.5:
                        self.library.video(video["id"], video["version"])
                        reservation.resize(max(reservation.size, allocated(work) + 1024**2))
                        checked = time.monotonic()
                    time.sleep(0.05)
                if process.returncode:
                    with self.source.condition:
                        if self.source.capacity_failures.get((video['id'], video['version']), 0) >= started:
                            raise CapacityError()
                    message = stderr.read_bytes()[-1600:].decode(errors="replace").replace(self.token, "[private]")
                    raise BrowserError(f"Media processing failed: {message}", 422)
                self.library.video(video["id"], video["version"])
                return stdout.read_bytes()
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
                with self.lock:
                    self.processes.discard(process)
                stdout.unlink(missing_ok=True)
                stderr.unlink(missing_ok=True)

    def cached_json(self, key):
        with self.cache.pin(key):
            path = self.cache.get(key)
            return json.loads(path.read_text()) if path else None

    def metadata(self, video):
        key = self.key("metadata", video)
        cached = self.cached_json(key)
        if cached:
            return cached, None
        job = self.jobs.submit(key, lambda cancel: self._probe(video, cancel))
        return None, job

    def _probe(self, video, cancel, priority=0):
        with self.cache.workspace(2 * 1024**2) as (work, reservation):
            command = [str(self.settings.ffprobe), "-v", "error", "-show_format", "-show_streams", "-of", "json", self.url(video, priority)]
            payload = json.loads(self.run(command, work, reservation, cancel, video))
            stream = next((s for s in payload["streams"] if s["codec_type"] == "video"), None)
            if not stream:
                raise BrowserError("The file has no video track.", 422)
            audio = next((s for s in payload["streams"] if s["codec_type"] == "audio"), None)
            rate = stream.get("avg_frame_rate", "0/1").split("/")
            fps = float(rate[0]) / max(float(rate[1]), 1) if len(rate) == 2 else float(rate[0])
            pixel = stream.get("pix_fmt", "")
            depth = int(stream.get("bits_per_raw_sample") or (12 if "12" in pixel else 10 if "10" in pixel else 8))
            codec = stream["codec_name"]
            level = int(stream.get("level", 8))
            if codec == "av1":
                profile = {"Main": 0, "High": 1, "Professional": 2}.get(stream.get("profile"), 0)
                mime = f"av01.{profile}.{max(0, min(level if level >= 0 else 8, 23)):02d}M.{depth:02d}"
            elif codec == "h264":
                mime = "avc1.640028"
            elif codec == "hevc":
                mime = "hvc1.1.6.L120.B0"
            else:
                raise BrowserError(f"Segmented playback is unsupported for codec: {codec}", 415)
            duration = float(payload.get("format", {}).get("duration") or stream.get("duration") or 0)
            if not math.isfinite(duration) or duration <= 0:
                raise BrowserError("Unable to determine the video duration.", 422)
            info = {"id": video["id"], "version": video["version"], "name": video["name"], "size": video["size"],
                    "duration": duration, "width": stream["width"], "height": stream["height"], "fps": fps,
                    "codec": codec, "video_codec": mime, "bit_depth": depth,
                    "audio": audio["codec_name"] if audio else None, "channels": audio.get("channels", 0) if audio else 0,
                    "bitrate": int(payload.get("format", {}).get("bit_rate") or video["size"] * 8 / duration)}
            self.cache.put(self.key("metadata", video), json.dumps(info).encode())
            return info

    def timeline(self, video, info):
        key = self.key("timeline", video)
        cached = self.cached_json(key)
        if cached:
            return cached, None
        return None, self.jobs.submit(key, lambda cancel: self._timeline(video, info, cancel))

    def _timeline(self, video, info, cancel):
        times, origin = [], "packets"
        if Path(video["path"]).suffix.lower() == ".mkv":
            try:
                times = Matroska(self.source, video).keyframes()
                if times:
                    origin = "cues"
            except (ValueError, IndexError, OverflowError):
                pass
        if not times:
            with self.cache.workspace(8 * 1024**2) as (work, reservation):
                command = [str(self.settings.ffprobe), "-v", "error", "-select_streams", "v:0", "-show_packets",
                           "-show_entries", "packet=pts_time,flags", "-of", "csv=p=0", self.url(video, 0)]
                data = self.run(command, work, reservation, cancel, video, timeout=600)
                for line in data.decode().splitlines():
                    fields = line.split(",")
                    if len(fields) >= 2 and "K" in fields[1]:
                        try:
                            times.append(float(fields[0]))
                        except ValueError:
                            pass
        times = sorted(set(t for t in times if math.isfinite(t) and 0 <= t < info["duration"] - 0.05))
        if not times:
            raise BrowserError("Unable to build a reliable keyframe timeline.", 422)
        # Cues normally start a few milliseconds after the container's negative audio start.
        if times[0] > 0.25:
            raise BrowserError("The opening keyframe is missing; a reliable timeline cannot be built.", 422)
        boundaries = [0.0]
        for t in times[1:]:
            if t - boundaries[-1] >= 4:
                boundaries.append(t)
        boundaries.append(info["duration"])
        result = {"boundaries": boundaries, "origin": origin}
        self.library.video(video["id"], video["version"])
        self.cache.put(self.key("timeline", video), json.dumps(result).encode())
        return result

    def segment_key(self, video, audio, index):
        return self.key("media", video, f"v2-{audio}/{index}")

    def segment(self, video, info, timeline, audio, index, priority=0):
        key = self.segment_key(video, audio, index)
        with self.cache.pin(key):
            if self.cache.get(key):
                return None
        return self.jobs.submit(key, lambda cancel: self._segment(video, info, timeline, audio, index, cancel), priority)

    def _segment(self, video, info, timeline, audio, index, cancel):
        key = self.segment_key(video, audio, index)
        boundaries = timeline["boundaries"]
        start, duration = boundaries[index], boundaries[index+1] - boundaries[index]
        estimate = max(8 * 1024**2, int(info["bitrate"] * duration / 8 * 2 + duration * 32_000))
        with self.cache.pin(key), self.cache.workspace(estimate) as (work, reservation):
            mp4 = work / "fragment.mp4"
            command = [str(self.settings.ffmpeg), "-nostdin", "-hide_banner", "-loglevel", "error", "-seek_timestamp", "1", "-ss", f"{start:.6f}",
                       "-i", self.url(video, 0), "-t", f"{duration:.6f}", "-map", "0:v:0", "-map", "0:a:0?",
                       "-c:v", "copy", "-sn", "-dn", "-threads", "2"]
            if audio == "aac":
                command += ["-c:a", "aac", "-b:a", "128k"]
            else:
                command += ["-c:a", "copy"]
            command += ["-avoid_negative_ts", "make_zero", "-movflags", "+frag_keyframe+empty_moov+default_base_moof", "-y", str(mp4)]
            self.run(command, work, reservation, cancel, video)
            probe = json.loads(self.run([str(self.settings.ffprobe), "-v", "error", "-show_streams", "-show_format",
                                        "-of", "json", str(mp4)], work, reservation, cancel, video))
            output_video = next(s for s in probe["streams"] if s["codec_type"] == "video")
            if output_video["codec_name"] != info["codec"] or abs(float(probe["format"]["duration"]) - duration) > 0.2:
                raise BrowserError("The remuxed segment does not match the keyframe timeline. Cache publication was rejected.", 422)
            init, fragment = split_mp4(mp4.read_bytes())
            # Move rather than duplicate a potentially large fragment in the workspace.
            mp4.unlink()
            output = work / "result"
            output.mkdir()
            (output / "init.mp4").write_bytes(init)
            (output / "segment.m4s").write_bytes(fragment)
            self.library.video(video["id"], video["version"])
            return self.cache.publish(key, output, reservation)

    def image(self, video, kind, owner=None, priority=None):
        key = self.image_key(video, kind)
        with self.cache.pin(key):
            if self.cache.get(key):
                return None
        if kind == 'preview':
            failed = self.library.rows('SELECT error FROM preview_failures WHERE video_id=? AND version=? AND retry>?',
                                       (video['id'], video['version'], time.time()))
            if failed:
                raise BrowserError(failed[0]['error'], 422)
        priority = priority if priority is not None else (10 if kind == "poster" else 20)
        return self.jobs.submit(key, lambda cancel: self._image(video, kind, cancel), priority, owner)

    def image_key(self, video, kind):
        return self.key(kind, video, 'four-v1') if kind == 'preview' else self.key(kind, video)

    def _image(self, video, kind, cancel):
        key = self.image_key(video, kind)
        with self.cache.pin(key), self.cache.workspace(4 * 1024**2) as (work, reservation):
            if kind == "poster":
                image = work / "image.jpg"
                command = [str(self.settings.ffmpeg), "-nostdin", "-hide_banner", "-loglevel", "error", "-ss", "1",
                           "-i", self.url(video, 10), "-frames:v", "1", "-vf", "scale=480:-2", "-q:v", "4", "-threads", "2", "-y", str(image)]
                self.run(command, work, reservation, cancel, video)
                if not image.is_file() or image.stat().st_size == 0:
                    raise BrowserError(f"No video frame could be decoded for {video['path']} at 1 second. The video track may be empty or truncated.", 422)
            else:
                info = self.cached_json(self.key("metadata", video)) or self._probe(video, cancel, 20)
                for i, fraction in enumerate((0.2, 0.4, 0.6, 0.8)):
                    frame = work / f"frame{i:02d}.jpg"
                    position = info['duration'] * fraction
                    command = [str(self.settings.ffmpeg), "-nostdin", "-hide_banner", "-loglevel", "error",
                               "-seek_timestamp", "1", "-ss", f"{position:.6f}", "-i", self.url(video, 20),
                               "-map", "0:v:0", "-frames:v", "1", "-vf", "scale=320:180:force_original_aspect_ratio=decrease,pad=320:180:(ow-iw)/2:(oh-ih)/2",
                               "-q:v", "4", "-threads", "2", "-y", str(frame)]
                    self.run(command, work, reservation, cancel, video)
                    if not frame.is_file() or frame.stat().st_size == 0:
                        raise BrowserError(f"No video frame could be decoded for {video['path']} at {position:.2f} seconds ({fraction:.0%}). The video track may be empty, truncated, or shorter than the reported duration.", 422)
                image = work / "image.jpg"
                command = [str(self.settings.ffmpeg), "-nostdin", "-hide_banner", "-loglevel", "error", "-framerate", "1",
                           "-i", str(work / "frame%02d.jpg"), "-vf", "tile=2x2:padding=4:margin=4", "-frames:v", "1", "-q:v", "3", "-y", str(image)]
                self.run(command, work, reservation, cancel, video)
            self.library.video(video["id"], video["version"])
            if not image.exists():
                raise BrowserError("Unable to generate the preview image.", 422)
            return self.cache.publish(key, image, reservation)

    def close(self):
        with self.lock:
            for process in self.processes:
                if process.poll() is None:
                    process.terminate()
