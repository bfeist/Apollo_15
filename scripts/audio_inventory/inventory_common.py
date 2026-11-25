"""Shared paths, tape keys, mission names and cached HTTP for the audio inventory.

The inventory joins three listings of NASA mission audio by tape number:

- the JSC "NASA Historical Audio Archive - Digital Audio File Metadata" PDF,
- the Internet Archive uploads JSC made from it (``nasaaudiocollection``),
- the National Archives catalog (Record Group 255, sound recordings).

A tape key is the JSC file name, e.g. ``540-AAA`` or ``3304``. NARA local
identifiers carry the same number behind a series prefix (``255-PAOa-602-AAA``
-> ``602-AAA``, ``255-STa-3304`` -> ``3304``). Whether a match is real is
checked by comparing missions, not assumed.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from md_common import LOCAL, USER_AGENT

OUT = LOCAL / 'audio_inventory'                 # generated tables (local only, not committed)
CACHE = LOCAL / 'audio_inventory'               # raw API responses (not committed)
JSC_PDF = Path(os.environ.get(
    'A15_JSC_AUDIO_INDEX', 'N:/Projects/Apollo/Mission Data/NASA-Audio-Archive_Digital-Audio-File_Metadata.pdf'))
NARA_AUDIO_ROOT = Path(os.environ.get('A15_NARA_AUDIO_ROOT', 'F:/Apollo_15_data/raw/audio/NARA'))
IA_AUDIO_ROOT = Path(os.environ.get('A15_SG_ROOT', 'F:/Apollo_15_data/raw/audio/Internet Archive'))

ROMAN = {'I': 1, 'II': 2, 'III': 3, 'IV': 4, 'V': 5, 'VI': 6, 'VII': 7, 'VIII': 8, 'IX': 9, 'X': 10,
         'XI': 11, 'XII': 12, 'XIII': 13, 'XIV': 14, 'XV': 15, 'XVI': 16, 'XVII': 17}
TO_ROMAN = {v: k for k, v in ROMAN.items()}
# Mercury flights by JSC code (Mercury-Redstone / Mercury-Atlas numbering in NARA titles).
MERCURY = {3: ('MR-3', 'Freedom 7'), 4: ('MR-4', 'Liberty Bell 7'), 5: ('MA-5', 'Enos'),
           6: ('MA-6', 'Friendship 7'), 7: ('MA-7', 'Aurora 7'), 8: ('MA-8', 'Sigma 7'), 9: ('MA-9', 'Faith 7')}


# ------------------------------------------------------------------ tape keys
_KEY = re.compile(r'(?<![0-9])(\d{1,4})\s*-?\s*(AA[A-Z])(?![A-Z])', re.I)


def tape_key(text: str) -> str:
    """``540-AAA``/``540AAA``/``255-PAOa-540-AAA`` -> ``540-AAA``; a bare tape number -> digits."""
    text = (text or '').strip().rstrip('*')
    m = _KEY.search(text)
    if m:
        return f'{int(m.group(1)):03d}-{m.group(2).upper()}'
    m = re.fullmatch(r'(\d{3,5})', text)
    return str(int(m.group(1))) if m else ''


def nara_key(local_id: str) -> str:
    """NARA local identifier -> tape key (series prefix removed)."""
    rest = re.sub(r'^255-[A-Za-z]+-', '', (local_id or '').strip())
    return tape_key(rest) or rest


def ia_file_key(name: str) -> str:
    """Key at the start of an Internet Archive file or item name (``045-AAC_STS-3_...``)."""
    stem = Path(name).name
    m = re.match(r'(\d{1,4})\s*-?\s*(AA[A-Z])(?![A-Za-z])', stem, re.I)
    if m:
        return f'{int(m.group(1)):03d}-{m.group(2).upper()}'
    m = re.match(r'(\d{3,5})(?=[_.\-]|$)', stem)
    return str(int(m.group(1))) if m else ''


# ------------------------------------------------------------------- missions
def mission_of_code(code: str) -> str:
    """JSC mission column (``A/15``, ``G/IV``, ``SL/II``, ``51-F``, ``STS70``) -> canonical name."""
    c = (code or '').strip().upper()
    if m := re.fullmatch(r'A/(\d+)', c):
        return f'Apollo {int(m.group(1))}'
    if c == 'G-7/6':
        return 'Gemini 7/6'
    if m := re.fullmatch(r'G[/-](IXA|[IVX]+)', c):
        return 'Gemini 9A' if m.group(1) == 'IXA' else f'Gemini {ROMAN[m.group(1)]}'
    if m := re.fullmatch(r'M/([IVX]+)', c):
        return f'Mercury {MERCURY[ROMAN[m.group(1)]][0]}' if ROMAN[m.group(1)] in MERCURY else 'Mercury'
    if c in ('MERC', 'MERCURY'):
        return 'Mercury'
    if c == 'GEMINI':
        return 'Gemini'
    if m := re.fullmatch(r'SL/([IV]+)', c):
        return f'Skylab {ROMAN[m.group(1)]}'
    if c == 'ASTP':
        return 'ASTP'
    if c == 'VYGR':
        return 'Voyager'
    if c == 'STS/FF':
        return 'Shuttle ALT'
    if m := re.fullmatch(r'(?:STS-?|07S-)(\d+)', c):
        return f'STS-{int(m.group(1))}'
    if m := re.fullmatch(r'(\d{2})-?([A-Z])', c):
        return f'STS-{m.group(1)}{m.group(2)}'
    if c == 'STS':
        return 'STS'
    return 'General'


def nara_queries(mission: str) -> list[str]:
    """Quoted NARA search phrases for a mission (the catalog needs phrase quotes)."""
    q = lambda *terms: [f'"{t}"' for t in terms]
    if m := re.fullmatch(r'Apollo (\d+)', mission):
        n = int(m.group(1))
        extra = ['AS-204'] if n == 1 else []
        return q(f'Apollo {n}', f'Apollo {TO_ROMAN[n]}', *extra)
    if mission == 'Gemini 7/6':
        return q('Gemini 7', 'Gemini VII', 'GT-7', 'GT-VII', 'Gemini 6', 'Gemini VI', 'GT-6', 'GT-VI')
    if mission == 'Gemini 9A':
        return q('Gemini 9', 'Gemini IX', 'GT-9', 'GT-IX')
    if m := re.fullmatch(r'Gemini (\d+)', mission):
        n = int(m.group(1))
        return q(f'Gemini {n}', f'Gemini {TO_ROMAN[n]}', f'GT-{n}', f'GT-{TO_ROMAN[n]}')
    if m := re.fullmatch(r'Mercury (M[RA]-\d)', mission):
        number = int(m.group(1)[-1])
        return q(m.group(1), MERCURY[number][1])
    if m := re.fullmatch(r'Skylab (\d)', mission):
        n = int(m.group(1))
        return q(f'Skylab {n}', f'Skylab {TO_ROMAN[n]}', f'SL-{n}')
    if mission == 'ASTP':
        return q('Apollo-Soyuz', 'Apollo Soyuz', 'ASTP')
    if mission == 'Voyager':
        return q('Voyager')
    if mission == 'Shuttle ALT':
        return q('Approach and Landing Test', 'ALT Free Flight')
    if m := re.fullmatch(r'STS-(\d{2})([A-Z])', mission):
        a, b = m.groups()
        return q(f'STS-{a}{b}', f'STS {a}-{b}', f'STS-{a}-{b}', f'{a}-{b}')
    if re.fullmatch(r'STS-\d+', mission):
        return q(mission)
    return []


def mission_of_text(text: str) -> str:
    """Best-effort mission named in a title or item name (for rows without a JSC entry)."""
    # Underscores separate words in Internet Archive names, so \b is not used.
    t = re.sub(r'[_]+', ' ', text or '')
    if m := re.search(r'\bA(\d{1,2}) T\d', t):
        return f'Apollo {int(m.group(1))}'
    if re.search(r'Apollo[- ]Soyuz|\bASTP\b', t, re.I):
        return 'ASTP'
    if m := re.search(r'\bSTS[ -]?(\d{2})[ -]?([A-L])\b', t, re.I):
        return f'STS-{m.group(1)}{m.group(2).upper()}'
    for pattern, name in ((r'\bApollo[ -]?(\d{1,2})(?!\d)', 'Apollo'), (r'\bGemini[ -]?(\d{1,2})(?!\d)', 'Gemini'),
                          (r'\bGT-(\d{1,2})(?!\d)', 'Gemini'), (r'\bSky[Ll]ab[ -]?(\d)(?!\d)', 'Skylab'),
                          (r'\bSTS[ -]?(\d{1,3})(?!\d)', 'STS')):
        if m := re.search(pattern, t, re.I):
            return f'STS-{int(m.group(1))}' if name == 'STS' else f'{name} {int(m.group(1))}'
    if m := re.search(r'\bApollo ([IVX]+)\b', t):
        return f'Apollo {ROMAN[m.group(1)]}' if m.group(1) in ROMAN else ''
    if m := re.search(r'\bGT-([IVX]+)\b|\bGemini ([IVX]+)\b', t):
        r = m.group(1) or m.group(2)
        return f'Gemini {ROMAN[r]}' if r in ROMAN else ''
    if m := re.search(r'\b(M[RA]-\d)\b', t):
        return f'Mercury {m.group(1)}'
    return ''


# Missions searched in the NARA catalog in addition to those the JSC index lists.
EXTRA_MISSIONS = (['Apollo 1'] + [f'Apollo {n}' for n in range(4, 18)] + [f'Gemini {n}' for n in range(3, 13)]
                  + [f'Mercury {v[0]}' for v in MERCURY.values()] + ['Skylab 2', 'Skylab 3', 'Skylab 4', 'ASTP'])


def mission_sort_key(mission: str):
    order = ('Mercury', 'Gemini', 'Apollo', 'Skylab', 'ASTP', 'Shuttle ALT', 'STS', 'Voyager', 'General')
    family = next((i for i, f in enumerate(order) if mission.startswith(f)), len(order))
    m = re.search(r'(\d+)', mission)
    return family, int(m.group(1)) if m else 0, mission


# ---------------------------------------------------------------------- HTTP
class CachedJSON:
    """Throttled GET of JSON documents, cached by URL under ``CACHE/<name>``."""

    def __init__(self, name: str, delay: float = 1.0, refresh: bool = False, headers: dict | None = None):
        self.root = CACHE / name
        self.delay, self.refresh, self.headers = delay, refresh, headers or {}
        self._last = 0.0
        self.requests = 0

    def path(self, url: str) -> Path:
        return self.root / (hashlib.sha256(url.encode('utf-8')).hexdigest()[:24] + '.json')

    def get(self, url: str):
        path = self.path(url)
        if path.is_file() and not self.refresh:
            return json.loads(path.read_text(encoding='utf-8'))['response']
        last = None
        for attempt in range(4):
            pause = self._last + self.delay - time.monotonic()
            if pause > 0:
                time.sleep(pause)
            self._last = time.monotonic()
            try:
                request = urllib.request.Request(url, headers={'User-Agent': USER_AGENT,
                                                               'Accept': 'application/json', **self.headers})
                with urllib.request.urlopen(request, timeout=120) as response:
                    body = response.read()
                value = json.loads(body)
                break
            except urllib.error.HTTPError as exc:
                if exc.code in (400, 401, 403, 404):
                    raise
                last = exc
            except (urllib.error.URLError, TimeoutError, ConnectionError, json.JSONDecodeError) as exc:
                last = exc  # the NARA proxy answers some requests with its HTML shell
            time.sleep(3 * 2 ** attempt)
        else:
            raise RuntimeError(f'GET {url} failed: {last}')
        self.requests += 1
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({'url': url, 'response': value}, ensure_ascii=False), encoding='utf-8')
        return value


def url_with(base: str, params: dict) -> str:
    return base + '?' + urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
