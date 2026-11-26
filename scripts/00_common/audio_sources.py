"""Stable source identities, sample-exact readers and media checksums for transcription.

Shared by the 30-track timing transcript (stage 06), the space-to-ground reels
(scripts/sg-audio/01_sg-internetarchive), the NARA flight tapes (01_sg-nara) and the DVD audio streams (scripts/video/dvds). Each caller
supplies its own source inventory; this module is collection-agnostic.

Source identities omit drive letters. A changed checksum, origin, review or map
produces a new identity, so dependent transcript chunks are never reused.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
from dataclasses import dataclass, field, asdict
from pathlib import Path

import numpy as np

# Small processing products of the non-30-track sources (SG reels, DVD streams): one folder per source family.
PROCESSING_ROOT = Path(os.environ.get('A15_PROCESSING_ROOT', 'F:/Apollo_15_data/processing'))


def canonical_json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def digest(value, length=16):
    return hashlib.sha256(canonical_json(value).encode('utf-8')).hexdigest()[:length]


def file_sha256(path, block=8 * 1024 * 1024):
    h = hashlib.sha256()
    with open(path, 'rb') as stream:
        while chunk := stream.read(block):
            h.update(chunk)
    return h.hexdigest()


@dataclass
class Source:
    key: str                      # e.g. tape/A15_T510093/CH18, sg/572-AAA, dvd/D04-T02-P01/a0
    collection: str
    relpath: str                  # collection-relative POSIX path
    path: str                     # resolved local path for this machine
    sample_rate: int
    frames: int
    source_channels: int
    mix: str                      # single_channel | equal_weight_mean
    stream: int = 0               # zero-based audio stream (DVD)
    container: str = 'wav'
    sample_format: str = ''
    canonical_origin_s: float = 0.0
    time_domain: str = 'source_elapsed_seconds'
    content_sha256: str = ''
    hash_origin: str = ''
    size: int = 0
    mtime_ns: int = 0
    tape: str | None = None
    channel: int | None = None
    review: dict = field(default_factory=dict)
    pts_origin_s: float = 0.0
    label: str = ''

    @property
    def hours(self):
        return self.frames / self.sample_rate / 3600

    def identity(self):
        """Fields whose change invalidates transcripts and downstream evidence."""
        return dict(key=self.key, collection=self.collection, relpath=self.relpath,
                    sample_rate=self.sample_rate, frames=self.frames, mix=self.mix, stream=self.stream,
                    source_channels=self.source_channels, content_sha256=self.content_sha256,
                    canonical_origin_s=self.canonical_origin_s, pts_origin_s=self.pts_origin_s,
                    review_sha256=self.review.get('sha256'), map_sha256=self.review.get('map_sha256'))

    @property
    def identity_id(self):
        return digest(self.identity())

    def to_json(self):
        return dict(asdict(self), identity_id=self.identity_id, hours=self.hours)

    @classmethod
    def from_json(cls, data):
        data = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**data)

    def stat_matches(self):
        stat = Path(self.path).stat()
        return (stat.st_size, stat.st_mtime_ns) == (self.size, self.mtime_ns)


# --------------------------------------------------------------------------- WAV access

@dataclass
class WavLayout:
    data_offset: int
    data_bytes: int
    channels: int
    rate: int
    bits: int
    float_format: bool

    @property
    def block(self):
        return self.channels * self.bits // 8

    @property
    def frames(self):
        return self.data_bytes // self.block

    @property
    def description(self):
        return f"{'FLOAT' if self.float_format else 'PCM'}_{self.bits}"


def wav_layout(path):
    """Locate PCM sample bytes directly (RIFF or RF64) for checksummed range reads."""
    with open(path, 'rb') as f:
        head = f.read(12)
        if head[:4] not in (b'RIFF', b'RF64') or head[8:12] != b'WAVE':
            raise ValueError(f'Not a WAV file: {path}')
        rf64_size = None
        fmt = None
        while True:
            header = f.read(8)
            if len(header) < 8:
                raise ValueError(f'No data chunk in {path}')
            ident, size = header[:4], struct.unpack('<I', header[4:])[0]
            if ident == b'ds64':
                body = f.read(size)
                rf64_size = struct.unpack('<Q', body[8:16])[0]
                f.seek(size % 2, 1)
            elif ident == b'fmt ':
                body = f.read(size)
                tag, channels, rate, _, _, bits = struct.unpack('<HHIIHH', body[:16])
                if tag == 0xFFFE:
                    tag = struct.unpack('<H', body[24:26])[0]
                if tag not in (1, 3):
                    raise ValueError(f'Unsupported WAV format tag {tag}: {path}')
                fmt = (channels, rate, bits, tag == 3)
                f.seek(size % 2, 1)
            elif ident == b'data':
                if fmt is None:
                    raise ValueError(f'WAV data precedes fmt: {path}')
                data_bytes = rf64_size if (size == 0xFFFFFFFF and rf64_size is not None) else size
                return WavLayout(f.tell(), data_bytes, *fmt)
            else:
                f.seek(size + size % 2, 1)


def decode_pcm(raw, layout):
    if layout.float_format:
        dtype = {32: '<f4', 64: '<f8'}[layout.bits]
        samples = np.frombuffer(raw, dtype=dtype).astype(np.float32)
    elif layout.bits == 16:
        samples = np.frombuffer(raw, dtype='<i2').astype(np.float32) / 32768.0
    elif layout.bits == 24:
        b = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
        value = b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16)
        value = np.where(value >= 1 << 23, value - (1 << 24), value)
        samples = value.astype(np.float32) / float(1 << 23)
    elif layout.bits == 32:
        samples = np.frombuffer(raw, dtype='<i4').astype(np.float32) / float(1 << 31)
    else:
        raise ValueError(f'Unsupported PCM width {layout.bits}')
    return samples.reshape(-1, layout.channels)


class Reader:
    """Read mono float32 samples at the source rate, with a digest of what was read."""

    def __init__(self, source: Source):
        self.source = source
        self.layout = wav_layout(source.path) if source.container == 'wav' else None

    def frame_bytes(self):
        return self.layout.block if self.layout is not None else 4 * self.source.source_channels

    def read(self, start, end, raw_bytes=False):
        """Return (mono float32, sha256 of the exact source bytes or decoded samples).

        With ``raw_bytes`` the second value is the bytes themselves, so a caller can
        checksum a sub-range without reading it again.
        """
        source = self.source
        start, end = max(0, int(start)), min(source.frames, int(end))
        if end <= start:
            return np.zeros(0, np.float32), (b'' if raw_bytes else hashlib.sha256().hexdigest())
        if self.layout is not None:
            block = self.layout.block
            with open(source.path, 'rb') as f:
                f.seek(self.layout.data_offset + start * block)
                raw = f.read((end - start) * block)
            if len(raw) != (end - start) * block:
                raise ValueError(f'Short read at frame {start} in {source.path}')
            samples = decode_pcm(raw, self.layout)
        else:
            # Count decoded samples from the start of the stream; input seeking can
            # shift compressed-audio timestamps by an encoder-dependent amount.
            raw = subprocess.check_output([
                'ffmpeg', '-v', 'error', '-nostdin', '-i', source.path,
                '-map', f'0:a:{source.stream}', '-af', f'atrim=start_sample={start}:end_sample={end}',
                '-f', 'f32le', '-acodec', 'pcm_f32le', 'pipe:1'], stderr=subprocess.PIPE)
            samples = np.frombuffer(raw, dtype='<f4').reshape(-1, source.source_channels)
            if len(samples) != end - start:
                raise ValueError(f'Decoded {len(samples)} of {end - start} samples at {start} in {source.path}')
        mono = samples[:, 0] if source.mix == 'single_channel' else samples.mean(axis=1)
        return np.ascontiguousarray(mono, dtype=np.float32), (raw if raw_bytes else hashlib.sha256(raw).hexdigest())


# --------------------------------------------------------------------------- inventory

class HashCache:
    """Content checksums for external media keyed by path, size and mtime."""

    def __init__(self, path):
        self.path = Path(path) if path else None
        self.values = {}
        if self.path and self.path.is_file():
            self.values = json.loads(self.path.read_text(encoding='utf-8'))
        self.changed = False

    def get(self, path, compute=True):
        stat = Path(path).stat()
        key = f'{Path(path).resolve()}|{stat.st_size}|{stat.st_mtime_ns}'
        if key not in self.values and compute:
            self.values[key] = file_sha256(path)
            self.changed = True
        return self.values.get(key)

    def save(self):
        if self.path and self.changed:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix('.tmp')
            tmp.write_text(json.dumps(self.values, indent=1, sort_keys=True), encoding='utf-8')
            os.replace(tmp, self.path)
            self.changed = False


def gpu_description():
    if not shutil.which('nvidia-smi'):
        return None
    try:
        out = subprocess.check_output(['nvidia-smi', '--query-gpu=name,driver_version,memory.total',
                                       '--format=csv,noheader,nounits'], text=True).strip().splitlines()[0]
        name, driver, memory = [x.strip() for x in out.split(',')]
        return dict(name=name, driver=driver, memory_mib=int(memory))
    except (subprocess.CalledProcessError, ValueError, IndexError):
        return None
