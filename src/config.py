"""Application paths and conservative resource limits."""
from dataclasses import dataclass
from pathlib import Path
import os

PROJECT = Path(__file__).resolve().parent.parent
VIDEO_EXTENSIONS = {'.mkv', '.mp4', '.m4v', '.webm', '.mov', '.avi', '.ts', '.m2ts', '.ogv', '.wmv'}
PAGE_SIZE = 48
BLOCK_SIZE = 4 * 1024 * 1024


@dataclass(frozen=True)
class Settings:
    source: Path
    cache: Path
    max_bytes: int = 32 * 1024**3
    min_free: int = 16 * 1024**3
    ffmpeg: Path = PROJECT / 'bin/ffmpeg'
    ffprobe: Path = PROJECT / 'bin/ffprobe'

    def __post_init__(self):
        object.__setattr__(self, 'source', Path(self.source).resolve())
        object.__setattr__(self, 'cache', Path(self.cache).resolve())

    @classmethod
    def load(cls, source: Path, cache: Path | None = None):
        return cls(source, cache or Path(os.environ.get('CACHE_DIR', PROJECT / 'cache')),
                   int(float(os.environ.get('CACHE_GIB', '32')) * 1024**3),
                   int(float(os.environ.get('CACHE_MIN_FREE_GIB', '16')) * 1024**3))

    def validate(self):
        if not self.source.is_dir():
            raise ValueError(f'Video directory does not exist: {self.source}')
        if self.cache.is_relative_to(self.source) or self.source.is_relative_to(self.cache):
            raise ValueError('The cache and source directories must not overlap.')
        if self.max_bytes < 16 * 1024**2 or self.min_free < 0:
            raise ValueError('Invalid cache capacity settings.')
        for tool in (self.ffmpeg, self.ffprobe):
            if not os.access(tool, os.X_OK):
                raise ValueError(f'Missing project tool: {tool}')
