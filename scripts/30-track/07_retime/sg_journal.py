"""Journal anchors for the 07a joint fit: the 1971 transcripts placed directly on the 30-track tapes.

docs/23_2026-11-26_head_rows_and_real_time.md, "Journal anchors".

The SG reference (IA reels and NARA flight tapes) leaves stretches of 30-track tape without
anchors: GET 7-9, 15-25 and 40-49 h, 653-AAA, everything after 285.8 h, and the gaps inside reels.
The technical air-to-ground transcript (TEC) and the Apollo Flight Journal (AFJ, whose stamps are
the TEC times) cover the whole mission. Their timed rows are matched to the 06 transcript of every
channel with the transcript-time technique (transcript_times.py): the first ``n`` comparable words
of a row as one run, heard once in the search window, with the following words agreeing too.

* Each match is moved onto the earlier, real-time head row with the tape's head-row plan
  (head_rows.row_plan), and the channels that heard a row give one event: median tape time.
* Printed stamps are whole seconds and run about 0.35-0.5 s early against IRIG-B (truncation; the
  transcript-time check on the NARA tapes). One mission-wide nuisance offset ``journal:TEC`` with
  prior ``prior_mean_s`` +/- ``prior_sigma_s`` is fitted jointly, so the NARA and time-code tapes
  calibrate it for every other tape.
* Event sigma is ``sigma_s``; the fit's robust weights reject misprints (about 3 % of rows are more
  than 5 s off). Before that, an event more than ``outlier_s`` from the running median of its
  neighbours' offsets is dropped.

Tapes with an SG anchors curve get the journal events as extra positions in the fit. Tapes without
one (T510111, T510112: no SG reference after 285.8 h, no time code) get a journal anchors edition
built through the SG pipeline (``sg_match.vote`` and ``sg_stage.pooled_fit``: stagger groups from
the channels that heard the same row, then a curve). It is written as
``<tape>/sg_anchors/anchors-journal-<digest>`` with the files of an anchors edition, so head_rows
and 07b use it like any other.

This module only extends the fit (through sg_levels.build_fit); the upstream SG editions and their
code hash are unchanged.
"""
from __future__ import annotations

import bisect
import dataclasses
import difflib
import hashlib
import time
from collections import defaultdict
from pathlib import Path

import numpy as np

VERSION = 'journal-anchors-v1'
KEY = 'journal:TEC'
JOURNAL = dict(
    sources=('TEC', 'AFJ'),
    n=4, span_s=4.0, follow_min=0.5,
    window_s=600.0,          # first pass: search window around the tape's SG curve (tapes with SG anchors)
    refine_window_s=60.0,    # second pass: around the curve plus the running offset of the first pass
    prior_window_s=3600.0,   # ... around the inventory prior (tapes without)
    sigma_s=0.5,
    prior_mean_s=-0.40,      # printed GET minus true GET (whole-second stamps, truncated)
    prior_sigma_s=0.2,
    outlier_s=3.0, outlier_width=15,
    min_events=20,
)
SEGMENT = dict(key=KEY, kind='journal', source='TEC/AFJ timed rows', nuisance=True, constant_s=0.0,
               prior_mean_s=JOURNAL['prior_mean_s'], prior_sigma_s=JOURNAL['prior_sigma_s'],
               get_start_s=-1e9, get_end_s=1e9)


def code_sha():
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


_ROWS = {}


def journal_rows():
    """Timed TEC/AFJ rows (own stamps only), one per (stamp, first words): AFJ repeats the TEC."""
    import transcript_times as tt
    state = tt.mission_state()
    if state is None:
        raise FileNotFoundError('no scraped mission transcripts (.local/mission_data/scraped_data)')
    key = tuple(sorted(state.items()))
    if key not in _ROWS:
        seen, rows = set(), []
        for e in tt.load_events(state):
            if e['source'] not in JOURNAL['sources'] or e['basis'] not in tt.TIMED_BASES:
                continue
            k = (int(e['t']), ' '.join(e['tokens'][:JOURNAL['n']]))
            if k in seen:
                continue
            seen.add(k)
            rows.append(e)
        _ROWS.clear()
        _ROWS[key] = rows
    return _ROWS[key], state


def match_channel(tok, est_get, rows, window):
    """[(row index, word index)] of ``rows`` heard in one channel (comparable tokens ``tok`` placed at
    estimated GET ``est_get``): the first ``n`` words as one run, once within ``window``."""
    n, span = JOURNAL['n'], JOURNAL['span_s']
    tok, est_get = np.asarray(tok), np.asarray(est_get, float)
    if len(tok) < n:
        return []
    grams = defaultdict(list)
    for i in range(len(tok) - n + 1):
        if 0 <= est_get[i + n - 1] - est_get[i] < span:
            grams[' '.join(tok[i:i + n])].append(i)
    times = [r['t'] for r in rows]
    lo = bisect.bisect_left(times, float(est_get.min()) - window)
    hi = bisect.bisect_right(times, float(est_get.max()) + window)
    out = []
    for j in range(lo, hi):
        r = rows[j]
        hits = [i for i in grams.get(' '.join(r['tokens'][:n]), ()) if abs(est_get[i] - r['t']) <= window]
        if len(hits) != 1:
            continue
        i = hits[0]
        follow = list(r['tokens'][n:n + 12])
        if follow:
            sm = difflib.SequenceMatcher(None, follow, list(tok[i + n:i + n + len(follow) + 4]), autojunk=False)
            if sum(b.size for b in sm.get_matching_blocks()) / len(follow) < JOURNAL['follow_min']:
                continue
        out.append((j, i))
    return out


def drop_outliers(t, d):
    """Mask of events within ``outlier_s`` of the running median of their neighbours' offsets."""
    order = np.argsort(t)
    keep = np.zeros(len(t), bool)
    w = JOURNAL['outlier_width']
    ds = np.asarray(d, float)[order]
    for k in range(len(ds)):
        a, b = max(0, k - w // 2), min(len(ds), k + w // 2 + 1)
        keep[order[k]] = abs(ds[k] - np.median(ds[a:b])) <= JOURNAL['outlier_s']
    return keep


def _channels(tape):
    from sg_inputs import tape_channels
    return [c for c in tape_channels(tape) if not c['problem'] and c['identity_dir']]


def _words(c, root):
    from sg_inputs import load_words
    return load_words(Path(c['identity_dir']), Path(root) / '_cache' / 'words')


# ------------------------------------------------------- tapes with an SG curve
def tape_events(tape, anchors, duration, root, log=print):
    """Journal events of a tape with an SG anchors curve: dicts t (real-time row), g (printed GET)."""
    import head_rows
    import sg_curve
    from sg_inputs import read_json
    rows, _ = journal_rows()
    curve = sg_curve.Curve.from_json(read_json(Path(anchors) / 'curve.json'))
    channels = _channels(tape)
    plan = head_rows.row_plan(tape, [c['channel'] for c in channels], duration, root=root)
    words = []
    for c in channels:
        w = _words(c, root)
        canonical = c['origin_s'] + w.s
        words.append((w.tok, canonical - head_rows.shift_at(plan['channels'][str(c['channel'])]['knots'], canonical)))

    def heard_with(place, window):
        heard = defaultdict(list)
        for tok, real in words:
            for j, i in match_channel(tok, place(real), rows, window):
                heard[j].append(float(real[i]))
        return heard
    # 1. wide window around the SG curve, which can be minutes off where it extrapolates over a gap
    first = _events(heard_with(curve.get, JOURNAL['window_s']), rows, curve.get)
    if not first:
        return []
    # 2. narrow window around the curve plus the running offset of the first pass
    t1 = np.array([e['t'] for e in first])
    d1 = np.array([e['g'] for e in first]) - curve.get(t1)
    smooth = np.array([np.median(d1[max(0, k - 7):k + 8]) for k in range(len(d1))])

    def place(t):
        return curve.get(t) + np.interp(t, t1, smooth)
    events = _events(heard_with(place, JOURNAL['refine_window_s']), rows, place)
    t = np.array([e['t'] for e in events])
    keep = drop_outliers(t, np.array([e['g'] for e in events]) - place(t))
    out = [e for e, k in zip(events, keep) if k]
    log(f'  {tape}: {len(out)} journal events ({len(events) - len(out)} dropped as outliers; first pass {len(first)})')
    return out


def _events(heard, rows, place):
    events = [dict(t=float(np.median(v)), g=float(rows[j]['t']), copies=len(v), spread_s=float(np.ptp(v)),
                   row=rows[j]['event_id'], source=rows[j]['source']) for j, v in heard.items()]
    return sorted(events, key=lambda e: e['t'])


def extend(ev, manifest, extra, tape, anchors, root, log=print):
    """``ev`` (sg_curve.TapeEvidence) plus the tape's journal events; ``extra`` records them."""
    if manifest.get('journal_anchors'):
        return ev, extra       # a journal anchors edition: its events are journal events already
    events = tape_events(tape, anchors, ev.duration, Path(anchors).parents[2], log)  # the anchors' own root
    _, state = journal_rows()
    extra = dict(extra, journal=dict(version=VERSION, code=code_sha(), settings=JOURNAL, mission=state,
                                     events=len(events)))
    if not events:
        return ev, extra
    n = len(events)
    ev = dataclasses.replace(
        ev, t=np.r_[ev.t, [e['t'] for e in events]], g=np.r_[ev.g, [e['g'] for e in events]],
        sigma=np.r_[ev.sigma, np.full(n, JOURNAL['sigma_s'])],
        bkey=np.r_[ev.bkey, np.full(n, KEY, dtype=object)].astype(object),
        ref_kind=np.r_[ev.ref_kind, np.full(n, 'journal', dtype=object)].astype(object),
        kind=np.r_[ev.kind, np.full(n, 'journal', dtype=object)].astype(object))
    return ev, extra


# ---------------------------------------------------- tapes without an SG curve
def anchors_payload(tape, reference_name):
    import sg_stage
    from sg_inputs import tape_prior
    _, state = journal_rows()
    channels = [(c['channel'], c['identity_id'], c['review_sha256'], c['origin_s']) for c in _channels(tape)]
    return dict(version=VERSION, stage=sg_stage.VERSION, tape=tape, reference=reference_name, channels=channels,
                prior=tape_prior(tape), mission=state, settings=JOURNAL, code=code_sha(), stage_code=sg_stage.code_sha())


def build_anchors(tape, reference_folder, root, log=print):
    """A journal anchors edition for a tape without an SG curve; returns the folder or None."""
    import sg_match
    import sg_stage
    from sg_inputs import digest, prior_get, tape_prior, write_json, write_tsv
    reference_folder = Path(reference_folder)
    payload = anchors_payload(tape, reference_folder.name)
    folder = Path(root) / tape / 'sg_anchors' / f'anchors-journal-{digest(payload, 16)}'
    if sg_stage.complete(folder):
        return folder if sg_stage.read_json(folder / 'manifest.json').get('result') == 'curve' else None
    rows, _ = journal_rows()
    prior = tape_prior(tape)
    channels = _channels(tape)
    duration = max(c['origin_s'] + c['duration_s'] for c in channels)
    cols = defaultdict(list)
    for c in channels:
        w = _words(c, root)
        canonical = c['origin_s'] + w.s
        for j, i in match_channel(w.tok, prior_get(prior, canonical), rows, JOURNAL['prior_window_s']):
            g = float(rows[j]['t'])
            cols['channel'].append(c['channel'])
            cols['t'].append(float(canonical[i]))
            cols['local_s'].append(float(w.s[i]))
            cols['g'].append(g)
            cols['d'].append(g - float(prior_get(prior, canonical[i])))
            cols['ref'].append(j)
            cols['src'].append(0)
            cols['seg'].append(0)
            cols['p'].append(float(w.p[i]))
            cols['aligned'].append(False)
    pairs = {k: np.asarray(v) for k, v in cols.items()}
    record = dict(status='complete', edition=folder.name, step='anchors', tape=tape, version=sg_stage.VERSION,
                  journal_anchors=VERSION, reference=reference_folder.name, inputs=payload, duration_s=duration,
                  origins={c['channel']: c['origin_s'] for c in channels},
                  reviews={c['channel']: c['review_sha256'] for c in channels},
                  basis='TEC/AFJ timed rows matched on the 06 transcripts (sg_journal.py); no SG reference here')

    def writer(tmp):
        began = time.monotonic()
        if not len(cols['t']):
            write_json(tmp / 'manifest.json', dict(record, result='no_journal_matches', pairs=0))
            return
        order = np.argsort(pairs['t'], kind='stable')
        p = {k: v[order] for k, v in pairs.items()}
        modes, keep, cuts = sg_match.vote(p, duration)
        if keep.sum() < JOURNAL['min_events']:
            write_json(tmp / 'manifest.json', dict(record, result='too_few_pairs', pairs=int(len(p['t'])),
                                                   kept=int(keep.sum())))
            return
        fit = sg_stage.pooled_fit(tape, p, keep, cuts, duration, [SEGMENT], log,
                                  {c['channel']: c['origin_s'] for c in channels})
        curve, ev = fit['curve'], fit['evidence']
        res, weight = fit['solution'].residuals[tape], fit['solution'].weights[tape]
        write_tsv(tmp / 'pairs.tsv.gz', sg_stage.PAIR_FIELDS, [
            dict(channel=int(p['channel'][i]), t=float(p['t'][i]), local_s=float(p['local_s'][i]), g=float(p['g'][i]),
                 d=float(p['d'][i]), ref=int(p['ref'][i]), src=0, seg=0, p=float(p['p'][i]), aligned=0, kept=int(keep[i]),
                 stagger_s=float(fit['stagger'][i]) if np.isfinite(fit['stagger'][i]) else None,
                 stagger_basis=fit['stagger_basis'][i], group=int(fit['pair_group'][i])) for i in range(len(p['t']))],
            compress=True)
        write_tsv(tmp / 'events.tsv', sg_stage.EVENT_FIELDS, [
            dict(e, residual_s=float(res[i]), weight=float(weight[i]),
                 heldout_residual_s=float(fit['heldout'][i]) if np.isfinite(fit['heldout'][i]) else None)
            for i, e in enumerate(fit['events'])])
        write_tsv(tmp / 'stagger.tsv', sg_stage.STAGGER_FIELDS, fit['stagger_rows'])
        write_tsv(tmp / 'blocks.tsv', sg_stage.BLOCK_FIELDS, modes)
        write_json(tmp / 'curve.json', dict(curve.to_json(), b=fit['solution'].b, groups=fit['groups'],
                                            reference_group=fit['reference_group']))
        used = ev.t[weight > 0]
        write_json(tmp / 'manifest.json', dict(
            record, result='curve', pairs=int(len(p['t'])), kept=int(keep.sum()), events=len(fit['events']),
            events_used=int((weight > 0).sum()),
            channels_with_pairs=sorted(int(c) for c in np.unique(p['channel'][keep])),
            reference_channel=fit['reference_channel'], stagger_groups_s=fit['groups'],
            channel_stagger={str(c): v for c, v in sorted(fit['tape_level'].items())},
            capture_offset_flags=fit['capture_offset_flags'], reference_group=fit['reference_group'],
            recorder_cuts_s=cuts, lam=fit['lam'], reference_row='earlier head row (real time; docs/23)',
            rereference_shift_s=fit['rereference_shift_s'], single_row_group=fit['single_row_group'],
            validation=fit['validation'], first_anchor_s=float(used.min()), last_anchor_s=float(used.max()),
            residual=dict(median_abs_s=float(np.median(np.abs(res[weight > 0]))),
                          p90_abs_s=float(np.percentile(np.abs(res[weight > 0]), 90))),
            seconds=round(time.monotonic() - began, 1)))
        log(f"{tape}: journal anchors: {int(keep.sum())}/{len(p['t'])} pairs kept, {len(fit['events'])} events, "
            f"groups {', '.join(f'{g:+.2f}' for g in fit['groups'])} s, tape {used.min() / 3600:.2f}-"
            f"{used.max() / 3600:.2f} h")

    sg_stage.publish(folder, writer)
    return folder if sg_stage.read_json(folder / 'manifest.json').get('result') == 'curve' else None


def substitute(anchors_by_tape, root, log=print):
    """Tapes whose SG anchors have no curve take a journal anchors edition when one can be built."""
    import sg_stage
    out = dict(anchors_by_tape)
    for tape, folder in anchors_by_tape.items():
        manifest = sg_stage.read_json(Path(folder) / 'manifest.json')
        if manifest.get('result') == 'curve':
            continue
        try:
            journal = build_anchors(tape, Path(root) / 'sg_reference' / manifest['reference'], root, log)
        except (ValueError, RuntimeError, FileNotFoundError) as error:
            log(f'{tape}: no journal anchors ({error})')
            continue
        if journal is not None:
            out[tape] = journal
            log(f'{tape}: SG anchors {manifest.get("result")}; using journal anchors {journal.name}')
        else:
            log(f'{tape}: SG anchors {manifest.get("result")}; journal matching found too little')
    return out
