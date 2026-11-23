"""Shared paths, deterministic tables and HTTP helpers for mission-data ingestion.

See docs/16_2026-11-23_mission_data_ingestion.md. Every generated table is
written byte-identically for identical inputs: UTF-8, LF line endings, sorted
or document-ordered rows, and no timestamps unless a row records a fetch.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
# Committed: raw-file manifest, human-recorded decisions, masters and AiRT2 indexes.
MISSION_DATA = REPO / 'MISSION_DATA'
REVIEW = MISSION_DATA / 'review'
# Not committed (.gitignore): journal snapshots, regenerable extractions, caches, work queues and diagnostics.
LOCAL = REPO / '.local' / 'mission_data'
JOURNALS = LOCAL / 'journals'
SCRAPED = LOCAL / 'scraped_data'
WORK = LOCAL / 'review'  # generated decision tables and review queues
AI_CACHE = LOCAL / 'ai'
INDEXES = MISSION_DATA / 'indexes'
RAW_MANIFEST = MISSION_DATA / 'raw_manifest.tsv'

RAW_ROOT = Path(os.environ.get('A15_RAW_ROOT', 'N:/Projects/Apollo/Apollo 15 Project'))
FLICKR_ROOT = Path(os.environ.get('A15_FLICKR_ROOT',
                                  'N:/Projects/Apollo/Mission Data/Apollo Images from flickr/AS15'))
MEDIA_ROOT = Path(os.environ.get('A15_MEDIA_ROOT', 'N:/Projects/Apollo/media.apolloinrealtime.org/A15'))

AFJ_BASE = 'https://apollojournals.org/afj/ap15fj/'
ALSJ_BASE = 'https://apollojournals.org/alsj/a15/'
USER_AGENT = 'apollo15-mission-data/1.0 (Apollo in Real Time; research archive)'

# NASA range zero, the epoch shared with the mission-timing plan.
RANGE_ZERO = datetime(1971, 7, 26, 13, 34, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------- GET helpers
def parse_get(text: str) -> int | None:
    """``HHH:MM:SS``, ``-HHH:MM:SS`` or an anchor ``HHHMMSS``/``-HHHMMSS`` -> seconds."""
    text = (text or '').strip()
    m = re.fullmatch(r'(-?)(\d{1,3}):(\d{2}):(\d{2})', text) or re.fullmatch(r'(-?)(\d{3})(\d{2})(\d{2})', text)
    if not m:
        return None
    sign, h, mi, s = m.groups()
    if int(mi) >= 60 or int(s) >= 60:
        return None
    value = int(h) * 3600 + int(mi) * 60 + int(s)
    return -value if sign else value


def format_get(seconds: int | None) -> str:
    if seconds is None or seconds == '':
        return ''
    seconds = int(seconds)
    sign = '-' if seconds < 0 else ''
    h, rem = divmod(abs(seconds), 3600)
    return f'{sign}{h:03d}:{rem // 60:02d}:{rem % 60:02d}'


def utc_to_get(utc: datetime) -> int:
    if utc.tzinfo is None:
        utc = utc.replace(tzinfo=timezone.utc)
    return round((utc - RANGE_ZERO).total_seconds())


def nfc(text: str) -> str:
    return unicodedata.normalize('NFC', text or '')


def squash(text: str) -> str:
    return re.sub(r'\s+', ' ', nfc(text)).strip()


def tokens(text: str) -> list[str]:
    """Comparison tokens: case, punctuation and apostrophes ignored, digits kept."""
    text = unicodedata.normalize('NFKC', text or '').casefold()
    text = re.sub(r"['’]", '', text)
    return re.findall(r'[a-z0-9]+', text)


def short_hash(text: str, length: int = 10) -> str:
    return hashlib.sha256(nfc(text).encode('utf-8')).hexdigest()[:length]


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path, block=8 * 1024 * 1024) -> str:
    h = hashlib.sha256()
    with open(path, 'rb') as stream:
        while chunk := stream.read(block):
            h.update(chunk)
    return h.hexdigest()


# ------------------------------------------------------------- table helpers
def _cell(value) -> str:
    if value is None:
        return ''
    if isinstance(value, float):
        return f'{value:.6g}'
    if isinstance(value, (list, tuple)):
        return ';'.join(map(str, value))
    return str(value).replace('\t', ' ').replace('\r', ' ').replace('\n', ' ')


def write_if_changed(path: Path, data: bytes) -> bool:
    path = Path(path)
    if path.is_file() and path.read_bytes() == data:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.tmp')
    tmp.write_bytes(data)
    os.replace(tmp, path)
    return True


def write_tsv(path: Path, fields, rows) -> bool:
    out = io.StringIO()
    out.write('\t'.join(fields) + '\n')
    for row in rows:
        out.write('\t'.join(_cell(row.get(f)) for f in fields) + '\n')
    return write_if_changed(path, out.getvalue().encode('utf-8'))


def read_tsv(path: Path) -> list[dict]:
    path = Path(path)
    if not path.is_file():
        return []
    with path.open(encoding='utf-8', newline='') as handle:
        return list(csv.DictReader(handle, delimiter='\t', quoting=csv.QUOTE_NONE))


def write_pipe(path: Path, rows) -> bool:
    """Legacy headerless pipe files read by the website builds."""
    lines = []
    for row in rows:
        cells = [_cell(c).replace('|', '/') for c in row]
        lines.append('|'.join(cells))
    return write_if_changed(path, ('\n'.join(lines) + '\n').encode('utf-8'))


def write_json(path: Path, value) -> bool:
    return write_if_changed(path, (json.dumps(value, indent=1, sort_keys=True, ensure_ascii=False) + '\n').encode('utf-8'))


def load_env(path: Path = REPO / '.env') -> dict:
    values = {}
    if Path(path).is_file():
        for line in Path(path).read_text(encoding='utf-8').splitlines():
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                key, value = line.split('=', 1)
                values[key.strip()] = value.strip().strip('"').strip("'")
    return {**values, **{k: v for k, v in os.environ.items() if k in values or k.startswith(('HGX_', 'STRATA_'))}}


# ---------------------------------------------------------------------- HTTP
class Fetcher:
    """Throttled, retrying HTTP GET. ``delay`` seconds between requests."""

    def __init__(self, delay: float = 1.5, retries: int = 3, timeout: float = 120):
        self.delay, self.retries, self.timeout = delay, retries, timeout
        self._last = 0.0

    def _wait(self):
        pause = self._last + self.delay - time.monotonic()
        if pause > 0:
            time.sleep(pause)
        self._last = time.monotonic()

    def open(self, url: str):
        last = None
        for attempt in range(self.retries):
            self._wait()
            try:
                request = urllib.request.Request(url, headers={'User-Agent': USER_AGENT})
                return urllib.request.urlopen(request, timeout=self.timeout)
            except urllib.error.HTTPError as exc:
                if exc.code in (404, 410, 403):
                    raise
                last = exc
            except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
                last = exc
            time.sleep(2 ** attempt * 3)
        raise RuntimeError(f'GET {url} failed after {self.retries} attempts: {last}')

    def get(self, url: str) -> tuple[bytes, str]:
        with self.open(url) as response:
            return response.read(), response.geturl()

    def download(self, url: str, target: Path, chunk=4 * 1024 * 1024) -> tuple[str, int, str]:
        """Stream ``url`` to ``target`` atomically. Returns (sha256, size, final_url)."""
        target = Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_name(target.name + '.partial')
        h, size = hashlib.sha256(), 0
        with self.open(url) as response, partial.open('wb') as out:
            final = response.geturl()
            while block := response.read(chunk):
                h.update(block)
                out.write(block)
                size += len(block)
        os.replace(partial, target)
        return h.hexdigest(), size, final


def today() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%d')


def absolute(base: str, href: str) -> str:
    return urllib.parse.urljoin(base, (href or '').strip())
