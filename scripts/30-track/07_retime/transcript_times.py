"""Stage 07b: check the GETs of the mission transcripts against words heard on retimed audio.

Every timed utterance of the 1971 technical transcript (TEC), the PAO edition, the AFJ and the
ALSJ (``.local/mission_data/scraped_data``) is looked for in a transcript of retimed audio: its
first ``N`` comparable words must occur as one run, once within ``WINDOW_S`` of the printed GET,
with the words that follow agreeing too (``FOLLOW_MIN``). For each match

    d = heard GET of the first word - transcript GET

is the transcript's timing error plus the recognizer's onset error. Printed and journal GETs are
whole seconds, so a correct stamp truncated to the second gives 0 <= d < 1.

The heard GET is independent of the transcripts only where it comes from IRIG-B time code (the
07i plans and the NARA flight tapes). 07a plans are fitted to these same transcripts, so their
matches are labelled ``sg_fit`` and kept out of the independent summary.

Where it runs:

* ``write_timed_transcript`` (07b) calls ``check_channel`` for every retimed channel and writes
  ``transcript_times.tsv``/``.json`` beside ``timed_transcript.tsv``. A missing mission-data
  checkout (``.local`` is not in Git) skips the check without failing the render.
* ``tape``: one tape's channel checks combined, one row per utterance (the median over the
  channels that heard it), with per-channel medians: a channel on the wrong head row stands out
  by about 1.38 s.
* ``preview``: the same check before 07b has rendered, on the 06 transcripts placed through the
  newest 07i plan (``irig_plan``) or, without one, the tape's IRIG-B curve inside its coverage,
  with the 07b head-row shift. Outputs go to ``stage_evidence/retiming/transcript_times/<tape>/``.

    uv run python scripts/run.py transcript_times.py preview --tape A15_T510075
    uv run python scripts/run.py transcript_times.py preview --timecode-tapes
    uv run python scripts/run.py transcript_times.py tape --tape A15_T510075
"""
from __future__ import annotations

import argparse
import bisect
import difflib
import hashlib
import json
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

VERSION = 'transcript-times-v1'
N = 4                  # first words of an utterance that must be heard as one run
SPAN_S = 4.0           # ... within this many seconds
WINDOW_S = 90.0        # searched this far either side of the transcript GET
FOLLOW_MIN = 0.5       # share of the next 12 utterance words heard after the run
TIMED_BASES = ('printed', 'anchor', 'stamp')  # own stamps; 'inherited' journal rows and PAO headers are not
STRETCH_WIDTH = 15     # running median over this many matches ...
STRETCH_LIMIT_S = 1.5  # ... more than this from +0.5 s marks a shifted stretch
OUTLIER_S = 3.0        # |d - 0.5| above this lists the row for review
FIELDS = ('source', 'event_id', 'chapter', 'who', 'basis', 'get', 'get_s', 'heard_get_s', 'd_s', 'timing',
          'channel', 'follow', 'word_p', 'text', 'heard')
TAPE_FIELDS = ('source', 'event_id', 'chapter', 'who', 'basis', 'get', 'get_s', 'heard_get_s', 'd_s', 'timing',
               'channels', 'd_spread_s', 'text', 'heard')


_SHA = {}


def _sha(path):
    """SHA-256 of a file, memoized by (path, size, mtime) within one process."""
    st = Path(path).stat()
    key = (str(path), st.st_size, st.st_mtime_ns)
    if key not in _SHA:
        _SHA[key] = hashlib.sha256(Path(path).read_bytes()).hexdigest()
    return _SHA[key]


def _fmt(seconds):
    from md_common import format_get
    return format_get(int(np.floor(seconds)))


# ------------------------------------------------------------------ transcripts
def mission_files():
    from md_common import SCRAPED
    names = ('transcript_tec.tsv', 'transcript_pao.tsv', 'journal_afj.tsv', 'journal_alsj.tsv',
             'journal_spans_afj.tsv', 'journal_spans_alsj.tsv')
    return {n: SCRAPED / n for n in names}


def mission_state():
    """Checksums of the scraped transcripts and journals, or None when they are not here."""
    files = mission_files()
    if not all(p.is_file() for p in files.values()):
        return None
    return {n: _sha(p) for n, p in files.items()}


_EVENTS = {}


def load_events(state=None):
    """Timed utterances with at least ``N`` comparable words, sorted by GET (cached per state)."""
    state = state or mission_state()
    if state is None:
        raise FileNotFoundError('no scraped mission transcripts (.local/mission_data/scraped_data)')
    key = json.dumps(state, sort_keys=True)
    if key in _EVENTS:
        return _EVENTS[key]
    from md_reconcile import comparable, load_journal, load_printed
    out = []
    for edition in ('tec', 'pao'):
        for r in load_printed(edition):
            out.append(dict(source=edition.upper(), event_id=r['event_id'], chapter=f"p{int(r['pdf_page']):04d}",
                            who=r.get('speaker') or r.get('speaker_ocr', ''), basis=r['get_basis'], t=r['t'],
                            tokens=comparable(r['tokens']), text=r['text_ocr']))
    for source in ('afj', 'alsj'):
        rows, _ = load_journal(source)
        for b in rows:
            out.append(dict(source=source.upper(), event_id=b['block_key'], chapter=b['chapter'],
                            who=b['speaker_label'], basis=b['get_basis'], t=b['t'], tokens=comparable(b['tokens']),
                            text=b['text']))
    out = sorted((e for e in out if len(e['tokens']) >= N), key=lambda e: (e['t'], e['source'], e['event_id']))
    _EVENTS.clear()
    _EVENTS[key] = out
    return out


# ------------------------------------------------------------------- matching
def match(tok, get, timing, events, channel='', p=None, window=WINDOW_S):
    """Matches of ``events`` in one word stream (comparable tokens ``tok`` at GET ``get``).

    ``timing`` labels each word's GET ('timecode', 'sg_fit', ...); words with a NaN GET are skipped."""
    tok, get = np.asarray(tok), np.asarray(get, float)
    timing = np.asarray(timing)
    p = np.zeros(len(tok)) if p is None else np.asarray(p, float)
    ok = np.isfinite(get)
    tok, get, timing, p = tok[ok], get[ok], timing[ok], p[ok]
    if len(tok) < N:
        return []
    grams = defaultdict(list)
    for i in range(len(tok) - N + 1):
        if 0 <= get[i + N - 1] - get[i] < SPAN_S:
            grams[' '.join(tok[i:i + N])].append(i)
    times = [e['t'] for e in events]
    lo = bisect.bisect_left(times, float(get.min()) - window)
    hi = bisect.bisect_right(times, float(get.max()) + window)
    rows = []
    for ev in events[lo:hi]:
        hits = [i for i in grams.get(' '.join(ev['tokens'][:N]), ()) if abs(get[i] - ev['t']) <= window]
        if len(hits) != 1:
            continue
        i = hits[0]
        follow = list(ev['tokens'][N:N + 12])
        f = 1.0
        if follow:
            sm = difflib.SequenceMatcher(None, follow, list(tok[i + N:i + N + len(follow) + 4]), autojunk=False)
            f = sum(b.size for b in sm.get_matching_blocks()) / len(follow)
        if f < FOLLOW_MIN:
            continue
        rows.append(dict(source=ev['source'], event_id=ev['event_id'], chapter=ev['chapter'], who=ev['who'],
                         basis=ev['basis'], get=_fmt(ev['t']), get_s=ev['t'], heard_get_s=round(float(get[i]), 3),
                         d_s=round(float(get[i]) - ev['t'], 3), timing=str(timing[i]), channel=channel,
                         follow=round(f, 2), word_p=round(float(p[i]), 3),
                         text=' '.join(str(ev['text']).split())[:160], heard=' '.join(tok[i:i + 12])))
    return rows


def stats(d):
    d = np.asarray(d, float)
    if not len(d):
        return dict(n=0)
    med = float(np.median(d))
    return dict(n=int(len(d)), median_s=round(med, 3), mad_s=round(float(np.median(np.abs(d - med))), 3),
                p10_s=round(float(np.percentile(d, 10)), 3), p90_s=round(float(np.percentile(d, 90)), 3),
                within_second=round(float(np.mean((d >= -1) & (d < 2))), 4),
                over_5s=round(float(np.mean(np.abs(d) > 5)), 4))


def stretches(rows, width=STRETCH_WIDTH, limit=STRETCH_LIMIT_S):
    """Runs where the running median of d leaves +0.5 +- ``limit`` s: a shifted stretch of stamps."""
    rows = sorted(rows, key=lambda r: r['get_s'])
    d = np.array([r['d_s'] for r in rows], float)
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
            out.append(dict(get_from=rows[k]['get'], get_to=rows[j]['get'], n=j - k + 1,
                            median_d_s=round(float(np.median(d[k:j + 1])), 3),
                            chapters=sorted({r['chapter'] for r in rows[k:j + 1]})))
        k = j + 1
    return out


def summarize(rows, independent=('timecode',)):
    """Statistics per source on the independently timed matches, shifted stretches and outliers."""
    own = [r for r in rows if r['basis'] in TIMED_BASES]
    ind = [r for r in own if r['timing'] in independent]
    by = defaultdict(list)
    for r in ind:
        by[r['source']].append(r['d_s'])
    other = defaultdict(list)
    for r in rows:
        if r not in ind:
            other[f"{r['source']}:{r['basis']}:{r['timing']}"].append(r['d_s'])
    return dict(
        independent_timing=list(independent), matches=len(rows), independent_matches=len(ind),
        sources={s: stats(v) for s, v in sorted(by.items())},
        not_independent={k: stats(v) for k, v in sorted(other.items())},
        stretches={s: stretches([r for r in ind if r['source'] == s]) for s in sorted(by)},
        outliers=[{k: r[k] for k in ('source', 'event_id', 'get', 'd_s', 'channel', 'text')}
                  for r in sorted(ind, key=lambda r: r['get_s']) if abs(r['d_s'] - 0.5) > OUTLIER_S],
        reading='d = heard GET - transcript GET; a correct whole-second stamp gives 0 <= d < 1 (median about '
                '+0.35 s against IRIG-B on the NARA tapes, docs/24)')


def write_tsv(path, fields, rows):
    path = Path(path)
    tmp = path.with_name(path.name + '.partial')
    with open(tmp, 'w', encoding='utf-8', newline='\n') as f:
        f.write('\t'.join(fields) + '\n')
        for r in rows:
            f.write('\t'.join(str(r.get(k, '')).replace('\t', ' ').replace('\n', ' ') for k in fields) + '\n')
    tmp.replace(path)


def write_json(path, value):
    path = Path(path)
    tmp = path.with_name(path.name + '.partial')
    tmp.write_text(json.dumps(value, indent=1, allow_nan=False) + '\n', encoding='utf-8', newline='\n')
    tmp.replace(path)


# ------------------------------------------------------------ retimed channel
def _words_from_tsv(path):
    """(tokens, GET, 05b-local start, probability) of the usable words of a timed transcript."""
    from sg_inputs import tokenizer
    tokens, comparable, hard, bad_segment = tokenizer()
    tok, get, src, p = [], [], [], []
    skip = False
    with open(path, encoding='utf-8') as f:
        header = f.readline().rstrip('\n').split('\t')
        col = {k: i for i, k in enumerate(header)}
        for line in f:
            r = line.rstrip('\n').split('\t')
            flags = set(filter(None, r[col['flags']].split(',')))
            if r[col['kind']] == 'segment':
                skip = bool(bad_segment & flags)
                continue
            if skip or hard & flags or not r[col['get_start_s']]:
                continue
            for t in comparable(tokens(r[col['text']])):
                tok.append(t)
                get.append(float(r[col['get_start_s']]))
                src.append(float(r[col['source_start_s']]))
                p.append(float(r[col['probability']] or 0))
    order = np.argsort(get, kind='stable')
    return (np.asarray(tok, dtype=str)[order] if tok else np.zeros(0, '<U1'), np.asarray(get, float)[order],
            np.asarray(src, float)[order], np.asarray(p, float)[order])


def plan_coverage(plan):
    """Canonical tape intervals (earlier head row) timed from IRIG-B time code by this plan, else []."""
    if not str(plan.get('method', '')).startswith('irig_timecode'):
        return []
    return [tuple(map(float, iv)) for iv in (plan.get('support') or {}).get('covered_intervals_s', [])]


def timing_labels(real, coverage, other):
    lab = np.full(len(real), other, dtype=object)
    for a, b in coverage:
        lab[(real >= a) & (real <= b)] = 'timecode'
    return lab


def check_channel(destination, progress=print):
    """Write (or reuse) ``transcript_times.tsv/.json`` beside one channel's ``timed_transcript.tsv``."""
    destination = Path(destination)
    meta_path, tsv_path = destination / 'timed_transcript.json', destination / 'timed_transcript.tsv'
    if not meta_path.is_file() or not tsv_path.is_file():
        return None
    state = mission_state()
    if state is None:
        progress(f'{destination}: no scraped mission transcripts here; transcript times not checked')
        return None
    timed = json.loads(meta_path.read_text(encoding='utf-8'))
    record = json.loads((destination / 'retime.json').read_text(encoding='utf-8'))
    shared = record.get('shared_plan') or {}
    # the timed transcript's own record holds its TSV checksum (07b verifies it on every reuse)
    inputs = dict(timed_transcript_sha256=timed['tsv_sha256'], retime_record_sha256=_sha(destination / 'retime.json'),
                  mission=state, implementation_sha256=_sha(__file__))
    out_json, out_tsv = destination / 'transcript_times.json', destination / 'transcript_times.tsv'
    if out_json.is_file():
        old = json.loads(out_json.read_text(encoding='utf-8'))
        if old.get('inputs') == inputs and out_tsv.is_file():
            return old
        stamp = old.get('inputs', {}).get('timed_transcript_sha256', 'old')[:8] + '-' + \
            hashlib.sha256(json.dumps(old.get('inputs'), sort_keys=True).encode()).hexdigest()[:8]
        for p in (out_json, out_tsv):  # a re-scrape of the journals: keep the earlier check beside it
            if p.is_file():
                p.replace(p.with_name(f'{p.stem}.superseded-{stamp}{p.suffix}'))
    plan = json.loads(Path(shared['path']).read_text(encoding='utf-8')) if shared.get('path') else {}
    tok, get, src, p = _words_from_tsv(tsv_path)
    canonical = float(timed['time_basis']['canonical_origin_s']) + src
    knots = (shared.get('head_row_shift') or {}).get('knots')
    if knots:
        from head_rows import shift_at
        canonical = canonical - shift_at(knots, canonical)
    method = str(plan.get('method') or '')
    other = 'sg_fit' if 'sg' in method else ('timecode_bridged' if method.startswith('irig') else method or 'unplaced')
    labels = timing_labels(canonical, plan_coverage(plan), other)
    channel = destination.name
    rows = match(tok, get, labels, load_events(state), channel, p)
    rows.sort(key=lambda r: (r['get_s'], r['source'], r['event_id']))
    write_tsv(out_tsv, FIELDS, rows)
    out = dict(schema=1, version=VERSION, status='complete', inputs=inputs, channel=channel,
               plan=dict(path=shared.get('path'), method=plan.get('method'), version=plan.get('version')),
               words=int(len(tok)), summary=summarize(rows), tsv_sha256=_sha(out_tsv), columns=list(FIELDS))
    write_json(out_json, out)
    s = out['summary']['sources'].get('TEC', {})
    progress(f"{destination}: transcript times, {len(rows)} matches"
             + (f", TEC median d {s['median_s']:+.2f} s (n={s['n']}, time code)" if s.get('n') else ''))
    return out


# ------------------------------------------------------------------ tape level
def combine(per_channel):
    """One row per utterance: the median d over the channels that heard it, and per-channel medians."""
    by = defaultdict(list)
    for rows in per_channel.values():
        for r in rows:
            by[r['source'], r['event_id']].append(r)
    events = []
    for rs in by.values():
        d = np.array([r['d_s'] for r in rs], float)
        r0 = min(rs, key=lambda r: abs(r['d_s'] - float(np.median(d))))
        timing = 'timecode' if all(r['timing'] == 'timecode' for r in rs) else \
            ','.join(sorted({r['timing'] for r in rs}))
        events.append(dict(r0, d_s=round(float(np.median(d)), 3), heard_get_s=round(r0['get_s'] + float(np.median(d)), 3),
                           timing=timing, channels=','.join(sorted(r['channel'] for r in rs)),
                           d_spread_s=round(float(d.max() - d.min()), 3)))
    events.sort(key=lambda r: (r['get_s'], r['source'], r['event_id']))
    # channel medians against the tape's own utterance medians (a head-row error is ~1.38 s)
    ref = {(r['source'], r['event_id']): r['d_s'] for r in events}
    channels = {}
    for ch, rows in sorted(per_channel.items()):
        own = [r for r in rows if r['basis'] in TIMED_BASES]
        rel = [r['d_s'] - ref[r['source'], r['event_id']] for r in own
               if len(by[r['source'], r['event_id']]) > 1]
        channels[ch] = dict(stats(np.array([r['d_s'] for r in own])),
                            minus_tape_median_s=round(float(np.median(rel)), 3) if rel else None, shared=len(rel))
    flagged = [ch for ch, c in channels.items() if c['minus_tape_median_s'] is not None and c['shared'] >= 10
               and abs(c['minus_tape_median_s']) > 0.5]
    return events, channels, flagged


def _publish(folder, write):
    import sg_stage
    sg_stage.publish(folder, write)
    return folder


def tape_check(tape, retimed_root=None, root=None, progress=print):
    """Combine the per-channel checks of a retimed tape into one edition."""
    from sg_inputs import RETIMING_ROOT, TAPE_DATA, digest
    retimed_root = Path(retimed_root or TAPE_DATA / 'retimed')
    root = Path(root or RETIMING_ROOT)
    channels = {}
    for d in sorted((retimed_root / tape).glob('CH[0-9][0-9]')):
        out = check_channel(d, progress)
        if out is not None:
            channels[d.name] = (d / 'transcript_times.tsv', out['tsv_sha256'])
    if not channels:
        raise ValueError(f'{tape}: no retimed channel with a timed transcript under {retimed_root}')
    payload = dict(version=VERSION, kind='retimed', tape=tape, channels={c: h for c, (_, h) in channels.items()})
    folder = root / 'transcript_times' / tape / f"retimed-{digest(payload, 16)}"

    def write(tmp):
        from md_common import read_tsv
        per = {}
        for c, (p, h) in channels.items():
            if hashlib.sha256(p.read_bytes()).hexdigest() != h:
                raise ValueError(f'{p} differs from its record')
            per[c] = [dict(r, get_s=int(r['get_s']), d_s=float(r['d_s'])) for r in read_tsv(p)]
        _write_tape(tmp, tape, payload, per)
    _publish(folder, write)
    _report(folder, progress)
    return folder


def tape_check_safe(tape, retimed_root=None, progress=print):
    """``tape_check`` after 07b has published a tape; reported and skipped, never failing the render."""
    try:
        return tape_check(tape, retimed_root, progress=progress)
    except Exception as error:  # noqa: BLE001 - a report, not a render product
        progress(f'{tape}: transcript times not combined ({type(error).__name__}: {error})')
        return None


def _write_tape(tmp, tape, payload, per):
    events, channels, flagged = combine(per)
    write_tsv(tmp / 'events.tsv', TAPE_FIELDS, events)
    summary = dict(schema=1, version=VERSION, tape=tape, status='complete', inputs=payload,
                   summary=summarize(events), channels=channels, channels_off_tape_median=flagged,
                   events_sha256=_sha(tmp / 'events.tsv'), columns=list(TAPE_FIELDS))
    write_json(tmp / 'summary.json', summary)
    write_json(tmp / 'manifest.json', dict(status='complete', version=VERSION, tape=tape, inputs=payload))


def _report(folder, progress=print):
    s = json.loads((Path(folder) / 'summary.json').read_text(encoding='utf-8'))
    progress(f"{s['tape']}: {s['summary']['matches']} utterances heard, {s['summary']['independent_matches']} on "
             f"time-code timing -> {folder}")
    for src, v in s['summary']['sources'].items():
        if v.get('n'):
            progress(f"  {src:4s} n={v['n']:5d} median {v['median_s']:+.2f} s  MAD {v['mad_s']:.2f}  "
                     f"p10 {v['p10_s']:+.2f}  p90 {v['p90_s']:+.2f}  within -1..+2 s {v['within_second']:.1%}  "
                     f"|d|>5 s {v['over_5s']:.1%}")
    for src, runs in s['summary']['stretches'].items():
        for r in runs:
            progress(f"  shifted {src} {r['get_from']}..{r['get_to']} n={r['n']} median d {r['median_d_s']:+.2f} s "
                     f"{', '.join(r['chapters'])[:80]}")
    for ch in s['channels_off_tape_median']:
        progress(f"  {ch}: {s['channels'][ch]['minus_tape_median_s']:+.2f} s from the tape's utterance medians "
                 f"(head row?)")


# --------------------------------------------------------------------- preview
def preview_tape(tape, root=None, curve=False, progress=print):
    """The check before 07b: 06 words placed by the 07i plan (or the IRIG-B curve) and the head-row shift."""
    import irig_timing as it
    import head_rows
    from sg_inputs import RETIMING_ROOT, digest, load_words, tape_channels
    root = Path(root or RETIMING_ROOT)
    track, _ = it.timecode_track(tape, root / 'irig')
    if track is None:
        progress(f'{tape}: no time code; nothing to check independently')
        return None
    plan_path = None if curve else it.irig_plan(tape, root=root)
    channels = [c for c in tape_channels(tape) if not c['problem'] and c['identity_dir']]
    duration = max(c['origin_s'] + c['duration_s'] for c in channels)
    rows_plan = head_rows.row_plan(tape, [c['channel'] for c in tape_channels(tape)], duration,
                                   timecode_channel=track['channel'], root=root)
    state = mission_state()
    payload = dict(version=VERSION, kind='preview', tape=tape, code=_sha(__file__),
                   placement=('plan', _sha(plan_path)) if plan_path else ('irig_curve', track['review_sha256'],
                                                                          track['strict']),
                   head_rows=head_rows.row_plan_digest(rows_plan), mission=state,
                   channels=[(c['channel'], c['review_sha256'], c['identity_id']) for c in channels])
    folder = root / 'transcript_times' / tape / f"preview-{digest(payload, 16)}"

    def write(tmp):
        if plan_path:
            plan = json.loads(Path(plan_path).read_text(encoding='utf-8'))
            with np.load(Path(plan_path).parent / 'timing_map.npz') as z:
                tt, oo = z['input_seconds'], z['output_seconds']
            segs = plan['get_placement']['segments']
            coverage = plan_coverage(plan)
        else:
            f = np.asarray(track['frames'], float)
            irig = it.IrigCurve(f[:, 0], f[:, 1])
            launch = it._nara()[1].RANGE_ZERO_S
        events = load_events(state)
        per = {}
        for c in channels:
            w = load_words(Path(c['identity_dir']))
            canonical = c['origin_s'] + w.s
            knots = rows_plan['channels'][str(c['channel'])]['knots']
            real = canonical - head_rows.shift_at(knots, canonical)
            get = np.full(len(real), np.nan)
            if plan_path:
                out = np.interp(real, tt, oo)
                for s in segs:
                    m = (real >= s['tape_start_s']) & (real <= s['tape_end_s'])
                    get[m] = out[m] + s['get_equals_output_plus_s']
                labels = timing_labels(real, coverage, 'sg_fit')
            else:
                labels = np.full(len(real), 'unplaced', dtype=object)
                for k, s in enumerate(irig.segments):
                    for a, b in s['covered']:
                        m = (real >= a) & (real <= b)
                        get[m] = irig.gmt(k, real[m]) - launch
                        labels[m] = 'timecode'
            name = f"CH{c['channel']:02d}"
            per[name] = match(w.tok, get, labels, events, name, w.p)
            progress(f'{tape} {name}: {len(w.tok):,} words, {len(per[name])} matches')
        for ch, rows in per.items():
            write_tsv(tmp / f'{ch}.tsv', FIELDS, rows)
        meta = dict(placement=f'07i plan {plan_path}' if plan_path else 'IRIG-B curve of the time-code track '
                    '(inside its coverage only; equals the 07i plan there)', head_rows=rows_plan['anchors'],
                    timecode_channel=track['channel'])
        _write_tape(tmp, tape, dict(payload, **meta), per)
    _publish(folder, write)
    _report(folder, progress)
    return folder


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    p.add_argument('step', choices=('preview', 'tape'))
    p.add_argument('--tape', action='append', default=[])
    p.add_argument('--timecode-tapes', action='store_true', help='every tape with a decoded time-code track')
    p.add_argument('--curve', action='store_true', help='preview: use the IRIG-B curve even where a 07i plan exists')
    p.add_argument('--retimed-root', type=Path)
    args = p.parse_args(argv)
    tapes = list(args.tape)
    if args.timecode_tapes:
        import irig_timing as it
        from sg_inputs import RETIMING_ROOT
        tapes += [d.name for d in sorted((RETIMING_ROOT / 'irig').glob('A15_T*'))
                  if it.timecode_track(d.name)[0] is not None and d.name not in tapes]
    if not tapes:
        p.error('give --tape or --timecode-tapes')
    for tape in tapes:
        if args.step == 'preview':
            preview_tape(tape, curve=args.curve, progress=lambda m: print(m, flush=True))
        else:
            tape_check(tape, args.retimed_root, progress=lambda m: print(m, flush=True))
    return 0


if __name__ == '__main__':
    sys.exit(main())
