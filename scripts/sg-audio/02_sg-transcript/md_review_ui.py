"""Local review UI for the transcript queues, with the SG-reel audio of each row.

Run with ``uv run python scripts/run.py sg_transcript.py review-ui`` and open the printed URL.
It serves on 127.0.0.1 only.

Queues (``.local/mission_data/review``):

* ``wording``   ``wording.tsv``: printed, AFJ and ALSJ readings. Choice printed, afj or alsj.
* ``editorial`` ``editorial_queue.tsv``: journal editorial spans. Class and action.
* ``pao``       ``pao_words.tsv``: PAO-edition OCR slots. Choice tl, ia or oxr.

A save writes the row to the queue file and records the decisions in ``MISSION_DATA/review``
with the stage's own recorder (``record_wording_choices``, ``record_queue_decisions``,
``record_choices``). Later pipeline builds reuse them.

Audio placement. The audio is the SG reel at the row's GET:

* ``anchor``  the event's own timing anchor (``sg/sg_anchors.tsv``, ok or flagged)
* ``nearby``  the offset of the nearest usable anchor on a reel within ``NEAR_S`` of the GET.
  The reels are edited, so an offset jump between the two neighbouring anchors is reported.
* ``pao_row`` a PAO-edition row placed on its reel (``sg/sg_pao_rows.tsv``). A slot inside a
  long row is placed in proportion to its word position, so it is approximate.

Onboard (CM/LM data-storage) recordings are not on the SG reels: such rows get no excerpt and
no ASR words.
The Whisper words shown in the window are only a locator aid. They are never a wording source.
"""
from __future__ import annotations

import bisect
import io
import json
import re
import threading
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from md_common import LOCAL, MISSION_DATA, SCRAPED, WORK, read_tsv, today, write_tsv

HTML = Path(__file__).with_name('review_ui.html')
NEAR_S = 600          # search for a reel anchor this far from the GET
EDIT_JUMP_S = 5.0     # neighbouring-anchor offsets differing by more than this straddle a reel edit
MAX_CLIP_S = 180
USABLE = {'ok', 'flagged'}

QUEUES = {
    'wording': {'file': 'wording.tsv', 'key': 'wording_key', 'choices': ['printed', 'afj', 'alsj'],
                'pending': lambda r: r['status'] == 'review' and not r['choice']},
    'editorial': {'file': 'editorial_queue.tsv', 'key': 'span_key',
                  'choices': ['remove', 'garble', 'annotate', 'to_commentary', 'keep'],
                  'pending': lambda r: not (r['action'] and r['reviewer'])},
    'pao': {'file': 'pao_words.tsv', 'key': 'slot_key', 'choices': ['tl', 'ia', 'oxr'],
            'pending': lambda r: not (r['choice'] and r['reviewer'])},
}


def queue_fields(path: Path) -> list[str]:
    with path.open(encoding='utf-8') as handle:
        return handle.readline().rstrip('\r\n').split('\t')


class Locator:
    """Places a GET, an event or a PAO slot on an SG reel."""

    def __init__(self):
        from md_sg import SG_STORE, reel_identity
        self.reels = {r['reel']: r for r in read_tsv(MISSION_DATA / 'sg_reels.tsv')}
        self.events = {e['event_id']: e for e in read_tsv(SCRAPED / 'utterance_events.tsv')}
        self.by_block = {}
        for e in self.events.values():
            for field in ('afj_key', 'alsj_key'):
                if e[field]:
                    self.by_block.setdefault(e[field], e['event_id'])
        self.direct, self.track = {}, defaultdict(list)
        for a in read_tsv(LOCAL / 'sg' / 'sg_anchors.tsv'):
            if a['status'] not in USABLE:
                continue
            self.direct.setdefault(a['event_id'], a)
            self.track[a['reel']].append((float(a['get_seconds']), float(a['offset_s'])))
        for points in self.track.values():
            points.sort()
        self.pao_rows = defaultdict(list)
        for r in read_tsv(LOCAL / 'sg' / 'sg_pao_rows.tsv'):
            self.pao_rows[r['event_id']].append(r)
        self._identity = {}
        self._asr = {}
        self._lock = threading.Lock()
        self._store, self._reel_identity = SG_STORE, reel_identity

    # -------------------------------------------------------------- reels
    def identity(self, reel: str) -> Path:
        if reel not in self._identity:
            settings = self._store / self.reels[reel]['asr_settings_id']
            self._identity[reel] = self._reel_identity(settings, reel)
        return self._identity[reel]

    def wav(self, reel: str) -> Path:
        source = json.loads((self.identity(reel) / 'source.json').read_text(encoding='utf-8'))['source']
        return Path(source['path'])

    def asr_words(self, reel: str, start: float, end: float) -> list[dict]:
        from md_sg import load_asr
        with self._lock:
            if reel not in self._asr:
                self._asr[reel] = load_asr(self.identity(reel))
        out = []
        for seg in self._asr[reel]:
            if seg['e'] >= start and seg['s'] <= end:
                out.append({'s': round(seg['s'], 2), 'e': round(seg['e'], 2), 'text': seg['text'],
                            'flags': seg['flags']})
        return out

    def find_words(self, reel: str, start: float, end: float, text: str, run: int = 3):
        """Start time of the longest run (at least ``run``) of the text's words in the reel words."""
        from md_common import tokens
        from md_reconcile import comparable
        want = comparable(tokens(text))
        if len(want) < run:
            return None
        self.asr_words(reel, 0, 0)  # loads and caches the reel
        words = [w for seg in self._asr[reel] if seg['e'] >= start - 30 and seg['s'] <= end + 30
                 for w in seg['words']]
        toks = [w['tok'] for w in words]
        best = (0, None)
        for k in range(len(want) - run + 1):
            gram = want[k:k + run]
            for i in range(len(toks) - run + 1):
                if toks[i:i + run] == gram:
                    n = run
                    while k + n < len(want) and i + n < len(toks) and toks[i + n] == want[k + n]:
                        n += 1
                    if n > best[0]:
                        best = (n, words[i]['s'])
        return best[1]

    # ------------------------------------------------------------ placing
    def event_for(self, queue: str, row: dict) -> str:
        if queue == 'editorial':
            block = row['span_key'].split('#')[0]
            return self.by_block.get(block, '')
        return row.get('event_id', '')

    def by_get(self, t: float) -> list[dict]:
        found = []
        for reel, points in self.track.items():
            i = bisect.bisect_left(points, (t, -1e18))
            before = points[i - 1] if i > 0 else None
            after = points[i] if i < len(points) else None
            near = [p for p in (before, after) if p and abs(p[0] - t) <= NEAR_S]
            if not near:
                continue
            best = min(near, key=lambda p: abs(p[0] - t))
            start = t - best[1]
            duration = float(self.reels[reel]['duration_s'])
            if not 0 <= start <= duration:
                continue
            note = ''
            if before and after and abs(before[1] - after[1]) > EDIT_JUMP_S:
                note = f'reel edit between anchors ({after[1] - before[1]:+.0f} s offset jump)'
            found.append({'reel': reel, 'start_s': round(start, 2), 'basis': 'nearby',
                          'anchor_distance_s': round(abs(best[0] - t), 1), 'note': note})
        return sorted(found, key=lambda c: c['anchor_distance_s'])[:4]

    def place(self, queue: str, row: dict) -> dict:
        event_id = self.event_for(queue, row)
        event = self.events.get(event_id)
        candidates, notes = [], []
        if event and event['type'] == 'O':
            # Onboard (CM/LM data-storage) speech is not on the SG reels, and the reel words at the
            # same GET are other speech, so no excerpt or ASR is offered.
            notes.append('Onboard recorder (CM/LM data storage): not on the SG reels. No audio excerpt.')
            return {'event_id': event_id, 'get_seconds': float(event['get_seconds']) if event['get_seconds'] else None,
                    'type': 'O', 'candidates': [], 'notes': notes}
        if queue == 'pao':
            m = re.match(r'.*?:(\d+):[0-9a-f]+$', row['slot_key'])
            i0 = int(m.group(1)) if m else 0
            for r in self.pao_rows.get(row['event_id'], []):
                s, e, n = float(r['start_s']), float(r['end_s']), max(1, int(r['tokens'] or 1))
                found = self.find_words(r['reel'], s, e, row['context'])
                if found is not None:
                    candidates.append({'reel': r['reel'], 'start_s': round(found, 2), 'basis': 'pao_context',
                                       'anchor_distance_s': 0, 'note': 'slot context found in the reel words'})
                else:
                    candidates.append({'reel': r['reel'], 'start_s': round(s + (e - s) * min(1.0, i0 / n), 2),
                                       'basis': 'pao_row', 'anchor_distance_s': 0,
                                       'note': f'approximate: word {i0} of {n} in a {e - s:.0f} s row'})
        anchor = self.direct.get(event_id)
        if anchor:
            candidates.append({'reel': anchor['reel'], 'start_s': float(anchor['source_s']), 'basis': 'anchor',
                               'anchor_distance_s': 0, 'note': f'anchor {anchor["anchor_id"]} ({anchor["status"]})'})
        t = float(event['get_seconds']) if event and event['get_seconds'] not in ('', None) else None
        if t is None and row.get('get'):
            t = get_seconds(row['get'])
        if t is not None:
            seen = {c['reel'] for c in candidates}
            candidates += [c for c in self.by_get(t) if c['reel'] not in seen]
        if not candidates:
            notes.append('No SG reel covers this GET.')
        return {'event_id': event_id, 'get_seconds': t, 'type': event['type'] if event else '',
                'candidates': candidates, 'notes': notes}


def get_seconds(text: str):
    m = re.fullmatch(r'(-?)(\d+):(\d\d):(\d\d)', (text or '').strip())
    if not m:
        return None
    value = int(m.group(2)) * 3600 + int(m.group(3)) * 60 + int(m.group(4))
    return -value if m.group(1) else value


def clip_wav(path: Path, start: float, duration: float) -> bytes:
    import numpy as np
    import soundfile as sf
    with sf.SoundFile(path) as f:
        rate = f.samplerate
        first = max(0, int(start * rate))
        f.seek(min(first, f.frames))
        data = f.read(int(min(duration, MAX_CLIP_S) * rate), dtype='float32', always_2d=True)
    mono = data.mean(axis=1) if data.size else np.zeros(1, dtype='float32')
    out = io.BytesIO()
    sf.write(out, mono, rate, format='WAV', subtype='PCM_16')
    return out.getvalue()


class Store:
    """The queue files, and saving decisions through each stage's recorder."""

    def __init__(self):
        self.lock = threading.Lock()

    def rows(self, queue: str) -> tuple[list[str], list[dict]]:
        path = WORK / QUEUES[queue]['file']
        return queue_fields(path), read_tsv(path)

    def save(self, queue: str, key: str, values: dict) -> dict:
        spec = QUEUES[queue]
        with self.lock:
            fields, rows = self.rows(queue)
            match = [r for r in rows if r[spec['key']] == key]
            if len(match) != 1:
                raise KeyError(f'{queue}: no unique row {key}')
            row = match[0]
            choice = values.get('choice', '')
            if choice not in spec['choices']:
                raise ValueError(f'{queue}: choice must be one of {spec["choices"]}')
            if not values.get('reviewer'):
                raise ValueError('reviewer is required')
            if queue == 'editorial':
                row['action'], row['class'] = choice, values.get('class', '') or row['class']
            else:
                row['choice'] = choice
            row['reviewer'] = values['reviewer']
            row['note'] = values.get('note', '')
            if 'date' in fields:
                row['date'] = today()
            if queue == 'wording':
                row['status'] = 'human'
            write_tsv(WORK / spec['file'], fields, rows)
            recorded = record(queue)
        return {'row': row, 'recorded': recorded}


def record(queue: str) -> int:
    if queue == 'wording':
        from md_reconcile import record_wording_choices
        return record_wording_choices()
    if queue == 'editorial':
        from md_spans import record_queue_decisions
        return record_queue_decisions()
    from md_pao import record_choices
    return record_choices()


def make_handler(locator: Locator, store: Store):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # quiet
            pass

        def send(self, status: int, body: bytes, kind: str):
            self.send_response(status)
            self.send_header('Content-Type', kind)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(body)

        def json(self, value, status=200):
            self.send(status, json.dumps(value).encode('utf-8'), 'application/json; charset=utf-8')

        def do_GET(self):
            url = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(url.query).items()}
            try:
                if url.path == '/':
                    return self.send(200, HTML.read_bytes(), 'text/html; charset=utf-8')
                if url.path == '/api/queues':
                    out = {}
                    for name, spec in QUEUES.items():
                        _, rows = store.rows(name)
                        out[name] = {'rows': len(rows), 'pending': sum(1 for r in rows if spec['pending'](r)),
                                     'choices': spec['choices'], 'key': spec['key']}
                    return self.json(out)
                if url.path == '/api/rows':
                    name = q['queue']
                    _, rows = store.rows(name)
                    for r in rows:
                        r['_pending'] = QUEUES[name]['pending'](r)
                    return self.json(rows)
                if url.path == '/api/place':
                    name = q['queue']
                    _, rows = store.rows(name)
                    row = next(r for r in rows if r[QUEUES[name]['key']] == q['key'])
                    return self.json(locator.place(name, row))
                if url.path == '/api/audio':
                    body = clip_wav(locator.wav(q['reel']), float(q['start']), float(q['dur']))
                    return self.send(200, body, 'audio/wav')
                if url.path == '/api/asr':
                    return self.json(locator.asr_words(q['reel'], float(q['start']), float(q['end'])))
                return self.send(404, b'not found', 'text/plain')
            except Exception as exc:  # report to the page
                return self.json({'error': f'{type(exc).__name__}: {exc}'}, 500)

        def do_POST(self):
            url = urlparse(self.path)
            try:
                if url.path != '/api/save':
                    return self.send(404, b'not found', 'text/plain')
                body = json.loads(self.rfile.read(int(self.headers.get('Content-Length') or 0)) or b'{}')
                return self.json(store.save(body['queue'], body['key'], body))
            except Exception as exc:
                return self.json({'error': f'{type(exc).__name__}: {exc}'}, 400)

    return Handler


def main(args) -> int:
    locator, store = Locator(), Store()
    server = ThreadingHTTPServer(('127.0.0.1', args.port), make_handler(locator, store))
    print(f'review UI: http://127.0.0.1:{args.port}/  (Ctrl+C to stop)')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
