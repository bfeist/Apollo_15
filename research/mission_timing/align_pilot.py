"""Pilot: forced alignment of the first-pass words against visually picked onsets.

Scores Whisper's native word starts and the CTC-aligned starts of the same words on

* the 31 reviewed call openings of PAO reel 572-AAA (``runs/mission_timing_pilot/pao_anchors.csv``),
* the Spur exchange picks on 572-AAA and T510092 CH23 (``catalog/spur_exchange.tsv``),

and reports alignment coverage and speed for whole sources. Writes nothing to the stores.

uv run python research/mission_timing/align_pilot.py

It aligns the stored rows again, so it applies to Whisper-time rows (it ran before the stores were
aligned in place). On aligned rows ``wsf`` would be overwritten by the aligned times.
"""
import argparse
import csv
import gzip
import json
import re
import sys
import time
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / 'scripts'))
from _bootstrap import install  # noqa: E402
install()

import mission_align as ma  # noqa: E402
from audio_sources import Source  # noqa: E402

SG = Path('F:/Apollo_15_data/processing/sg/timing_transcripts/asr-c1198b48a6d3')
TAPE = Path('D:/Apollo_15_30-Track/timing_transcripts/asr-c1198b48a6d3')
NARA = Path('F:/Apollo_15_data/processing/sg_nara/retimed_transcripts/asr-08ce474a85b7')
ROOTS = {'sg': Path('F:/Apollo_15_data/raw/audio/Internet Archive'), 'tape': Path('D:/Apollo_15_30-Track')}


def tokens(text):
    text = text.replace('’', "'")
    return [t.replace("'", '') for t in re.findall(r"[a-z0-9']+", text.lower())]


def identity_dirs(store, key):
    base = store / 'sources' / Path(*key.split('/'))
    return sorted(p for p in base.iterdir() if (p / 'source.json').is_file())


def load_source(identity):
    record = json.loads((identity / 'source.json').read_text(encoding='utf-8'))['source']
    source = Source.from_json(record)
    if not Path(source.path).is_file() and source.collection in ROOTS:
        source.path = str(ROOTS[source.collection] / source.relpath)
    return source


def current_identity(store, key):
    found = [p for p in identity_dirs(store, key)]
    for p in found:
        s = load_source(p)
        if Path(s.path).is_file() and Path(s.path).stat().st_size == s.size:
            return p, s
    raise SystemExit(f'No current identity for {key}')


def chunks(identity):
    for folder in sorted((identity / 'chunks').glob('c[0-9]*')):
        receipt = json.loads((folder / 'chunk.json').read_text(encoding='utf-8'))
        with gzip.open(folder / 'segments.jsonl.gz', 'rt', encoding='utf-8') as f:
            rows = [json.loads(line) for line in f if line.strip()]
        yield folder, receipt, rows


def align_source(aligner, identity, source, window=None, totals=None):
    """Aligned rows of the chunks of one source (optionally only those meeting [a, b) seconds)."""
    out = []
    rate = source.sample_rate
    origin = source.canonical_origin_s or 0.0
    for folder, receipt, rows in chunks(identity):
        lo, hi = (f / rate + origin for f in receipt['cut_frames'])
        if window and (hi < window[0] or lo > window[1]):
            continue
        t0 = time.perf_counter()
        audio, start = ma.chunk_audio(source, receipt)
        t1 = time.perf_counter()
        aligned, stats = ma.align_chunk(aligner, rows, receipt, audio, start)
        t2 = time.perf_counter()
        if totals is not None:
            totals['read_s'] += t1 - t0
            totals['align_s'] += t2 - t1
            totals['source_s'] += receipt['owned_seconds']
            totals['retained_s'] += receipt['retained_seconds']
            for k, v in stats.items():
                totals[k] = totals.get(k, 0) + v
            for row in aligned:
                for w in row['words']:
                    for f in w['flags']:
                        totals['flag_' + f] = totals.get('flag_' + f, 0) + 1
        out.extend(aligned)
    return out


def word_list(rows, rate, origin):
    words = []
    for row in rows:
        for w in row['words']:
            toks = tokens(w['w'])
            if not toks or w.get('wsf') is None:
                continue
            for t in toks:
                words.append(dict(tok=t, whisper=w['wsf'] / rate + origin,
                                  aligned=w['sf'] / rate + origin if w.get('sf') is not None else None,
                                  ap=w.get('ap'), flags=w['flags'], seg_flags=row['flags']))
    return words


def score(words, events, span=60.0, need=3):
    """For each (event_id, truth_s, text): the nearest Whisper occurrence of its opening tokens."""
    seq = [w['tok'] for w in words]
    out = []
    for event_id, truth, text in events:
        query = tokens(text)[:need]
        best = None
        for i in range(len(seq) - len(query) + 1):
            if seq[i:i + len(query)] != query:
                continue
            w = words[i]
            if abs(w['whisper'] - truth) > span and (w['aligned'] is None or abs(w['aligned'] - truth) > span):
                continue
            d = min(abs(w['whisper'] - truth), abs(w['aligned'] - truth) if w['aligned'] is not None else 1e9)
            if best is None or d < best[0]:
                best = (d, w)
        if best is None and need > 2:
            out.extend(score(words, [(event_id, truth, text)], span, need - 1))
            continue
        w = best[1] if best else None
        out.append(dict(event=event_id, truth=truth, query=' '.join(query),
                        whisper_err=None if w is None else round(w['whisper'] - truth, 3),
                        aligned_err=None if w is None or w['aligned'] is None else round(w['aligned'] - truth, 3),
                        ap=None if w is None else w['ap'],
                        whisper_collapsed=None if w is None else 'collapsed_timing' in w['seg_flags'] or
                        'zero_duration' in w['flags']))
    return out


def summary(results, key):
    e = np.array([abs(r[key]) for r in results if r[key] is not None])
    if not len(e):
        return 'n=0'
    return (f'n={len(e)} median={np.median(e):.3f} p90={np.percentile(e, 90):.3f} worst={e.max():.3f} '
            f'<=0.25s={int((e <= 0.25).sum())} <=0.5s={int((e <= 0.5).sum())}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, default=REPO / 'runs' / 'align_pilot')
    parser.add_argument('--coverage', action='append', default=[],
                        help="extra coverage run: 'nara:<key>' or 'tape:<key>' (whole source)")
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    t0 = time.perf_counter()
    aligner = ma.Aligner()
    print(f'model load {time.perf_counter() - t0:.1f} s', flush=True)
    report = {}

    # 572-AAA: the whole reel (coverage and speed) and both reviewed sets.
    identity, source = current_identity(SG, 'sg/572-AAA')
    totals = dict(read_s=0.0, align_s=0.0, source_s=0.0, retained_s=0.0)
    rows = align_source(aligner, identity, source, totals=totals)
    report['572-AAA totals'] = totals
    words = word_list(rows, source.sample_rate, 0.0)
    with (REPO / 'runs/mission_timing_pilot/pao_anchors.csv').open(encoding='utf-8') as f:
        anchors = [(r['anchor_id'], float(r['source_seconds']), r['text']) for r in csv.DictReader(f)]
    pao = score(words, anchors)
    spur_rows = list(csv.DictReader((REPO / 'catalog/spur_exchange.tsv').open(encoding='utf-8'), delimiter='\t'))
    spur_text = {r['event_id']: r['landmark'] for r in spur_rows}

    def opening(landmark):
        m = re.search(r'(?:of|sound of)\s+(.*)$', landmark)
        return m.group(1) if m else landmark

    spur_pao = score(words, [(r['event_id'], float(r['observed_local_s']), r['recognition_evidence'])
                             for r in spur_rows if r['source_id'] == 'pao_572aaa'], need=2)
    report['pao_31'] = dict(whisper=summary(pao, 'whisper_err'), aligned=summary(pao, 'aligned_err'))
    report['spur_pao'] = dict(whisper=summary(spur_pao, 'whisper_err'), aligned=summary(spur_pao, 'aligned_err'))

    # T510092 CH23 around Spur.
    identity, source = current_identity(TAPE, 'tape/A15_T510092/CH23')
    ch23 = [r for r in spur_rows if r['source_id'] == 't510092_ch23']
    times = [float(r['canonical_05b_s']) for r in ch23]
    tape_totals = dict(read_s=0.0, align_s=0.0, source_s=0.0, retained_s=0.0)
    rows = align_source(aligner, identity, source, (min(times) - 900, max(times) + 900), tape_totals)
    words = word_list(rows, source.sample_rate, source.canonical_origin_s)
    spur_ch23 = score(words, [(r['event_id'], float(r['canonical_05b_s']), r['recognition_evidence']) for r in ch23],
                      need=2)
    report['spur_ch23'] = dict(whisper=summary(spur_ch23, 'whisper_err'), aligned=summary(spur_ch23, 'aligned_err'),
                               source_sha256=source.content_sha256)
    report['T510092 CH23 totals'] = tape_totals

    for spec in args.coverage:
        store, key = spec.split(':', 1)
        identity, source = current_identity({'nara': NARA, 'tape': TAPE, 'sg': SG}[store], key)
        t = dict(read_s=0.0, align_s=0.0, source_s=0.0, retained_s=0.0)
        align_source(aligner, identity, source, totals=t)
        report[f'{key} totals'] = t

    detail = dict(pao_31=pao, spur_pao=spur_pao, spur_ch23=spur_ch23)
    (args.out / 'report.json').write_text(json.dumps(dict(report=report, detail=detail), indent=1), encoding='utf-8')
    for name, value in report.items():
        print(name, json.dumps(value))
    for name, rows in detail.items():
        print(f'--- {name}')
        for r in rows:
            print(f"  {r['event']:24s} {r['query']:28s} whisper {r['whisper_err']!s:>9} aligned {r['aligned_err']!s:>8} "
                  f"ap {r['ap']!s:>6} collapsed {r['whisper_collapsed']}")


if __name__ == '__main__':
    main()
