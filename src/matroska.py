"""Read Matroska seek metadata without scanning video packet payloads."""
import struct
from collections import OrderedDict
from .config import BLOCK_SIZE


class Matroska:
    def __init__(self, source, video):
        self.source, self.video = source, video
        self.buffers = OrderedDict()

    def read(self, offset, size):
        end, parts = min(offset + size, self.video["size"]), []
        while offset < end:
            block, within = divmod(offset, BLOCK_SIZE)
            if block not in self.buffers:
                self.buffers[block] = self.source.block(self.video, block)
            self.buffers.move_to_end(block)
            while len(self.buffers) > 2:
                self.buffers.popitem(last=False)
            data = self.buffers[block]
            length = min(end - offset, len(data) - within)
            if length <= 0:
                raise ValueError("Truncated EBML")
            parts.append(data[within:within + length])
            offset += length
        return b"".join(parts)

    def header(self, offset):
        data = self.read(offset, 16)
        if not data:
            raise ValueError("Truncated EBML")
        def vint(start, mask):
            first = data[start]
            length = next((n for n in range(1, 9) if first & (1 << (8 - n))), None)
            if length is None or start + length > len(data):
                raise ValueError("Invalid EBML integer")
            value = int.from_bytes(data[start:start+length], "big")
            if mask:
                value &= (1 << (7 * length)) - 1
            return value, length
        id, n = vint(0, False)
        size, m = vint(n, True)
        start = offset + n + m
        if size == (1 << (7 * m)) - 1:
            size = self.video["size"] - start
        return id, start, min(start + size, self.video["size"])

    def children(self, start, end):
        while start < end:
            id, body, finish = self.header(start)
            if finish <= start or finish > end:
                raise ValueError("Invalid EBML element")
            yield id, body, finish
            start = finish

    def integer(self, start, end):
        if end - start > 8:
            raise ValueError("Invalid EBML integer")
        return int.from_bytes(self.read(start, end - start), "big")

    def keyframes(self):
        segment = None
        for id, start, end in self.children(0, self.video["size"]):
            if id == 0x18538067:
                segment = (start, end)
                break
        if not segment:
            return []
        base, end = segment
        locations = {}
        # Stop at the first Cluster; the SeekHead normally points to tail Cues.
        for id, start, finish in self.children(base, end):
            locations[id] = (start, finish)
            if id == 0x114D9B74:
                for entry_id, entry, entry_end in self.children(start, finish):
                    if entry_id != 0x4DBB:
                        continue
                    target = position = None
                    for tag, a, b in self.children(entry, entry_end):
                        if tag == 0x53AB:
                            target = self.integer(a, b)
                        elif tag == 0x53AC:
                            position = self.integer(a, b)
                    if target is not None and position is not None:
                        actual, a, b = self.header(base + position)
                        if target == actual:
                            locations[target] = (a, b)
            if id == 0x1F43B675:
                break
        if 0x1C53BB6B not in locations or 0x1654AE6B not in locations:
            return []
        scale, track = 1_000_000, None
        if 0x1549A966 in locations:
            for id, a, b in self.children(*locations[0x1549A966]):
                if id == 0x2AD7B1:
                    scale = self.integer(a, b)
        for id, a, b in self.children(*locations[0x1654AE6B]):
            if id != 0xAE:
                continue
            values = {tag: self.integer(x, y) for tag, x, y in self.children(a, b) if tag in (0xD7, 0x83)}
            if values.get(0x83) == 1:
                track = values.get(0xD7)
                break
        times = []
        for id, a, b in self.children(*locations[0x1C53BB6B]):
            if id != 0xBB:
                continue
            timestamp, tracks = None, []
            for tag, x, y in self.children(a, b):
                if tag == 0xB3:
                    timestamp = self.integer(x, y)
                elif tag == 0xB7:
                    tracks.extend(self.integer(m, n) for child, m, n in self.children(x, y) if child == 0xF7)
            if timestamp is not None and track in tracks:
                times.append(timestamp * scale / 1_000_000_000)
        return sorted(set(times))


def split_mp4(data):
    """Separate initialization boxes from complete moof/mdat fragments."""
    offset, split = 0, None
    while offset + 8 <= len(data):
        size, type = struct.unpack_from(">I4s", data, offset)
        if size == 1:
            size = struct.unpack_from(">Q", data, offset + 8)[0]
        if size == 0:
            size = len(data) - offset
        if size < 8 or offset + size > len(data):
            raise ValueError("Incomplete MP4 box")
        if type == b"moof" and split is None:
            split = offset
        offset += size
    if split is None or offset != len(data):
        raise ValueError("Invalid fragmented MP4")
    return data[:split], data[split:]
