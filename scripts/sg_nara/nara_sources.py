"""The NARA Apollo 15 recordings as tapes and tracks.

NARA digitized each recording as MP3 (44.1 kHz). The flight tapes ("Historical Apollo 15
Flight Tape N", ``255-PAOa-608-AAA`` and ``640-AAA``..``655-AAA``) are four mono files per
tape, one per playback track (``-tr1``..``-tr4``). The playback head did not sit on the tape's
track pitch, so which file carries which recorded track (IRIG-B timecode, voice loops) changes
from tape to tape and drifts within a tape. The CSM Vox tapes (``602``..``605``) are stereo files
and the DSE dumps (``255-AVTa-15-0341x``) mono; both are surveyed the same way, as tracks.

A track is (tape, file, channel). Checksums come from the download manifest
(``.local/mission_data/audio_inventory/a15_nara_downloads.tsv``, each verified against NARA's
S3 checksum when downloaded) when the file's size matches; otherwise the file is hashed.
"""
from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import subprocess
from dataclasses import dataclass, asdict
from functools import lru_cache
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
NARA_ROOT = Path(os.environ.get('A15_NARA_AUDIO_ROOT', 'F:/Apollo_15_data/raw/audio/NARA'))
PROCESSING_ROOT = Path(os.environ.get('A15_PROCESSING_ROOT', 'F:/Apollo_15_data/processing'))
DATA_ROOT = PROCESSING_ROOT / 'sg_nara'
DOWNLOADS = REPO / '.local/mission_data/audio_inventory/a15_nara_downloads.tsv'
TRACK_RE = re.compile(r'-tr(\d)$', re.I)


@dataclass(frozen=True)
class Track:
    tape: str            # NARA local ID folder, e.g. 255-PAOa-640-AAA
    name: str            # tr1..tr4, or ch1/ch2 of a stereo file, or 'mono'
    path: str
    channel: int | None  # channel of a multichannel file; None for mono files
    sha256: str
    size: int
    sample_rate: int
    channels: int
    duration_s: float

    @property
    def key(self) -> str:
        return f'{self.tape}/{self.name}'

    def row(self) -> dict:
        return asdict(self)


def tape_short(tape: str) -> str:
    """255-PAOa-640-AAA -> 640-AAA; 255-AVTa-15-03414 -> 15-03414."""
    return re.sub(r'^255-[A-Za-z]+-', '', tape)


@lru_cache(maxsize=1)
def manifest() -> dict[str, dict]:
    if not DOWNLOADS.is_file():
        return {}
    with DOWNLOADS.open(encoding='utf-8', newline='') as f:
        return {r['file']: r for r in csv.DictReader(f, delimiter='\t')}


def sha256_of(path: Path) -> str:
    row = manifest().get(path.name)
    if row and int(row['size']) == path.stat().st_size and row.get('verified') == 'yes':
        return row['sha256']
    h = hashlib.sha256()
    with path.open('rb') as f:
        while chunk := f.read(16 << 20):
            h.update(chunk)
    return h.hexdigest()


def probe(path: Path) -> tuple[int, int, float]:
    out = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'a:0', '-show_entries',
                          'stream=sample_rate,channels:format=duration', '-of', 'json', str(path)],
                         capture_output=True, text=True, check=True).stdout
    j = json.loads(out)
    s = j['streams'][0]
    return int(s['sample_rate']), int(s['channels']), float(j['format']['duration'])


def tracks(root: Path = NARA_ROOT, tapes: list[str] | None = None) -> list[Track]:
    out = []
    for folder in sorted(p for p in Path(root).iterdir() if p.is_dir()):
        if tapes and not ({'all', folder.name, tape_short(folder.name)} & set(tapes)):
            continue
        for mp3 in sorted(folder.glob('*.mp3')):
            sr, ch, dur = probe(mp3)
            sha, size = sha256_of(mp3), mp3.stat().st_size
            m = TRACK_RE.search(mp3.stem)
            if ch == 1:
                out.append(Track(folder.name, f'tr{m[1]}' if m else 'mono', str(mp3), None, sha, size, sr, ch, dur))
            else:
                for c in range(ch):
                    out.append(Track(folder.name, f'ch{c + 1}', str(mp3), c, sha, size, sr, ch, dur))
    return out


def by_tape(items: list[Track]) -> dict[str, list[Track]]:
    out: dict[str, list[Track]] = {}
    for t in items:
        out.setdefault(t.tape, []).append(t)
    return out
