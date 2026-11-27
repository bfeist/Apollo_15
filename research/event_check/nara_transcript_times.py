"""Check transcript GETs against the IRIG-B-timed NARA flight tapes. Read-only.

The NARA flight tapes (608, 640-655-AAA, docs/19) are respeeded to mission rate from their own
IRIG-B time code, so a word heard in the respeeded mix has a measured GET (``sg_inputs.nara_sources``:
output seconds + the time-code constant, less the voice-row lag of catalog/nara_voice, docs/23).

For every timed utterance of the 1971 technical transcript (TEC), the PAO edition, the AFJ and
the ALSJ, this finds the utterance's first four comparable words as a run in the NARA ASR within
``--window`` seconds of the printed GET, with the matcher of stage 07b
(``scripts/30-track/07_retime/transcript_times.py``; docs/24). The difference

    d = heard GET of the first word - transcript GET

is the transcript's timing error plus the ASR onset error. Printed and journal GETs are whole
seconds, so a truncated stamp of a correct time gives 0 <= d < 1.

Each match is also tagged with whether the tape's time code was decoded near it (frames less than
30 s apart, as the map's coverage), so held-rate stretches can be told apart.

  uv run python research/event_check/nara_transcript_times.py
  uv run python research/event_check/nara_transcript_times.py --out .local/mission_data/sg/nara_transcript_check.tsv
"""
from __future__ import annotations

import argparse
import bisect
import json
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / 'scripts/30-track/07_retime'), str(ROOT / 'scripts/00_common'),
                str(ROOT / 'scripts/30-track/06_transcribe'), str(ROOT / 'scripts/sg-audio/02_sg-transcript'),
                str(ROOT / 'scripts/sg-audio/01_sg-nara'), str(ROOT / 'scripts/sg-audio')]
from sg_inputs import load_words, nara_sources, source_get  # noqa: E402
from md_common import format_get  # noqa: E402
import transcript_times as tt  # noqa: E402  (the same matcher as stage 07b)

CACHE = Path('D:/Apollo_15_30-Track/stage_evidence/retiming/_cache/words')
MAPS = Path('F:/Apollo_15_data/processing/sg_nara/maps')
ALIGN = Path('F:/Apollo_15_data/processing/sg_nara/align')
SUPPORT_GAP_S = 30.0
FIELDS = ('source', 'event_id', 'chapter', 'who', 'basis', 'get', 'get_s', 'tape', 'heard_get_s', 'd_s',
          'd_timecode_row_s', 'irig_supported', 'follow', 'word_p', 'text', 'asr')


def frame_outputs(tape: str) -> np.ndarray | None:
    """Output seconds of the tape's strict IRIG frames used by the map (for the coverage tag)."""
    try:
        from nara_irig_survey import IRIG_ROOT
        from nara_sources import NARA_ROOT, by_tape, tracks
        from nara_tape_map import pooled_frames
    except Exception as error:  # noqa: BLE001
        print(f'coverage tag unavailable: {error}', file=sys.stderr)
        return None
    full = f'255-PAOa-{tape}'
    items = by_tape(tracks(NARA_ROOT, [full])).get(full)
    align = json.loads((ALIGN / f'{full}.json').read_text(encoding='utf-8'))
    P = pooled_frames(items, IRIG_ROOT, {n: f['clock_start_s'] for n, f in align['files'].items()})
    if not len(P):
        return None
    z = np.load(MAPS / f'{full}.npz')
    t = P[:, 0]
    return np.sort(t + np.interp(t, z['t'], z['D']))


def supported(frames: np.ndarray | None, u: float) -> bool:
    if frames is None or not len(frames):
        return False
    i = bisect.bisect_left(frames, u)
    lo = frames[i - 1] if i > 0 else -np.inf
    hi = frames[i] if i < len(frames) else np.inf
    return hi - lo < SUPPORT_GAP_S


def tape_words(src: dict):
    w = load_words(Path(src['identity_dir']), CACHE)
    get, seg = source_get(src, w.s)
    keep = seg >= 0
    late = np.array([src['segments'][k]['voice_late_s'] for k in seg[keep]]) if keep.any() else np.zeros(0)
    return dict(name=src['name'], tok=w.tok[keep], get=get[keep], p=w.p[keep], late=late,
                const=src['segments'][0]['constant_s'] + src['segments'][0]['voice_late_s'])


def match(events, tapes, window):
    """transcript_times.match on each tape, with the IRIG coverage tag and the uncorrected (time-code row) d."""
    rows = []
    for tp in tapes:
        support = np.array([supported(tp['frames'], g - tp['const']) for g in tp['get']])
        labels = np.where(support, 'timecode', 'held')
        late_at = dict(zip(np.round(tp['get'], 3), tp['late']))
        for r in tt.match(tp['tok'], tp['get'], labels, events, tp['name'], tp['p'], window):
            r.update(tape=tp['name'], irig_supported=r['timing'] == 'timecode', asr=r['heard'],
                     d_timecode_row_s=round(r['d_s'] + float(late_at.get(r['heard_get_s'], 0.0)), 3))
            rows.append(r)
    return rows


def stats(d):
    d = np.asarray(d, float)
    if not len(d):
        return 'n=0'
    med = np.median(d)
    mad = np.median(np.abs(d - med))
    return (f'n={len(d):5d} median {med:+6.2f} s  MAD {mad:4.2f}  p10 {np.percentile(d, 10):+6.2f}  '
            f'p90 {np.percentile(d, 90):+6.2f}  in [-1,+2) {np.mean((d >= -1) & (d < 2)):5.1%}  '
            f'|d|>5 {np.mean(np.abs(d) > 5):5.1%}')


def runs_off(rows, width=15, limit=1.5):
    """Spans where the running median of d leaves +-limit (a shifted stretch of the transcript)."""
    rows = sorted(rows, key=lambda r: r['get_s'])
    d = np.array([r['d_s'] for r in rows])
    if len(d) < width:
        return []
    med = np.array([np.median(d[max(0, k - width // 2):k + width // 2 + 1]) for k in range(len(d))])
    bad = np.abs(med - 0.5) > limit
    out, k = [], 0
    while k < len(d):
        if not bad[k]:
            k += 1
            continue
        j = k
        while j + 1 < len(d) and bad[j + 1]:
            j += 1
        if j - k + 1 >= width // 2:
            out.append((rows[k]['get_s'], rows[j]['get_s'], j - k + 1, float(np.median(d[k:j + 1])),
                        sorted({r['chapter'] for r in rows[k:j + 1]})))
        k = j + 1
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('--window', type=float, default=90.0, help='search half-width around the transcript GET (s)')
    ap.add_argument('--out', type=Path, default=ROOT / '.local/mission_data/sg/nara_transcript_check.tsv')
    args = ap.parse_args()
    tapes = []
    for src in nara_sources():
        if not src['identity_dir']:
            continue
        tp = tape_words(src)
        tp['frames'] = frame_outputs(src['name'])
        tapes.append(tp)
        print(f"{tp['name']}: {len(tp['tok'])} words, GET {format_get(int(tp['get'].min()))}..{format_get(int(tp['get'].max()))}, "
              f"{0 if tp['frames'] is None else len(tp['frames'])} IRIG frames", flush=True)
    events = tt.load_events()
    rows = match(events, tapes, args.window)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, 'w', encoding='utf-8', newline='\n') as f:
        f.write('\t'.join(FIELDS) + '\n')
        for r in sorted(rows, key=lambda r: (r['get_s'], r['source'])):
            f.write('\t'.join(str(r[k]) for k in FIELDS) + '\n')
    print(f'\n{len(rows)} matches -> {args.out}')
    print('\nd = heard GET (IRIG, voice-row corrected) - transcript GET')
    by = defaultdict(list)
    for r in rows:
        by[r['source'], r['basis']].append(r)
    for (source, basis), rs in sorted(by.items()):
        print(f'{source:4s} {basis:14s} all       {stats([r["d_s"] for r in rs])}')
        sup = [r['d_s'] for r in rs if r['irig_supported']]
        print(f'{"":4s} {"":14s} IRIG-only {stats(sup)}')
        print(f'{"":4s} {"":14s} timecode-row (no voice-row correction) {stats([r["d_timecode_row_s"] for r in rs if r["irig_supported"]])}')
    print('\nPer tape (IRIG-supported, timed basis only)')
    for source in ('TEC', 'AFJ', 'ALSJ'):
        for tape in sorted({r['tape'] for r in rows}):
            d = [r['d_s'] for r in rows if r['source'] == source and r['tape'] == tape and r['irig_supported']
                 and r['basis'] in ('printed', 'anchor', 'stamp')]
            if len(d) >= 5:
                print(f'{source:4s} {tape}  {stats(d)}')
    print('\nShifted stretches (running median of 15 more than 1.5 s from +0.5 s)')
    for source in ('TEC', 'AFJ', 'ALSJ'):
        rs = [r for r in rows if r['source'] == source and r['basis'] in ('printed', 'anchor', 'stamp')]
        for a, b, n, med, ch in runs_off(rs):
            print(f'{source:4s} {format_get(a)}..{format_get(b)}  n={n:3d}  median d {med:+.2f} s  {", ".join(ch)[:100]}')


if __name__ == '__main__':
    main()
