"""Space-to-ground reels: catalogue, PAO separation and timing anchors.

docs/16_2026-11-23_mission_data_ingestion.md, "Space-to-ground reels", steps 1, 3 and 4.
The journals and the 1971 transcripts are the timing authority. The stage-06a-style ASR of
each reel (written by scripts/sg-audio/sg_transcribe.py) only locates their utterances on the
reel clock. Retiming the audio from these anchors is later work in scripts/sg-audio/.

Steps (``sg_transcript.py sg <step>``):

* ``reels``   ``MISSION_DATA/sg_reels.tsv`` (committed): file, checksum, duration, the NASA
  audio-index row (PAO segment range, MET) and the GET span bounded by the PAO edition's
  segment headers.
* ``pao``     ``sg/sg_pao.tsv``: reel intervals whose ASR matches a PAO announcement. They are
  never used as anchors.
* ``anchors`` ``sg/sg_offsets.tsv`` (coarse reel-to-GET offset by distinctive phrase votes),
  ``sg/sg_anchors.tsv`` (one row per utterance onset matched to a TEC or journal utterance) and
  ``sg/sg_summary.tsv`` (per reel).

Onsets are Whisper word starts. The printed and journal GETs are whole seconds, so their
quantization (±0.5 s) dominates the word-time error.
"""
from __future__ import annotations

import bisect
import difflib
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from statistics import median

from md_common import LOCAL, MISSION_DATA, RAW_MANIFEST, SCRAPED, format_get, read_tsv, sha256_file, today, \
    tokens, write_tsv
from md_reconcile import comparable, load_journal, load_printed  # noqa: F401
from transcript_read import identity_rows

SG_STORE = Path('F:/Apollo_15_data/processing/sg/timing_transcripts')
SG_INDEX_PDF = Path('N:/Projects/Apollo/Mission Data/NASA-Audio-Archive_Digital-Audio-File_Metadata.pdf')
OUT = LOCAL / 'sg'
REELS_TSV = MISSION_DATA / 'sg_reels.tsv'

REEL_FIELDS = ('reel', 'file', 'sha256', 'sample_rate', 'channels', 'frames', 'duration_s', 'index_date',
               'index_description', 'segment_first', 'segment_last', 'index_met_start', 'index_met_end',
               'pao_get_start', 'pao_get_end', 'span_note', 'asr_settings_id', 'asr_identity_id')
PAO_FIELDS = ('reel', 'start_s', 'end_s', 'pao_event_id', 'pao_segment', 'header_get', 'coverage', 'method',
              'asr_text')
OFFSET_FIELDS = ('reel', 'source_s', 'offset_s', 'votes', 'share', 'rank')
ANCHOR_FIELDS = ('anchor_id', 'reel', 'source_s', 'source_end_s', 'event_id', 'get', 'get_seconds', 'get_basis',
                 'printed_get', 'journal_get', 'get_choice', 'who', 'offset_s', 'local_offset_s', 'residual_s',
                 'status', 'run', 'event_tokens', 'coverage', 'word_p', 'flags', 'asr_text', 'event_text')
SUMMARY_FIELDS = ('reel', 'duration_s', 'pao_get_start', 'pao_get_end', 'asr_words', 'pao_intervals', 'pao_s',
                  'offset_points', 'offset_first', 'offset_last', 'anchors', 'ok', 'flagged', 'rejected',
                  'outliers', 'isolated',
                  'covered_s', 'note')

# Matching parameters.
NGRAM = 4                 # coarse votes use distinctive word 4-grams
RARE = 2                  # a 4-gram is distinctive if it occurs in at most this many events in the span
SPAN_MARGIN = 2 * 3600    # events searched beyond the PAO-header span of a reel
OFFSET_STEP = 60          # coarse-offset grid spacing (reel seconds)
OFFSET_HALF = 600         # votes within this many reel seconds of a grid point
OFFSET_BIN = 4            # vote histogram bin (s)
OFFSET_MIN_VOTES = 4
FINE_WINDOW = 45          # events searched this far beyond the predicted GET of an ASR segment
MIN_RUN = 3               # matched tokens from an utterance's first word
LOCAL_HALF = 1800         # neighbours searched this far on the reel clock
LOCAL_EACH = 4            # nearest clean neighbours used on each side (a local Theil-Sen line)
OK_RESIDUAL = 3.0         # |offset - local median| for an anchor to be consistent
PAO_MIN_TOKENS = 5
PAO_COVERAGE = 0.5
BAD_SEGMENT_FLAGS = {'collapsed_timing', 'high_compression', 'no_word_times', 'align_failed'}
# Word or segment timing that cannot place an onset (the timing plan's "no collapsed or uncertain words").
# Measured on all reels (2026-11-24): a word whose segment has collapsed timing is a median 13 s off
# its neighbours, and a long word spanning a VAD gap 6 s. A zero-duration, clipped or low-probability
# word alone keeps a good start (93-100 % within 3 s, as unflagged words); those are soft flags.
HARD_FLAGS = BAD_SEGMENT_FLAGS | {'invalid_time', 'long', 'outside_context'}
PAO_PHRASES = (('this', 'is', 'apollo', 'control'), ('this', 'is', 'kennedy', 'launch', 'control'),
               ('apollo', 'control', 'houston'))


# ------------------------------------------------------------------- ASR store
def settings_dirs() -> list[Path]:
    return sorted(p for p in SG_STORE.glob('asr-*') if (p / 'settings.json').is_file())


def reel_identity(settings: Path, reel: str) -> Path | None:
    """The transcript of the reel's current WAV.

    A reel that was replaced (553, 581 and 582-AAA were reversed in the archive and resaved
    the right way round) keeps its old transcript under another identity. The current one is
    the identity whose recorded size and modification time match the file on disk.
    """
    base = settings / 'sources' / 'sg' / reel
    found = sorted(p for p in base.iterdir() if (p / 'source.json').is_file()) if base.is_dir() else []
    if len(found) <= 1:
        return found[0] if found else None
    current = []
    for p in found:
        source = json.loads((p / 'source.json').read_text(encoding='utf-8'))['source']
        wav = Path(source['path'])
        if wav.is_file() and wav.stat().st_size == source['size'] and wav.stat().st_mtime_ns == source['mtime_ns']:
            current.append(p)
    if len(current) != 1:
        raise RuntimeError(f'{reel}: {len(found)} transcript identities under {base} and {len(current)} match the WAV')
    return current[0]


def load_asr(identity: Path) -> list[dict]:
    """Owned segments of every completed chunk, in reel order, with comparable tokens per word.

    Word times are the CTC-aligned ones where the chunk is aligned (scripts/00_common/
    transcript_read.py), Whisper's otherwise.
    """
    rate = json.loads((identity / 'source.json').read_text(encoding='utf-8'))['identity']['sample_rate']
    try:
        segments = identity_rows(identity)
    except RuntimeError as error:
        raise RuntimeError(f'{error}; finish sg_transcribe.py first') from error
    out = []
    for i, seg in enumerate(segments):
        words = []
        for w in seg['words']:
            for tok in comparable(tokens(w['w'])):
                words.append({'tok': tok, 's': w['sf'] / rate, 'e': w['ef'] / rate, 'p': w['p'],
                              'flags': w['flags'], 'text': w['w']})
        out.append({'index': i, 's': seg['sf'] / rate, 'e': seg['ef'] / rate, 'text': seg['text'].strip(),
                    'flags': seg['flags'], 'words': words})
    return out


# ------------------------------------------------------------- step 1: reels
def parse_index_pdf(path: Path = SG_INDEX_PDF) -> dict[str, dict]:
    """Apollo 15 rows of the NASA audio-archive metadata (PDF pages 21-22), by file stem."""
    import pymupdf
    columns = {'met_start': (620, 660), 'met_end': (660, 700)}
    out = {}
    with pymupdf.open(path) as doc:
        for page in doc:
            words = page.get_text('words')
            heads = sorted((w[1], w[4]) for w in words if w[0] < 60 and re.fullmatch(r'\d{3}-AA[A-Z]', w[4]))
            if not heads:
                continue
            ys = [y for y, _ in heads]
            rows = {name: [] for _, name in heads}
            for x0, y0, _x1, _y1, text, *_ in words:
                i = bisect.bisect_right(ys, y0 + 2) - 1  # the filename row at or above this word
                if i >= 0:
                    rows[heads[i][1]].append((x0, text))
            for name, cells in rows.items():
                cells.sort()
                desc = ' '.join(t for x, t in cells if 180 <= x < 540)
                if 'A/15' not in [t for x, t in cells if 140 <= x < 180]:
                    continue
                yr, mo, da = ([t for x, t in cells if lo <= x < hi] for lo, hi in ((85, 105), (105, 122), (122, 140)))
                row = {'index_date': f'19{yr[0]}-{mo[0]}-{da[0]}' if yr and mo and da else '',
                       'index_description': desc}
                for key, (lo, hi) in columns.items():
                    row[key] = ' '.join(t for x, t in cells if lo <= x < hi)
                m = re.search(r'Commentary (\d+)-(\d+|EOM)', desc)
                row['segment_first'] = int(m.group(1)) if m else ''
                row['segment_last'] = (int(m.group(2)) if m.group(2).isdigit() else 'EOM') if m else ''
                out[name] = row
    return out


def pao_segment(text: str) -> int | None:
    m = re.fullmatch(r'(?:MC)?\s*(\d+)\s*/\s*\d+', (text or '').strip())
    return int(m.group(1)) if m else None


def pao_segment_gets() -> dict[int, int]:
    """First header GET of each PAO-edition segment, keeping only the longest GET-increasing chain."""
    first: dict[int, int] = {}
    for row in read_tsv(SCRAPED / 'transcript_pao.tsv'):
        seg = pao_segment(row['transcript_tape_ocr'])
        if seg is None or row['get_seconds'] in ('', None) or seg in first:
            continue
        first[seg] = int(row['get_seconds'])
    items = sorted(first.items())
    # Longest non-decreasing subsequence of GET in segment order (drops OCR-misread headers).
    tails, tail_idx, parent = [], [], [-1] * len(items)
    for i, (_, get) in enumerate(items):
        j = bisect.bisect_right(tails, get)
        if j == len(tails):
            tails.append(get)
            tail_idx.append(i)
        else:
            tails[j], tail_idx[j] = get, i
        parent[i] = tail_idx[j - 1] if j else -1
    keep, i = [], tail_idx[-1] if tail_idx else -1
    while i >= 0:
        keep.append(items[i])
        i = parent[i]
    return dict(reversed(keep))


def build_reels() -> list[dict]:
    index = parse_index_pdf()
    headers = pao_segment_gets()
    header_segs = sorted(headers)
    settings = settings_dirs()
    if not settings:
        raise RuntimeError(f'No SG transcript under {SG_STORE}')
    settings = settings[-1]
    rows = []
    reels = sorted(p.name for p in (settings / 'sources' / 'sg').iterdir() if p.is_dir())
    for reel in reels:
        identity = reel_identity(settings, reel)
        record = json.loads((identity / 'source.json').read_text(encoding='utf-8'))
        ident, source = record['identity'], record['source']
        meta = index.get(reel, {})
        row = {'reel': reel, 'file': ident['relpath'], 'sha256': ident['content_sha256'],
               'sample_rate': ident['sample_rate'], 'channels': ident['source_channels'], 'frames': ident['frames'],
               'duration_s': round(ident['frames'] / ident['sample_rate'], 3),
               'index_date': meta.get('index_date', ''), 'index_description': meta.get('index_description', ''),
               'segment_first': meta.get('segment_first', ''), 'segment_last': meta.get('segment_last', ''),
               'index_met_start': meta.get('met_start', ''), 'index_met_end': meta.get('met_end', ''),
               'asr_settings_id': settings.name, 'asr_identity_id': identity.name, 'span_note': ''}
        first, last = row['segment_first'], row['segment_last']
        if isinstance(first, int):
            last_n = last if isinstance(last, int) else first
            starts = [s for s in header_segs if first <= s <= last_n]
            after = [s for s in header_segs if s > last_n]
            notes = []
            if starts:
                row['pao_get_start'] = format_get(headers[starts[0]])
                if starts[0] != first:
                    notes.append(f'first header is segment {starts[0]}')
            if after:
                row['pao_get_end'] = format_get(headers[after[0]])
                if after[0] != last_n + 1:
                    notes.append(f'end from segment {after[0]}')
            elif starts:
                row['pao_get_end'] = format_get(headers[starts[-1]] + row['duration_s'])
                notes.append('end = last header + duration')
            row['span_note'] = '; '.join(notes)
        rows.append(row)
    write_tsv(REELS_TSV, REEL_FIELDS, rows)
    record_index_pdf()
    return rows


def record_index_pdf() -> None:
    """The index PDF lives outside the raw folder; record it by its external path."""
    from md_snapshot import RAW_FIELDS, load_raw_manifest, pdf_pages
    rows = load_raw_manifest()
    key = str(SG_INDEX_PDF).replace('\\', '/')
    size = SG_INDEX_PDF.stat().st_size
    old = rows.get(key)
    if old and old.get('bytes') == str(size) and old.get('sha256'):
        return
    rows[key] = {'path': key, 'url': '', 'final_url': '', 'fetch_date': today(), 'bytes': size,
                 'sha256': sha256_file(SG_INDEX_PDF), 'count': pdf_pages(SG_INDEX_PDF), 'kind': 'sg_index',
                 'note': 'external path; NASA audio-archive metadata, Apollo 15 SG reels on PDF pages 21-22'}
    write_tsv(RAW_MANIFEST, RAW_FIELDS, [rows[k] for k in sorted(rows)])


def load_reels() -> list[dict]:
    rows = read_tsv(REELS_TSV)
    if not rows:
        raise RuntimeError(f'{REELS_TSV} is missing; run `sg_transcript.py sg reels` first')
    return rows


def reel_span(reel: dict) -> tuple[int, int] | None:
    from md_common import parse_get
    lo, hi = parse_get(reel['pao_get_start']), parse_get(reel['pao_get_end'])
    return (lo, hi) if lo is not None and hi is not None else None


# ------------------------------------------------------------------ events
def load_events() -> list[dict]:
    """Air-to-ground utterances (TEC rows and journal-only rows) with the timing choice recorded."""
    journal = {}
    for source in ('afj', 'alsj'):
        rows, _ = load_journal(source)
        journal.update({b['block_key']: b for b in rows})
    printed = {r['event_id']: r for r in load_printed('tec')}
    out = []
    for ev in read_tsv(SCRAPED / 'utterance_events.tsv'):
        if ev['type'] != 'T':
            continue
        key = ev['afj_key'] or ev['alsj_key']
        block = journal.get(key) if key else None
        prow = printed.get(ev['event_id'])
        # Where the journal breaks this utterance differently (a group), match the printed row's own words.
        words = block if block and not (ev.get('group_id') and prow) else None
        toks = comparable(words['tokens']) if words and words['tokens'] else (comparable(prow['tokens']) if prow else [])
        if not toks:
            continue
        journal_get = block['get'] if block and block['get_basis'] in ('anchor', 'stamp') else ''
        out.append({'event_id': ev['event_id'], 't': int(ev['get_seconds']), 'get': ev['get'],
                    'get_basis': ev['get_basis'], 'printed_get': ev['printed_get'], 'journal_get': journal_get,
                    'journal_basis': block['get_basis'] if block else '', 'who': ev['who'], 'tokens': toks,
                    'text': (words['text'] if words else ev['printed_text'])})
    out.sort(key=lambda e: (e['t'], e['event_id']))
    return out


def get_choice(ev: dict) -> str:
    if ev['get_basis'] == 'journal':
        return 'journal' if ev['printed_get'] in ('', ev['journal_get']) else 'journal_over_printed'
    if ev['get_basis'] == 'printed':
        return 'printed'
    return f'journal_{ev["get_basis"]}'  # journal-only row: anchor, stamp or inherited


# --------------------------------------------------------- coarse offsets
def coarse_offsets(reel: str, segments: list[dict], events: list[dict]) -> list[dict]:
    """Votes (reel time, GET - reel time) from distinctive 4-grams, then a local mode on a grid."""
    grams: dict[tuple, set] = defaultdict(set)
    for k, ev in enumerate(events):
        t = ev['tokens']
        for i in range(len(t) - NGRAM + 1):
            grams[tuple(t[i:i + NGRAM])].add(k)
    rare = {g: ks for g, ks in grams.items() if len(ks) <= RARE}
    votes = []
    for seg in segments:
        toks = [w['tok'] for w in seg['words']]
        for i in range(len(toks) - NGRAM + 1):
            for k in rare.get(tuple(toks[i:i + NGRAM]), ()):
                votes.append((seg['words'][i]['s'], events[k]['t'] - seg['words'][i]['s']))
    votes.sort()
    times = [v[0] for v in votes]
    rows = []
    end = segments[-1]['e'] if segments else 0
    t = 0.0
    while t <= end + OFFSET_STEP:
        lo, hi = bisect.bisect_left(times, t - OFFSET_HALF), bisect.bisect_right(times, t + OFFSET_HALF)
        local = [o for _, o in votes[lo:hi]]
        if len(local) >= OFFSET_MIN_VOTES:
            hist = Counter(round(o / OFFSET_BIN) for o in local)
            peaks = []
            for b in sorted(hist, key=lambda b: (-(hist[b - 1] + hist[b] + hist[b + 1]), b)):
                if any(abs(b - p) <= 3 for p in peaks):
                    continue
                peaks.append(b)
                if len(peaks) == 2:
                    break
            for rank, b in enumerate(peaks, 1):
                near = [o for o in local if abs(o / OFFSET_BIN - b) <= 1.5]
                if len(near) >= OFFSET_MIN_VOTES and len(near) / len(local) >= (0.25 if rank == 1 else 0.15):
                    rows.append({'reel': reel, 'source_s': t, 'offset_s': round(median(near), 2), 'votes': len(near),
                                 'share': round(len(near) / len(local), 3), 'rank': rank})
        t += OFFSET_STEP
    return rows


def offsets_at(grid: list[dict], t: float) -> list[float]:
    times = sorted({g['source_s'] for g in grid})
    if not times:
        return []
    i = min(range(len(times)), key=lambda j: abs(times[j] - t))
    if abs(times[i] - t) > OFFSET_STEP * 5:
        return []
    return [g['offset_s'] for g in grid if g['source_s'] == times[i]]


# ------------------------------------------------------------ PAO separation
def load_pao_announcements(rows: list[dict]) -> list[dict]:
    """PAO-speaker rows of the PAO edition, with the corrected wording (md_pao)."""
    out = []
    for r in rows:
        if r['speaker'] != 'PAO' or r['t'] is None:
            continue
        ctokens = comparable(r['ctokens'])
        out.append({'event_id': r['event_id'], 't': r['t'], 'segment': r['segment'], 'ctokens': ctokens,
                    'grams': {tuple(ctokens[i:i + 3]) for i in range(len(ctokens) - 2)}})
    return out


def find_pao(reel: dict, segments: list[dict], announcements: list[dict]) -> list[dict]:
    first, last = reel['segment_first'], reel['segment_last']
    if first.isdigit():
        lo, hi = int(first) - 1, (int(last) if last.isdigit() else 10 ** 6) + 1
        cands = [a for a in announcements if a['segment'] is not None and lo <= a['segment'] <= hi]
    else:
        cands = []
    span = reel_span(reel)
    if span:  # announcements whose segment number was misread, by header GET
        ids = {a['event_id'] for a in cands}
        cands += [a for a in announcements if a['segment'] is None and span[0] - 600 <= a['t'] <= span[1] + 600
                  and a['event_id'] not in ids]
    marks = []
    for seg in segments:
        toks = [w['tok'] for w in seg['words']]
        if not toks:
            continue
        best = (0.0, None)
        if len(toks) >= PAO_MIN_TOKENS:
            grams = {tuple(toks[i:i + 3]) for i in range(len(toks) - 2)}
            for a in cands:
                if len(grams & a['grams']) < 2:
                    continue
                sm = difflib.SequenceMatcher(None, toks, a['ctokens'], autojunk=False)
                matched = sum(b.size for b in sm.get_matching_blocks() if b.size >= 3)
                cov = matched / len(toks)
                if cov > best[0]:
                    best = (cov, a)
        if best[0] >= PAO_COVERAGE:
            marks.append((seg, best[1], best[0], 'pao_text'))
        elif any(tuple(toks[:len(p)]) == p for p in PAO_PHRASES):
            marks.append((seg, best[1] if best[0] >= 0.2 else None, best[0], 'pao_phrase'))
    rows = []
    for seg, a, cov, method in marks:
        rows.append({'reel': reel['reel'], 'start_s': round(seg['s'], 3), 'end_s': round(seg['e'], 3),
                     'pao_event_id': a['event_id'] if a else '', 'pao_segment': a['segment'] if a else '',
                     'header_get': format_get(a['t']) if a else '', 'coverage': round(cov, 3), 'method': method,
                     'asr_text': seg['text'][:200], '_seg': seg['index']})
    # Merge neighbouring PAO segments of the same announcement.
    merged = []
    for r in rows:
        if merged and merged[-1]['pao_event_id'] == r['pao_event_id'] and r['start_s'] - merged[-1]['end_s'] <= 5:
            m = merged[-1]
            m['end_s'], m['coverage'] = r['end_s'], max(m['coverage'], r['coverage'])
            m['_segs'].append(r['_seg'])
            m['asr_text'] = (m['asr_text'] + ' ' + r['asr_text'])[:200]
            if r['method'] == 'pao_text':
                m['method'] = 'pao_text'
        else:
            merged.append({**r, '_segs': [r['_seg']]})
    return merged


# --------------------------------------------------------------- anchors
def anchors_for_segment(seg: dict, cands: list[dict]) -> list[dict]:
    """Utterances whose first MIN_RUN tokens are matched, in order, in this ASR segment."""
    toks = [w['tok'] for w in seg['words']]
    stream, owner, starts = [], [], {}
    for k, ev in enumerate(cands):
        starts[len(stream)] = k
        stream.extend(ev['tokens'])
        owner.extend([k] * len(ev['tokens']))
    if not toks or not stream:
        return []
    sm = difflib.SequenceMatcher(None, toks, stream, autojunk=False)
    blocks = [b for b in sm.get_matching_blocks() if b.size]
    found = []
    for b in blocks:
        for pos, k in starts.items():
            if not (b.b <= pos < b.b + b.size):
                continue
            ev = cands[k]
            run = min(b.b + b.size - pos, len(ev['tokens']))
            whole = run == len(ev['tokens'])
            if run < MIN_RUN and not (whole and run >= 2):
                continue
            i = b.a + (pos - b.b)
            # Coverage: matched share of this utterance's tokens within the segment.
            matched = sum(max(0, min(bb.b + bb.size, pos + len(ev['tokens'])) - max(bb.b, pos)) for bb in blocks)
            found.append((i, ev, run, matched / len(ev['tokens'])))
    return found


def match_reel(reel: dict, segments: list[dict], events: list[dict], pao_segs: set) -> tuple[list, list]:
    span = reel_span(reel)
    if span:
        lo, hi = span[0] - SPAN_MARGIN, span[1] + SPAN_MARGIN
        pool = [e for e in events if lo <= e['t'] <= hi]
    else:
        pool = events
    usable = [s for s in segments if s['index'] not in pao_segs]
    grid = coarse_offsets(reel['reel'], usable, pool)
    times = [e['t'] for e in pool]
    anchors = []
    for seg in usable:
        offs = offsets_at(grid, seg['s'])
        if not offs or not seg['words']:
            continue
        seen = set()
        for off in offs:
            a = bisect.bisect_left(times, seg['s'] + off - FINE_WINDOW)
            b = bisect.bisect_right(times, seg['e'] + off + FINE_WINDOW)
            for i, ev, run, cov in anchors_for_segment(seg, pool[a:b]):
                if ev['event_id'] in seen:
                    continue
                seen.add(ev['event_id'])
                w = seg['words'][i]
                flags = sorted(set(w['flags']) | (set(seg['flags']) & BAD_SEGMENT_FLAGS))
                if ev['get_basis'] == 'inherited':
                    flags.append('inherited_get')
                if w['p'] < 0.5:
                    flags.append('low_word_p')
                anchors.append({'reel': reel['reel'], 'source_s': round(w['s'], 3), 'source_end_s': round(w['e'], 3),
                                'event_id': ev['event_id'], 'get': ev['get'], 'get_seconds': ev['t'],
                                'get_basis': ev['get_basis'], 'printed_get': ev['printed_get'],
                                'journal_get': ev['journal_get'], 'get_choice': get_choice(ev), 'who': ev['who'],
                                'offset_s': round(ev['t'] - w['s'], 3), 'run': run, 'event_tokens': len(ev['tokens']),
                                'coverage': round(cov, 3), 'word_p': w['p'], 'flags': flags,
                                'asr_text': ' '.join(x['text'].strip() for x in seg['words'][i:i + 12]),
                                'event_text': ev['text'][:200]})
    anchors.sort(key=lambda r: (r['source_s'], r['event_id']))
    judge(anchors)
    return grid, anchors


def local_line(near: list[dict], t: float) -> float:
    """Theil-Sen offset at reel time ``t``: robust to a minority of wrong neighbours, follows slow drift."""
    pts = [(c['source_s'], c['offset_s']) for c in near]
    slopes = [(o2 - o1) / (t2 - t1) for k, (t1, o1) in enumerate(pts) for t2, o2 in pts[k + 1:] if t2 - t1 > 1]
    slope = median(slopes) if slopes else 0.0
    if abs(slope) > 0.02:  # faster than 2 % speed error: an edit between neighbours, not drift
        slope = 0.0
    return median(o - slope * (s - t) for s, o in pts)


def judge(anchors: list[dict]) -> None:
    """Local consistency: offset against the median of clean neighbours on the reel clock."""
    counts = Counter(a['event_id'] for a in anchors)
    for n, a in enumerate(anchors):
        a['anchor_id'] = f'{a["reel"]}-{n + 1:05d}'
        if counts[a['event_id']] > 1:
            a['flags'] = a['flags'] + ['event_matched_twice']
    clean = [a for a in anchors if not a['flags']]
    times = [a['source_s'] for a in clean]
    for a in anchors:
        t = a['source_s']
        i = bisect.bisect_left(times, t)
        before = [c for c in clean[max(0, i - LOCAL_EACH - 1):i] if c is not a and t - c['source_s'] <= LOCAL_HALF]
        after = [c for c in clean[i:i + LOCAL_EACH + 1] if c is not a and c['source_s'] - t <= LOCAL_HALF]
        near = (before[-LOCAL_EACH:] + after[:LOCAL_EACH])
        if set(a['flags']) & HARD_FLAGS:
            a['status'] = 'rejected'
        if len(near) < 3:
            a['local_offset_s'], a['residual_s'] = '', ''
            a.setdefault('status', 'isolated')
            continue
        local = local_line(near, t)
        a['local_offset_s'] = round(local, 3)
        a['residual_s'] = round(a['offset_s'] - local, 3)
        if 'status' in a:
            continue
        if abs(a['residual_s']) > OK_RESIDUAL:
            a['status'] = 'outlier'
        elif a['flags']:
            a['status'] = 'flagged'
        else:
            a['status'] = 'ok'


# ------------------------------------------------------------------ driver
def run(step: str, only: list[str] | None = None) -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    if step in ('reels', 'all'):
        rows = build_reels()
        print(f'sg reels: {len(rows)} reels -> {REELS_TSV.relative_to(MISSION_DATA.parent)}')
        if step == 'reels':
            return 0
    reels = [r for r in load_reels() if not only or r['reel'] in only]
    settings = SG_STORE / reels[0]['asr_settings_id']
    cache = {}

    def segments_of(reel):
        if reel['reel'] not in cache:
            cache[reel['reel']] = load_asr(settings / 'sources' / 'sg' / reel['reel'] / reel['asr_identity_id'])
        return cache[reel['reel']]

    import md_pao
    announcements = load_pao_announcements(md_pao.run(load_reels(), lambda segs: segs, segments_of))
    events = load_events() if step in ('anchors', 'all') else []
    pao_rows, offset_rows, anchor_rows, summary = [], [], [], []
    for reel in reels:
        segments = segments_of(reel)
        pao = find_pao(reel, segments, announcements)
        pao_rows.extend(pao)
        if step == 'pao':
            print(f'  {reel["reel"]}: {len(pao)} PAO intervals, {sum(p["end_s"] - p["start_s"] for p in pao):.0f} s')
            continue
        pao_segs = {i for p in pao for i in p['_segs']}
        grid, anchors = match_reel(reel, segments, events, pao_segs)
        offset_rows.extend(grid)
        anchor_rows.extend(anchors)
        status = Counter(a['status'] for a in anchors)
        good = sorted(a['source_s'] for a in anchors if a['status'] in ('ok', 'flagged'))
        covered = sum(min(b - a, 120) for a, b in zip(good, good[1:]))  # gaps over 2 min are not covered
        primary = [g for g in grid if g['rank'] == 1]
        note = '' if anchors else 'no anchors'
        summary.append({'reel': reel['reel'], 'duration_s': reel['duration_s'], 'pao_get_start': reel['pao_get_start'],
                        'pao_get_end': reel['pao_get_end'],
                        'asr_words': sum(len(s['words']) for s in segments), 'pao_intervals': len(pao),
                        'pao_s': round(sum(p['end_s'] - p['start_s'] for p in pao)), 'offset_points': len(primary),
                        'offset_first': format_get(round(primary[0]['offset_s'])) if primary else '',
                        'offset_last': format_get(round(primary[-1]['offset_s'])) if primary else '',
                        'anchors': len(anchors), 'ok': status['ok'], 'flagged': status['flagged'],
                        'rejected': status['rejected'], 'outliers': status['outlier'],
                        'isolated': status['isolated'], 'covered_s': round(covered), 'note': note})
        s = summary[-1]
        print(f'  {reel["reel"]}: {s["anchors"]} anchors ({s["ok"]} ok, {s["outliers"]} outliers), '
              f'offset {s["offset_first"]}..{s["offset_last"]}, PAO {s["pao_s"]} s')
    suffix = '' if not only else '_partial'
    write_tsv(OUT / f'sg_pao{suffix}.tsv', PAO_FIELDS, pao_rows)
    if step != 'pao':
        write_tsv(OUT / f'sg_offsets{suffix}.tsv', OFFSET_FIELDS, offset_rows)
        write_tsv(OUT / f'sg_anchors{suffix}.tsv', ANCHOR_FIELDS, anchor_rows)
        write_tsv(OUT / f'sg_summary{suffix}.tsv', SUMMARY_FIELDS, summary)
        total = Counter(a['status'] for a in anchor_rows)
        print(f'sg anchors: {len(anchor_rows)} anchors {dict(total)}; PAO intervals {len(pao_rows)}; '
              f'outputs in {OUT.relative_to(LOCAL.parent.parent)}')
    return 0


def main(args) -> int:
    return run(args.step, args.reel)
