"""Apollo 15 NARA recordings placed on GET and compared with our SG reels.

The Internet Archive SG reels 540-AAA..583-AAA cover GET -001:30 to 199:16
(``MISSION_DATA/sg_reels.tsv``); reels 584-AAA..601-AAA (to end of mission)
were never uploaded and are not in the NARA catalog. NARA instead holds:

- "Historical Apollo 15 Flight Tape Number N" (255-PAOa-608-AAA..655-AAA):
  consecutive 6-hour GMT blocks from 1971-07-26 12:00, four tracks each. The
  GET range follows from the tape's place in the series (titles and dates
  contain typos); the scope note's GMT start hour is checked against it.
- "Prime Goss Voice Tape # 15-03xxx" (255-AVTa): the GOSS air-to-ground net in
  ~95-minute reels with "Recorded Start/Stop Time GET DDD:HH:MM:SS".
- DSE dump / on-board tapes (the spacecraft recorder, not the SG loop),
  debriefings and assorted PAO items.

Coverage is the fraction of a record's GET range inside the SG reels' nominal
spans (``have`` >= 95 %, ``partial``, ``missing``). The measured column uses
the coarse reel offsets in ``.local/mission_data/sg/sg_offsets.tsv``: the SG
reels are edited commentary, so their audio covers only part of each span.
"""
from __future__ import annotations

import hashlib
import os
import re
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from inventory_common import NARA_AUDIO_ROOT, OUT, nara_key
from md_common import LOCAL, MISSION_DATA, RANGE_ZERO, USER_AGENT, format_get, parse_get, read_tsv, today, \
    write_tsv

SPLASHDOWN = 295 * 3600 + 11 * 60 + 53
FLIGHT_TAPE_ZERO = datetime(1971, 7, 26, 12, 0, tzinfo=timezone.utc)  # tape 1 = 255-PAOa-608-AAA
GOSS_REEL_S = 95 * 60
FIELDS = ('nara_local_id', 'kind', 'get_start', 'get_end', 'get_basis', 'sg_nominal_pct', 'sg_audio_pct',
          'status', 'needed', 'nara_objects', 'nara_bytes', 'local', 'nara_title', 'nara_scope', 'note',
          'nara_naid', 'nara_url', 'get_start_s', 'get_end_s')
DOWNLOAD_FIELDS = ('nara_local_id', 'file', 'designator', 'size', 'sha256', 's3_sha256', 'verified', 'path',
                   'url', 'fetched')


def kind(title: str, local_id: str) -> str:
    t = title.lower()
    if 'flight tape' in t:
        return 'flight_tape'
    if 'goss' in t:
        return 'goss'
    if 'dse' in t or 'on board' in t:
        return 'onboard'
    if 'debrief' in t:
        return 'debriefing'
    return 'other'


def _get4(text: str) -> int | None:
    """``DDD:HH:MM:SS`` (days first) or ``HHH:MM:SS``; tolerates ``.``/``,`` typos."""
    parts = [p for p in re.split(r'[:.,]+', text.strip(' .')) if p != '']
    if not all(p.isdigit() for p in parts):
        return None
    numbers = list(map(int, parts))
    if len(numbers) == 4:
        d, h, m, s = numbers
    elif len(numbers) == 3:
        d, (h, m, s) = 0, numbers
    else:
        return None
    if h >= 24 and len(numbers) == 4 or m >= 60 or s >= 60:
        return None
    return ((d * 24 + h) * 60 + m) * 60 + s


def get_range(row: dict) -> tuple[int | None, int | None, str, str]:
    """(start, end, basis, note) in GET seconds for a NARA Apollo 15 record."""
    scope, local, title = row['nara_scope'], row['nara_local_id'], row['nara_title']
    k = kind(title, local)
    if k == 'flight_tape' and (m := re.fullmatch(r'255-PAOa-(\d{3})-AAA', local)):
        number = int(m.group(1)) - 607
        start = FLIGHT_TAPE_ZERO + timedelta(hours=6 * (number - 1))
        notes = []
        if (t := re.search(r'Number (\d+)', title)) and int(t.group(1)) != number:
            notes.append(f'title says tape {t.group(1)}; series position {number}')
        if (g := re.search(r'GMT Start (\d{2})(\d{2})', scope)) and int(g.group(1)) != start.hour:
            notes.append(f'scope GMT start {g.group(0)[-4:]} differs from {start:%H%M}')
        begin = round((start - RANGE_ZERO).total_seconds())
        return begin, begin + 6 * 3600, f'GMT {start:%m-%d %H:%M}+6h (tape {number})', '; '.join(notes)
    m = re.search(r'Start Time GET\s*(\S+)\s+to Recorded Stop Time GET\s*(\S+?)\.?$', scope)
    if not m or k == 'debriefing':
        return None, None, '', ''
    start, end = _get4(m.group(1)), _get4(m.group(2))
    printed = f'printed {m.group(1)} to {m.group(2)}'
    ok = start is not None and end is not None and 0 < end - start <= (3 * 3600 if k == 'goss' else 10 ** 7)
    if ok:
        return start, end, 'GET', ''
    if k == 'goss':
        # One printed time is a typo; goss_repair() keeps the one that continues the previous reel.
        return None, None, 'goss?', printed
    if start is not None and end is not None and 0 < start - end <= 2 * 3600:
        return end, start, 'GET (swapped)', f'{printed}; swapped'
    return None, None, '', f'unreadable or inconsistent: {printed}'


def goss_repair(rows: list[dict]) -> None:
    """Inconsistent GOSS reels: a 95-minute reel from whichever printed time continues the series."""
    def number(r):
        return int(r['nara_local_id'].rsplit('-', 1)[-1])
    series = sorted((r for r in rows if r['kind'] == 'goss'), key=number)
    for i, r in enumerate(series):
        if r['get_basis'] != 'goss?':
            continue
        m = re.search(r'printed (\S+) to (\S+)', r['note'])
        start, end = _get4(m.group(1)), _get4(m.group(2).rstrip('.'))
        prev = next((p['get_end_s'] for p in reversed(series[:i]) if p['get_end_s'] is not None
                     and number(p) == number(r) - 1), None)
        options = [(s, f'start + 95 min') for s in [start] if s is not None]
        options += [(e - GOSS_REEL_S, 'stop - 95 min') for e in [end] if e is not None]
        if not options:
            r['get_basis'] = ''
            continue
        best = min(options, key=lambda o: abs(o[0] - prev)) if prev is not None else options[-1]
        r['get_start_s'], r['get_end_s'] = best[0], best[0] + GOSS_REEL_S
        r['get_basis'] = f'GET {best[1]}'
        r['note'] += '; one printed time inconsistent'


def sg_nominal() -> list[tuple[int, int]]:
    spans = []
    for r in read_tsv(MISSION_DATA / 'sg_reels.tsv'):
        a, b = parse_get(r['pao_get_start']), parse_get(r['pao_get_end'])
        if a is not None and b is not None:
            spans.append((a, b))
    return union(spans)


def sg_audio() -> list[tuple[int, int]]:
    """GET intervals that SG reel audio actually covers (60-s offset grid)."""
    spans = []
    for r in read_tsv(LOCAL / 'sg' / 'sg_offsets.tsv'):
        if r.get('rank') == '1':
            g = float(r['source_s']) + float(r['offset_s'])
            spans.append((round(g), round(g) + 60))
    return union(spans)


def union(spans):
    out = []
    for a, b in sorted(spans):
        if out and a <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


def overlap(a: int, b: int, spans) -> int:
    return sum(max(0, min(b, y) - max(a, x)) for x, y in spans)


def _file_key(name: str) -> str:
    return nara_key(re.sub(r'(-tr\d+)?\.(mp3|wav)$', '', name, flags=re.I))


def local_files(local_id: str) -> list[Path]:
    """Downloads in ``<root>/<localID>/`` and earlier manual downloads in the root folder."""
    folder = NARA_AUDIO_ROOT / local_id
    found = [p for p in folder.glob('*') if p.suffix.lower() in ('.mp3', '.wav')] if folder.is_dir() else []
    if NARA_AUDIO_ROOT.is_dir():
        key = nara_key(local_id)
        found += [p for p in NARA_AUDIO_ROOT.glob('*.mp3') if _file_key(p.name) == key]
    return sorted(found)


def table(nara_rows: list[dict]) -> list[dict]:
    nominal, audio = sg_nominal(), sg_audio()
    out = []
    for r in nara_rows:
        start, end, basis, note = get_range(r)
        out.append({**{f: r.get(f, '') for f in ('nara_local_id', 'nara_title', 'nara_scope', 'nara_objects',
                                                 'nara_bytes', 'nara_naid', 'nara_url')},
                    'kind': kind(r['nara_title'], r['nara_local_id']), 'get_basis': basis, 'note': note,
                    'get_start_s': start, 'get_end_s': end})
    goss_repair(out)
    for row in out:
        k, start, end = row['kind'], row['get_start_s'], row['get_end_s']
        row['get_start'], row['get_end'] = format_get(start), format_get(end)
        if start is not None and end > start:
            span = end - start
            row['sg_nominal_pct'] = round(100 * overlap(start, end, nominal) / span)
            row['sg_audio_pct'] = round(100 * overlap(start, end, audio) / span) if audio else ''
            pct = row['sg_nominal_pct']
            row['status'] = 'have' if pct >= 95 else 'partial' if pct > 5 else 'missing'
            if k in ('flight_tape', 'goss') and row['status'] != 'have' and start < SPLASHDOWN + 3600:
                row['needed'] = 'yes'
            elif k == 'onboard':
                row['needed'] = 'onboard recorder (not SG)'
        else:
            row['status'] = 'no GET'
        files = local_files(row['nara_local_id'])
        row['local'] = f'{len(files)} file(s)' if files else ''
    out.sort(key=lambda r: (r['get_start_s'] is None, r['get_start_s'] or 0, r['nara_local_id']))
    return out


SECTIONS = (('Flight tapes, debriefings and other items', lambda k: k not in ('goss', 'onboard')),
            ('GOSS air-to-ground voice tapes', lambda k: k == 'goss'),
            ('Onboard recorder (DSE) tapes', lambda k: k == 'onboard'))


def markdown(rows: list[dict]) -> str:
    return '\n'.join(f'## {title}\n\n' + _table([r for r in rows if keep(r['kind'])])
                     for title, keep in SECTIONS)


def _table(rows: list[dict]) -> str:
    lines = ['| NARA localID | Kind | GET start | GET end | In our SG reels | Needed | NARA digital | Local |',
             '|---|---|---|---|---|---|---|---|']
    for r in rows:
        have = f"{r['status']} ({r['sg_nominal_pct']}%)" if r.get('sg_nominal_pct', '') != '' else r['status']
        digital = f"{r['nara_objects']} file(s), {int(r['nara_bytes'] or 0) / 1e9:.1f} GB" if int(r['nara_objects'] or 0) \
            else 'not digitized'
        lines.append(f"| {r['nara_local_id']} | {r['kind']} | {r['get_start']} | {r['get_end']} | {have} | "
                     f"{r.get('needed', '') or ''} | {digital} | {r['local']} |")
    return '\n'.join(lines) + '\n'


# ------------------------------------------------------------------ download
def _head(url: str) -> dict:
    request = urllib.request.Request(url, method='HEAD', headers={'User-Agent': USER_AGENT})
    with urllib.request.urlopen(request, timeout=120) as response:
        return {'size': int(response.headers.get('Content-Length') or 0),
                'sha256': response.headers.get('x-amz-meta-sha256', '')}


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as stream:
        while block := stream.read(16 * 1024 * 1024):
            h.update(block)
    return h.hexdigest()


def fetch(url: str, target: Path, size: int, attempts: int = 6) -> None:
    """Resumable download to ``target`` through ``target.partial``."""
    partial = target.with_name(target.name + '.partial')
    target.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(attempts):
        have = partial.stat().st_size if partial.exists() else 0
        if have >= size > 0:
            break
        headers = {'User-Agent': USER_AGENT, **({'Range': f'bytes={have}-'} if have else {})}
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=300) as response:
                mode = 'ab' if have and response.status == 206 else 'wb'
                with partial.open(mode) as out:
                    while block := response.read(8 * 1024 * 1024):
                        out.write(block)
        except (OSError, urllib.error.URLError) as exc:
            print(f'  retry {attempt + 1} after {exc}', flush=True)
            time.sleep(10 * (attempt + 1))
    if partial.stat().st_size != size:
        raise RuntimeError(f'{url}: got {partial.stat().st_size} of {size} bytes')
    os.replace(partial, target)


def download(records: dict[str, list[dict]], manifest: Path = OUT / 'a15_nara_downloads.tsv') -> list[dict]:
    """Download every object of the given records; verify against the S3 SHA-256."""
    done = {(r['nara_local_id'], r['file']): r for r in read_tsv(manifest)}
    for local_id, objects in records.items():
        for o in objects:
            target = NARA_AUDIO_ROOT / local_id / o['name']
            previous = done.get((local_id, o['name']))
            if previous and previous['verified'] == 'yes' and target.is_file() \
                    and target.stat().st_size == int(previous['size']):
                continue
            head = _head(o['url'])
            legacy = NARA_AUDIO_ROOT / o['name']  # earlier manual downloads sit in the root folder
            if not target.is_file() and legacy.is_file() and legacy.stat().st_size == head['size']:
                target.parent.mkdir(parents=True, exist_ok=True)
                os.replace(legacy, target)
            if not (target.is_file() and target.stat().st_size == head['size']):
                print(f'{local_id}/{o["name"]}: {head["size"] / 1e6:.0f} MB', flush=True)
                fetch(o['url'], target, head['size'])
            digest = _sha256(target)
            verified = 'yes' if head['sha256'] and digest == head['sha256'] else 'no S3 checksum' \
                if not head['sha256'] else 'MISMATCH'
            print(f'  {o["name"]} sha256 {verified}', flush=True)
            done[(local_id, o['name'])] = {
                'nara_local_id': local_id, 'file': o['name'], 'designator': o['designator'], 'size': head['size'],
                'sha256': digest, 's3_sha256': head['sha256'], 'verified': verified,
                'path': str(target).replace('\\', '/'), 'url': o['url'], 'fetched': today()}
            write_tsv(manifest, DOWNLOAD_FIELDS, sorted(done.values(), key=lambda r: (r['nara_local_id'], r['file'])))
    return list(done.values())
