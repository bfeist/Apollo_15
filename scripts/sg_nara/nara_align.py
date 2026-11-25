"""NARA step 2: where each file of a tape starts on the shared tape clock.

The four files of a flight tape come from one simultaneous capture through one transport,
but NARA cut them with different start points (up to 80 s apart on 650-AAA). Measured where
two files both carry IRIG-B, the offset between them is constant to well under a millisecond
for the whole tape (no drift), so each file needs one start offset and nothing else.

Evidence, per pair of files:

* ``irig``: the same strict GMT second decoded on both files; the offset is the median of
  (time on b - time on a); precise to a fraction of a millisecond.
* ``irig_candidates``: where one file has strict frames and the other only weak timecode
  bleed, its frame candidates are paired by decoded label and the dominant offset cluster is
  taken (misread labels scatter; ``MIN_CAND`` frames and ``CAND_DOMINANCE`` over the runner-up).
* ``audio``: for neighbouring files, cross-correlation of the audio. The head straddles the
  recorded tracks, so neighbouring files carry the same programme at different levels.
  GCC-PHAT on 8 kHz audio with the IRIG band (600-1500 Hz) removed, in ``WINDOWS`` windows
  across the tape, each searched over +/-``SEARCH_S``; accepted when at least ``MIN_AGREE``
  windows agree within ``AGREE_S``; offset and drift are fitted to those windows and a drift
  above ``MAX_DRIFT_PPM`` (a separate pass at another speed) is not accepted. Used only to
  place files without shared timecode.

Programme and timecode need not agree. On 650-AAA the voice on tr1, tr2 and tr3 lines up
0.68 s differently from the timecode on tr1 and tr3 (each constant over the whole tape): the
recorded tracks do not all sit at the same along-tape position, or a loop was recorded twice
with a delay. ``audio_checks`` reports, for every timecode-linked pair, how far the programme
correlation lies from the timecode offset, so a file placed by audio carries a known caveat.

Two placements are solved from the reference file (most strict frames):

* ``start_s`` (programme): from audio evidence first, timecode only for files without it. All
  files rendered with these offsets keep their speech mutually aligned. tape seconds = file
  seconds + start_s; the earliest file starts at tape second 0.
* ``clock_start_s`` (timecode): from timecode evidence only, on the same axis. The map pools
  every file's frames with these offsets.

A file that cannot be placed by programme is reported and left out of the shared timeline.

Outputs: ``F:/Apollo_15_data/processing/sg_nara/align/<tape>.json`` and ``align.tsv``.

    uv run python scripts/run.py nara_align.py --tape all --workers 6
"""
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))
from _bootstrap import install as _install
_install()

import argparse
import hashlib
import json
import os
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from scipy import signal

import nara_irig
from nara_irig_survey import IRIG_ROOT, load as load_survey
from nara_sources import DATA_ROOT, NARA_ROOT, by_tape, tracks

ALIGN_ROOT = DATA_ROOT / 'align'
VERSION = 'nara-align-v1'
RATE = 8000
MIN_COMMON = 20        # shared strict seconds for an IRIG offset
IRIG_MAD_S = 0.002     # their spread (median absolute deviation) must be below this
WINDOWS = 16
WINDOW_S = 20.0
SEARCH_S = 150.0
AGREE_S = 0.025        # windows agreeing within this count towards one offset ...
MIN_AGREE = 5          # ... and at least this many must; offset and drift are then fitted
MAX_DRIFT_PPM = 10     # a larger drift between files means separate passes: not placed
CAND_BIN_S = 0.002     # candidate-frame offsets are clustered in bins of this width ...
MIN_CAND = 15          # ... and the winning cluster needs this many frames ...
CAND_DOMINANCE = 3.0   # ... and this many times the runner-up


def survey_frames(root, track, strict=True):
    j = load_survey(root, track)
    if j is None:
        raise ValueError(f'{track.key}: no current IRIG survey (run nara_irig_survey.py)')
    f = np.array(j['frames'], dtype=float).reshape(-1, 9)
    if strict:
        f = f[f[:, 7] == 1]
    return f[:, 0], (f[:, 1] - 1) * 86400 + f[:, 2] * 3600 + f[:, 3] * 60 + f[:, 4]


def strict_frames(root, track):
    return survey_frames(root, track, True)


def candidate_offset(fa, cb):
    """Offset b - a from strict frames on a and every frame candidate on b (timecode bleed).

    Candidates on a weak file often misread a field; pairing by decoded label then gives
    scattered offsets, while correctly read ones pile up at the true offset.
    """
    (ta, ga), (tb, gb) = fa, cb
    index = {g: t for g, t in zip(ga, ta)}
    d = np.array([t - index[g] for g, t in zip(gb, tb) if g in index])
    if len(d) < MIN_CAND:
        return None
    bins = np.round(d / CAND_BIN_S).astype(np.int64)
    vals, counts = np.unique(bins, return_counts=True)
    # merge each bin with its neighbours so a cluster split by a bin edge counts once
    merged = np.array([counts[np.abs(vals - v) <= 1].sum() for v in vals])
    k = int(np.argmax(merged))
    best = merged[k]
    rival = merged[np.abs(vals - vals[k]) > 2].max(initial=0)
    if best < MIN_CAND or best < CAND_DOMINANCE * max(rival, 1):
        return None
    sel = np.abs(d - vals[k] * CAND_BIN_S) <= 1.5 * CAND_BIN_S
    return float(np.median(d[sel])), int(sel.sum()), int(rival)


def irig_offset(fa, fb):
    """(offset b-a in seconds, MAD, count) from shared strict seconds, or None."""
    (ta, ga), (tb, gb) = fa, fb
    common, ia, ib = np.intersect1d(ga, gb, return_indices=True)
    if len(common) < MIN_COMMON:
        return None
    d = tb[ib] - ta[ia]
    med = np.median(d)
    d = d[np.abs(d - med) < 0.05]  # misread fields pair different seconds
    if len(d) < MIN_COMMON:
        return None
    med = float(np.median(d))
    mad = float(np.median(np.abs(d - med)))
    return (med, mad, int(len(d))) if mad <= IRIG_MAD_S else None


def decode_8k(track) -> np.ndarray:
    """Whole file at 8 kHz, IRIG band removed (sequential decode, same origin as the survey)."""
    # causal filters, block by block: the same delay on every file cancels in the offsets
    sos = np.r_[signal.butter(4, [150, 3400], btype='bandpass', fs=RATE, output='sos'),
                signal.butter(6, [550, 1600], btype='bandstop', fs=RATE, output='sos')]
    zi = np.zeros((sos.shape[0], 2))
    out = []
    proc = nara_irig.ffmpeg_reader(track.path, track.channel, RATE)
    for x in nara_irig.read_blocks(proc, RATE * 600):
        y, zi = signal.sosfilt(sos, x, zi=zi)
        out.append(y.astype(np.float32))
    return np.concatenate(out)


def gcc_lag(a: np.ndarray, b: np.ndarray) -> tuple[float, float]:
    """Lag (samples) of a's pattern inside b, and peak prominence (peak / 2nd peak)."""
    n = len(a) + len(b)
    nfft = 1 << (n - 1).bit_length()
    # taper both, or PHAT whitening turns the window edges into peaks at the extreme lags
    A = np.fft.rfft(a * signal.windows.tukey(len(a), 0.2), nfft)
    B = np.fft.rfft(b * signal.windows.tukey(len(b), 0.05), nfft)
    R = B * np.conj(A)
    R /= np.maximum(np.abs(R), 1e-12)
    cc = np.fft.irfft(R, nfft)[:len(b) - len(a) + 1]
    edge = int(0.02 * len(cc))
    cc[:edge] = cc[-edge:] = 0  # ignore the extreme lags
    k = int(np.argmax(cc))
    peak = cc[k]
    if 0 < k < len(cc) - 1:
        y0, y1, y2 = cc[k - 1], cc[k], cc[k + 1]
        den = y0 - 2 * y1 + y2
        frac = 0.5 * (y0 - y2) / den if den else 0.0
    else:
        frac = 0.0
    masked = cc.copy()
    masked[max(0, k - 80):k + 80] = 0
    return k + frac, float(peak / max(masked.max(), 1e-12))


def audio_offset(xa: np.ndarray, xb: np.ndarray) -> dict:
    """Offset b - a in seconds from windowed GCC-PHAT, with per-window evidence."""
    n = min(len(xa), len(xb))
    w, s = int(WINDOW_S * RATE), int(SEARCH_S * RATE)
    centres = np.linspace(s + w, n - s - w, WINDOWS).astype(int)
    rows = []
    for c in centres:
        a = xa[c:c + w]
        if np.std(a) < 1e-5:
            continue
        lo = max(0, c - s)
        b = xb[lo:c + w + s]
        lag, prom = gcc_lag(a, b)
        # a's sample c appears at b's sample lo + lag: time on b - time on a
        rows.append(dict(window_s=round(c / RATE, 1), offset_s=float((lo + lag - c) / RATE),
                         prominence=round(prom, 2)))
    if not rows:
        return dict(status='no_signal', windows=rows)
    off = np.array([r['offset_s'] for r in rows])
    at = np.array([r['window_s'] for r in rows])
    best, agree = None, 0
    for v in off:
        k = int(np.sum(np.abs(off - v) <= AGREE_S))
        if k > agree:
            best, agree = v, k
    sel = np.abs(off - best) <= AGREE_S
    if agree < MIN_AGREE:
        return dict(status='inconsistent', offset_s=float(best), agreeing=agree, windows_tried=len(rows),
                    windows=rows)
    slope, icpt = np.polyfit(at[sel] - at[sel].mean(), off[sel], 1)
    resid = off[sel] - (icpt + slope * (at[sel] - at[sel].mean()))
    drift_ppm = float(slope * 1e6)
    out = dict(offset_s=float(np.median(off[sel])), agreeing=agree, windows_tried=len(rows),
               spread_ms=round(float(np.ptp(off[sel])) * 1000, 3),
               residual_rms_ms=round(float(np.sqrt(np.mean(resid ** 2))) * 1000, 3),
               drift_ppm=round(drift_ppm, 3), windows=rows)
    out['status'] = 'ok' if abs(drift_ppm) <= MAX_DRIFT_PPM else 'drifting'
    return out


def solve(names, ref, edges, kinds):
    """Offsets (time on file - time on ref, same content) along edges of the given kinds,
    in order of preference; returns (offsets, how each file was placed)."""
    offs, how = {ref: 0.0}, {ref: 'reference'}
    usable = [e for e in edges if e['kind'] in kinds and (e['kind'] != 'audio' or e['status'] == 'ok')]
    usable.sort(key=lambda e: kinds.index(e['kind']))
    changed = True
    while changed:
        changed = False
        for e in usable:
            for x, y, sign in ((e['a'], e['b'], 1), (e['b'], e['a'], -1)):
                if x in offs and y not in offs:
                    offs[y] = offs[x] + sign * e['offset_s']
                    how[y] = f"{e['kind']} via {x}"
                    changed = True
    return offs, how


def align_tape(tape, items, irig_root):
    frames = {t.name: strict_frames(irig_root, t) for t in items}
    names = [t.name for t in items]
    by_name = {t.name: t for t in items}
    ref = max(names, key=lambda n: len(frames[n][0]))
    edges = []
    # timecode evidence: strict seconds on both files, else strict on one and bleed on the other
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            r = irig_offset(frames[a], frames[b])
            if r:
                edges.append(dict(a=a, b=b, kind='irig', offset_s=r[0], mad_ms=round(r[1] * 1000, 3), count=r[2]))
    linked = {frozenset((e['a'], e['b'])) for e in edges}
    for a in names:
        for b in names:
            if a == b or frozenset((a, b)) in linked or len(frames[a][0]) < MIN_COMMON:
                continue
            r = candidate_offset(frames[a], survey_frames(irig_root, by_name[b], strict=False))
            if r:
                edges.append(dict(a=a, b=b, kind='irig_candidates', offset_s=r[0], count=r[1], rival=r[2]))
                linked.add(frozenset((a, b)))
    # programme evidence: every adjacent pair, then whatever else is needed to link every file
    audio = {n: decode_8k(by_name[n]) for n in names}
    tried = set()

    def audio_edge(a, b):
        a, b = sorted((a, b), key=names.index)
        tried.add(frozenset((a, b)))
        edges.append(dict(a=a, b=b, kind='audio', **audio_offset(audio[a], audio[b])))

    for i in range(len(names) - 1):
        audio_edge(names[i], names[i + 1])
    programme_kinds = ('audio', 'irig', 'irig_candidates')
    for m in names:
        if m in solve(names, ref, edges, programme_kinds)[0]:
            continue
        placed = solve(names, ref, edges, programme_kinds)[0]
        for p_ in sorted((n for n in names if n != m), key=lambda n: (n not in placed, abs(names.index(n) - names.index(m)))):
            if frozenset((p_, m)) not in tried:
                audio_edge(p_, m)
                if m in solve(names, ref, edges, programme_kinds)[0]:
                    break
    # every timecode-linked pair: how far the programme correlation lies from the timecode offset
    checks = []
    for e in [e for e in edges if e['kind'] in ('irig', 'irig_candidates')]:
        if frozenset((e['a'], e['b'])) not in tried:
            audio_edge(e['a'], e['b'])
        r = next(x for x in edges if x['kind'] == 'audio' and {x['a'], x['b']} == {e['a'], e['b']})
        row = dict(a=e['a'], b=e['b'], kind=e['kind'], irig_offset_s=e['offset_s'], audio_status=r['status'])
        if r['status'] == 'ok':
            sign = 1 if r['a'] == e['a'] else -1
            row.update(audio_offset_s=sign * r['offset_s'],
                       difference_ms=round((sign * r['offset_s'] - e['offset_s']) * 1000, 3))
        checks.append(row)
    clock, clock_how = solve(names, ref, edges, ('irig', 'irig_candidates'))
    prog, prog_how = solve(names, ref, edges, programme_kinds)
    # tape seconds = file seconds + start_s (programme placement; the earliest file starts at 0);
    # clock_start_s puts the same file's timecode on that axis
    zero = min(-v for v in prog.values())
    start = {n: round(-prog[n] - zero, 6) for n in prog}
    clock_start = {n: round(-clock[n] - zero, 6) for n in clock}
    dur = {t.name: t.duration_s for t in items}
    return dict(tape=tape, version=VERSION, reference=ref,
                tape_duration_s=max(start[n] + dur[n] for n in start),
                files={t.name: dict(path=t.path, sha256=t.sha256, duration_s=t.duration_s,
                                    strict_frames=int(len(frames[t.name][0])),
                                    start_s=start.get(t.name), placed_by=prog_how.get(t.name, 'not placed'),
                                    clock_start_s=clock_start.get(t.name),
                                    clock_placed_by=clock_how.get(t.name, 'no timecode link'),
                                    clock_minus_programme_ms=round((clock_start[t.name] - start[t.name]) * 1000, 3)
                                    if t.name in clock_start and t.name in start else None)
                       for t in items},
                unplaced=[n for n in names if n not in prog], edges=edges, audio_checks=checks,
                time_domain='tape seconds = file seconds + start_s (programme); timecode frames of a file '
                            'sit at file seconds + clock_start_s')


def run(args_tuple):
    tape, items, irig_root, out = args_tuple
    m = align_tape(tape, items, irig_root)
    m.update(implementation_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
             built=datetime.now(timezone.utc).isoformat(timespec='seconds'))
    (out / f'{tape}.json').write_text(json.dumps(m, indent=1) + '\n', encoding='utf-8')
    return m


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    p.add_argument('--tape', nargs='+', required=True, help="tapes (255-PAOa-640-AAA or 640-AAA), or 'all'")
    p.add_argument('--workers', type=int, default=4)
    p.add_argument('--nara-root', type=Path, default=NARA_ROOT)
    p.add_argument('--irig-root', type=Path, default=IRIG_ROOT)
    p.add_argument('--output-root', type=Path, default=ALIGN_ROOT)
    args = p.parse_args(argv)
    args.output_root.mkdir(parents=True, exist_ok=True)
    tapes = by_tape(tracks(args.nara_root, args.tape))
    jobs = [(tape, items, args.irig_root, args.output_root) for tape, items in tapes.items()
            if any(load_survey(args.irig_root, t) and load_survey(args.irig_root, t)['strict'] for t in items)]
    rows = ['tape\treference\t' + '\t'.join(f'tr{i}_start_s\ttr{i}_by' for i in range(1, 5)) + '\tunplaced\tchecks_ms']
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        for m in pool.map(run, jobs):
            cells = []
            for i in range(1, 5):
                f = m['files'].get(f'tr{i}', {})
                cells += [str(f.get('start_s', '')), f.get('placed_by', ''), str(f.get('clock_minus_programme_ms', ''))]
            checks = ','.join(str(c['difference_ms']) for c in m['audio_checks'])
            rows.append(f"{m['tape']}\t{m['reference']}\t" + '\t'.join(cells) + f"\t{','.join(m['unplaced'])}\t{checks}")
            print(rows[-1], flush=True)
    if 'all' in args.tape:
        (args.output_root / 'align.tsv').write_text('\n'.join(rows) + '\n', encoding='utf-8')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
