"""Stage 06c: SG anchors for the 30-track tapes, one curve per tape, frozen shared-layout plans.

docs/20_2026-11-25_sg_retime_30track.md. Steps and artifacts (under ``RETIMING_ROOT``):

* ``reference``: ``sg_reference/<edition>/`` the SG words on GET (``reference.npz``) and their
  provenance (``manifest.json``).
* ``anchors``: ``<tape>/sg_anchors/<edition>/`` text pairs, block modes, per-channel/interval
  stagger, the tape event table (duplicates across channels collapsed; the only input to the fit)
  and the pooled preliminary curve.
* ``refine``: ``<tape>/sg_anchors/<anchors>/audio/<edition>/`` GCC-PHAT windows, audio anchors
  and audio-refined staggers.
* ``quindar``: ``<tape>/sg_anchors/<anchors>/quindar/<edition>/`` bursts, NARA matches (a check),
  rate witnesses; the generator calibration in ``sg_quindar/``.
* ``fit``: ``sg_fit/<edition>/`` one joint fit over the selected tapes with shared IA segment
  offsets, a proposed IA revision (never applied to ``catalog/sg_drift``), coverage as the union
  of supported tape time, and per tape a plan in the stage-06 shared-plan layout that
  ``06b_retime.py --quindar-plan`` applies identically to every channel.

Every edition is named by the digest of its inputs (transcript chunk checksums and alignment
state, 05b review checksums, origins, drift map and respeed checksums, settings and code), written
once and reused. A change of any input gives a new edition beside the old one. An edition read
from a transcript that is not fully CTC-aligned, or from a store with a running transcription
session, is ``provisional-``; its plans carry ``status: provisional`` and 06b refuses them.
"""
from __future__ import annotations

import hashlib
import shutil
import time
from pathlib import Path

import numpy as np

import sg_audio
import sg_curve
import sg_match
from sg_inputs import (DRIFT_MAPS, RETIMING_ROOT, STORES, digest, ia_sources, load_words, nara_sources, read_json,
                       read_tsv, sha256_file, store_state, tape_channels, tape_prior, transcript_state, write_json,
                       write_tsv)

VERSION = 'sg-retime-v2'  # v2: CTC-aligned word times; rescue layers withdrawn
CODE = [Path(__file__).with_name(n) for n in ('sg_stage.py', 'sg_inputs.py', 'sg_match.py', 'sg_curve.py', 'sg_audio.py')]
TEXT_SIGMA_S = (0.15, 0.5)  # text event sigma: both word times aligned / otherwise (Whisper)
OPEN_DECISIONS = [
    'Reference stagger group: the map defines GET for the reference group; the other group stays '
    'off GET by its stagger (metadata, no per-channel shift).',
    'IA map feedback: the fitted IA segment offsets are a proposal only; catalog/sg_drift is unchanged.',
    'Rate bounds: max_rate_change_per_hour is provisional until the T510104 hours 11-16 change is confirmed.',
]


def code_sha():
    h = hashlib.sha256()
    for p in CODE:
        h.update(p.read_bytes())
    return h.hexdigest()


class Provisional(RuntimeError):
    pass


def readiness(stores, transcripts):
    """Why an edition would not be final: running sessions on its stores, transcripts not fully
    aligned with the current aligner, provisional upstream editions. Empty: final."""
    reasons = []
    for name in stores:
        state = store_state(STORES[name])
        if state['running']:
            reasons.append(f"{name}: transcription session {', '.join(state['running'])} running")
    pending = [label for label, state in transcripts if state is not None and not state['aligned']]
    if pending:
        reasons.append(f"{len(pending)} transcripts not fully aligned ({', '.join(pending[:4])}"
                       f"{', ...' if len(pending) > 4 else ''})")
    return reasons


def check_ready(reasons, allow_provisional, log=print):
    if not reasons:
        return
    message = '; '.join(reasons)
    if not allow_provisional:
        raise Provisional(f'Inputs are not final ({message}). Wait for the alignment to finish, or pass '
                          '--allow-provisional for a provisional edition.')
    log(f'Provisional edition: {message}')


def edition_name(step, payload, final):
    return f"{'' if final else 'provisional-'}{step}-{digest(payload, 16)}"


def complete(folder):
    path = Path(folder) / 'manifest.json'
    return path.is_file() and read_json(path).get('status') in ('complete', 'provisional')


def publish(folder, writer):
    """Write into a temporary folder and rename it into place (editions are immutable)."""
    folder = Path(folder)
    if complete(folder):
        return False
    tmp = folder.with_name('.tmp-' + folder.name)
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    try:
        writer(tmp)
        if folder.exists():
            shutil.rmtree(folder)
        tmp.rename(folder)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return True


# ------------------------------------------------------------------ reference
def reference_inputs():
    sources = [*nara_sources(), *ia_sources()]
    rows = []
    for s in sources:
        state = transcript_state(Path(s['identity_dir'])) if s.get('identity_dir') else None
        rows.append(dict(kind=s['kind'], name=s['name'], identity=Path(s['identity_dir']).name if s.get('identity_dir') else None,
                         transcript=state, segments=[(g['key'], g['source_start_s'], g['source_end_s'], g['constant_s'])
                                                     for g in s['segments']],
                         respeed_sha256=s.get('respeed_sha256'), audio_sha256=s.get('audio_sha256')))
    return sources, rows


def reference_edition(root=RETIMING_ROOT):
    sources, rows = reference_inputs()
    reasons = readiness(('ia', 'nara'), [(r['name'], r['transcript']) for r in rows])
    payload = dict(version=VERSION, sources=rows, drift_maps_sha256=sha256_file(DRIFT_MAPS), code=code_sha())
    name = edition_name('reference', payload, not reasons)
    return Path(root) / 'sg_reference' / name, sources, payload, reasons


def build_reference(allow_provisional=False, root=RETIMING_ROOT, log=print):
    folder, sources, payload, reasons = reference_edition(root)
    name = folder.name
    if complete(folder):
        return folder
    check_ready(reasons, allow_provisional, log)

    def writer(tmp):
        cache = Path(root) / '_cache' / 'words'
        began = time.monotonic()
        ref, out_sources, segments = sg_match.reference_from_sources(
            sources, lambda s: load_words(Path(s['identity_dir']), cache) if s.get('identity_dir') else None)
        for row, out in zip(payload['sources'], out_sources):
            if out.get('transcript') not in (None, row['transcript']):
                raise RuntimeError(f"{out['name']}: transcript changed while the reference was built (chunks are "
                                   'being aligned); run the step again')
        np.savez_compressed(tmp / 'reference.npz', **ref)
        write_json(tmp / 'manifest.json', dict(
            status='provisional' if reasons else 'complete', provisional_reasons=reasons, edition=name,
            step='reference', version=VERSION, inputs=payload, sources=out_sources, segments=segments,
            source_records=[{k: v for k, v in s.items() if k != 'reel'} for s in sources],
            words=int(len(ref['tok'])), aligned_words=int(ref['aligned'].sum()),
            seconds=round(time.monotonic() - began, 1),
            excluded=dict(rate_only_ia='567/568-AAA have no GET placement',
                          nara_653='653-AAA has no timecode and no respeed record')))
        log(f'Reference {name}: {len(ref["tok"]):,} words ({int(ref["aligned"].sum()):,} aligned) from '
            f'{sum(1 for s in out_sources if s.get("words"))} sources')

    publish(folder, writer)
    return folder


def load_reference(folder):
    with np.load(Path(folder) / 'reference.npz') as z:
        ref = {k: z[k] for k in z.files}
    return ref, read_json(Path(folder) / 'manifest.json')


# ---------------------------------------------------------------- anchors
def anchor_inputs(tape, reference_folder):
    channels = tape_channels(tape)
    rows = []
    for c in channels:
        state = transcript_state(Path(c['identity_dir'])) if c['identity_dir'] and not c['problem'] else None
        c['transcript'] = state
        rows.append(dict(channel=c['channel'], identity=c['identity_id'], transcript=state, review=c['review_sha256'],
                         origin=c['origin_s'], plan=c['plan_sha256'], problem=c['problem']))
    payload = dict(version=VERSION, tape=tape, reference=Path(reference_folder).name, channels=rows,
                   prior=tape_prior(tape), match=sg_match.MATCH, fit=sg_curve.FIT, text_sigma_s=TEXT_SIGMA_S,
                   code=code_sha())
    return channels, payload


def anchors_folder(tape, reference_folder, root=RETIMING_ROOT):
    channels, payload = anchor_inputs(tape, reference_folder)
    ref_manifest = read_json(Path(reference_folder) / 'manifest.json')
    reasons = readiness(('tape',), [(f"CH{c['channel']:02d}", c['transcript']) for c in channels])
    if ref_manifest['status'] != 'complete':
        reasons.append(f"reference {ref_manifest['edition']} is provisional")
    name = edition_name('anchors', payload, not reasons)
    return Path(root) / tape / 'sg_anchors' / name, channels, payload, reasons


def evidence_from_events(tape, duration, events, cuts, sigma=TEXT_SIGMA_S[1], kind='text'):
    return sg_curve.TapeEvidence(
        tape=tape, duration=duration, t=[e['t'] for e in events], g=[e['g'] for e in events],
        sigma=[e.get('sigma_s', sigma) for e in events], bkey=[e.get('bkey') or None for e in events],
        ref_kind=[e['ref_kind'] for e in events], kind=[kind] * len(events), cuts=cuts)


def bprior_of(segments):
    return {s['key']: s['prior_sigma_s'] for s in segments if s['kind'] == 'ia'}


def capture_offset_flags(pairs, origins, groups, tape_level, tolerance=0.3, minimum_s=2.0, window_s=15.0):
    """Channels that hear the same SG words a capture-offset difference of the tape (2-12 s) later
    or earlier than channels of known stagger, more often than at any stagger group: a channel on
    the wrong clock. Curve-free, so an IA map error cannot raise it; all pairs are used (such
    pairs fall outside the vote's keep window). Flagged, never fitted as stagger. Offsets below
    ``minimum_s`` overlap the stagger range and cannot be told apart."""
    values = sorted({round(a - b, 6) for a in origins.values() for b in origins.values() if abs(a - b) >= minimum_s})
    known = {c: v['stagger_s'] for c, v in tape_level.items() if not v['mixed']}
    if not values or not known:
        return []
    heard = list(sg_match._shared(pairs, np.ones(len(pairs['t']), bool)).values())
    out = []
    for c in np.unique(pairs['channel']):
        x = np.asarray(sg_match._diffs(heard, int(c), {j: st for j, st in known.items() if j != c}, window_s))
        if not len(x):
            continue
        on = sum(int((np.abs(x - g) <= tolerance).sum()) for g in groups)
        best = max((int(sum((np.abs(x - (v + g)) <= tolerance).sum() for g in groups)), v) for v in values)
        if best[0] >= sg_match.MATCH['min_stagger_pairs'] and best[0] > on:
            sel = np.min([np.abs(x - (best[1] + g)) for g in groups], axis=0) <= tolerance
            out.append(dict(channel=int(c), median_residual_s=float(np.median(x[sel])), capture_offset_s=best[1],
                            pairs_near_offset=best[0], pairs_in_groups=on))
    return out


def pooled_fit(tape, pairs, keep, cuts, duration, segments, log=print, origins=None):
    """Curve-free stagger per channel and interval, then all channels pooled on one curve."""
    bprior = bprior_of(segments)
    ch = pairs['channel']
    counts = {int(c): int((keep & (ch == c)).sum()) for c in np.unique(ch[keep])}
    reference_channel = max(counts, key=counts.get)
    tape_level, rows = sg_match.relative_stagger(pairs, keep, reference_channel)
    groups = sg_match.cluster_groups(rows)
    curve = None
    for iteration in range(sg_match.MATCH['stagger_iterations']):
        stagger, basis = sg_match.assign_stagger(pairs, keep, rows, tape_level, groups, curve)
        events = sg_match.tape_events(pairs, keep, stagger, segments, TEXT_SIGMA_S)
        evidence = evidence_from_events(tape, duration, events, cuts)
        if len(events) < sg_curve.FIT['min_events']:
            raise ValueError(f'{tape}: {len(events)} text events; too few for a curve')
        lam, sol, validation, heldout = sg_curve.choose_lambda(evidence, bprior)
        curve = sol.curves[tape]
        log(f'  pass {iteration + 1}: {len(events):,} events; stagger groups {", ".join(f"{g:+.3f}" for g in groups)} s; '
            f'{int(np.isfinite(stagger).sum()):,} placed pairs')
    reference_group = int(np.argmin(np.abs(groups)))
    flags = capture_offset_flags(pairs, origins or {}, groups, tape_level)
    for f in flags:
        log(f"  WARNING CH{f['channel']:02d}: shared words at {f['median_residual_s']:+.2f} s, near capture offset "
            f"{f['capture_offset_s']:+.3f} s (placed on local time?)")
    pair_group = np.array([int(np.argmin(np.abs(np.asarray(groups) - s))) if np.isfinite(s) else -1 for s in stagger])
    return dict(events=events, evidence=evidence, solution=sol, curve=curve, lam=lam, validation=validation,
                heldout=heldout, stagger=stagger, stagger_basis=basis, stagger_rows=rows, tape_level=tape_level,
                groups=groups, reference_group=reference_group, reference_channel=reference_channel, pair_group=pair_group,
                capture_offset_flags=flags)


PAIR_FIELDS = ('channel', 't', 'local_s', 'g', 'd', 'ref', 'src', 'seg', 'p', 'aligned', 'kept', 'stagger_s', 'stagger_basis', 'group')
EVENT_FIELDS = ('t', 'g', 'sigma_s', 'aligned', 'residual_s', 'weight', 'heldout_residual_s', 'copies', 'channels',
                'spread_s', 'ref_kind', 'source', 'bkey', 'ref', 'p')
STAGGER_FIELDS = ('channel', 'interval', 'start_s', 'end_s', 'pairs', 'stagger_s', 'mad_s', 'tape_level_s', 'group', 'measured',
                  'mixed')
BLOCK_FIELDS = ('block', 'tc', 'offset', 'slope', 'votes', 'clusters', 'pairs', 'switched', 'sandwiched', 'segment',
                'accepted')


def build_anchors(tape, reference_folder, allow_provisional=False, root=RETIMING_ROOT, log=print):
    folder, channels, payload, reasons = anchors_folder(tape, reference_folder, root)
    if complete(folder):
        return folder
    check_ready(reasons, allow_provisional, log)

    def writer(tmp):
        began = time.monotonic()
        ref, ref_manifest = load_reference(reference_folder)
        segments = ref_manifest['segments']
        prior = payload['prior']
        usable = [c for c in channels if not c['problem']]
        refused = [dict(channel=c['channel'], problem=c['problem']) for c in channels if c['problem']]
        duration = max(c['origin_s'] + c['duration_s'] for c in channels)
        cache = Path(root) / '_cache' / 'words'
        loaded = []
        for c in usable:
            w = load_words(Path(c['identity_dir']), cache)
            if w.meta['digest'] != c['transcript']:
                raise RuntimeError(f"{tape} CH{c['channel']:02d}: transcript changed while the anchors were built "
                                   '(chunks are being aligned); run the step again')
            loaded.append((c['channel'], c['origin_s'], w))
        pairs, stats = sg_match.text_pairs(loaded, ref, prior, duration)
        record = dict(status='provisional' if reasons else 'complete', provisional_reasons=reasons, edition=folder.name,
                      step='anchors', tape=tape, version=VERSION, reference=Path(reference_folder).name,
                      inputs=payload, duration_s=duration, refused_channels=refused, channel_stats=stats,
                      reviews={c['channel']: c['review_sha256'] for c in channels},
                      origins={c['channel']: c['origin_s'] for c in channels})
        if not len(pairs['t']):
            record.update(result='no_sg_reference', pairs=0)
            write_json(tmp / 'manifest.json', record)
            log(f'{tape}: no SG text pairs')
            return
        modes, keep, cuts = sg_match.vote(pairs, duration)
        if keep.sum() < sg_curve.FIT['min_events']:
            record.update(result='too_few_pairs', pairs=int(len(pairs['t'])), kept=int(keep.sum()), blocks=modes)
            write_json(tmp / 'manifest.json', record)
            log(f'{tape}: {int(keep.sum())} kept pairs; no curve')
            return
        fit = pooled_fit(tape, pairs, keep, cuts, duration, segments, log, {c['channel']: c['origin_s'] for c in usable})
        curve, ev = fit['curve'], fit['evidence']
        res = fit['solution'].residuals[tape]
        weight = fit['solution'].weights[tape]
        pair_rows = [dict(channel=int(pairs['channel'][i]), t=float(pairs['t'][i]), local_s=float(pairs['local_s'][i]),
                          g=float(pairs['g'][i]), d=float(pairs['d'][i]), ref=int(pairs['ref'][i]), src=int(pairs['src'][i]),
                          seg=int(pairs['seg'][i]), p=float(pairs['p'][i]), aligned=int(pairs['aligned'][i]), kept=int(keep[i]),
                          stagger_s=float(fit['stagger'][i]) if np.isfinite(fit['stagger'][i]) else None,
                          stagger_basis=fit['stagger_basis'][i], group=int(fit['pair_group'][i]))
                     for i in range(len(pairs['t']))]
        write_tsv(tmp / 'pairs.tsv.gz', PAIR_FIELDS, pair_rows, compress=True)
        event_rows = [dict(e, residual_s=float(res[i]), weight=float(weight[i]),
                           heldout_residual_s=float(fit['heldout'][i]) if np.isfinite(fit['heldout'][i]) else None)
                      for i, e in enumerate(fit['events'])]
        write_tsv(tmp / 'events.tsv', EVENT_FIELDS, event_rows)
        write_tsv(tmp / 'stagger.tsv', STAGGER_FIELDS, fit['stagger_rows'])
        write_tsv(tmp / 'blocks.tsv', BLOCK_FIELDS, modes)
        write_json(tmp / 'curve.json', dict(curve.to_json(), b=fit['solution'].b, groups=fit['groups'],
                                            reference_group=fit['reference_group']))
        used = ev.t[weight > 0]
        support = {str(r): sg_curve.covered(sg_curve.clip_intervals(sg_curve.support_intervals(used, r), 0, duration)) / duration
                   for r in (120, 600)}
        channels_with_pairs = sorted(int(c) for c in np.unique(pairs['channel'][keep]))
        record.update(result='curve', pairs=int(len(pairs['t'])), kept=int(keep.sum()), events=len(fit['events']),
                      aligned_pair_share=float(pairs['aligned'][keep].mean()),
                      events_used=int((weight > 0).sum()), channels_with_pairs=channels_with_pairs,
                      channels_with_100_pairs=sorted(c for c in channels_with_pairs
                                                     if int((keep & (pairs['channel'] == c)).sum()) >= 100),
                      reference_channel=fit['reference_channel'], stagger_groups_s=fit['groups'],
                      channel_stagger={str(c): v for c, v in sorted(fit['tape_level'].items())},
                      capture_offset_flags=fit['capture_offset_flags'],
                      reference_group=fit['reference_group'], recorder_cuts_s=cuts, lam=fit['lam'],
                      validation=fit['validation'], supported_fraction=support,
                      prior_offset_s=dict(at_start=float(curve.get(np.array([curve.first_s]))[0] - (prior['get_at_zero_s'] + curve.first_s * prior['rate'])),
                                          at_end=float(curve.get(np.array([curve.last_s]))[0] - (prior['get_at_zero_s'] + curve.last_s * prior['rate']))),
                      residual=dict(median_abs_s=float(np.median(np.abs(res[weight > 0]))),
                                    p90_abs_s=float(np.percentile(np.abs(res[weight > 0]), 90))),
                      rate_range=[float(curve.rate(np.array([curve.first_s, curve.last_s])).min()),
                                  float(curve.rate(np.linspace(curve.first_s, curve.last_s, 500)).max())],
                      seconds=round(time.monotonic() - began, 1))
        record['rate_range'][0] = float(curve.rate(np.linspace(curve.first_s, curve.last_s, 500)).min())
        write_json(tmp / 'manifest.json', record)
        h = record['validation']['heldout'].get('all', {})
        log(f"{tape}: {record['kept']:,}/{record['pairs']:,} pairs kept on {len(channels_with_pairs)} channels, "
            f"{record['events_used']:,} events; residual median {record['residual']['median_abs_s']:.3f} s; "
            f"held-out median {h.get('median_abs_s', float('nan')):.3f} s, p95 {h.get('p95_abs_s', float('nan')):.2f} s; "
            f"groups {', '.join(f'{g:+.2f}' for g in fit['groups'])} s ({record['seconds']} s)")

    publish(folder, writer)
    return folder


def load_anchors(folder):
    folder = Path(folder)
    manifest = read_json(folder / 'manifest.json')
    if manifest.get('result') != 'curve':
        return manifest, None, None, None
    curve_json = read_json(folder / 'curve.json')
    curve = sg_curve.Curve.from_json(curve_json)
    pairs = read_tsv(folder / 'pairs.tsv.gz')
    events = read_tsv(folder / 'events.tsv')
    return manifest, curve, curve_json, (pairs, events)


def _f(x):
    return float(x) if x not in ('', None) else np.nan


# -------------------------------------------------------------- audio refine
def build_refine(tape, anchors, root=RETIMING_ROOT, log=print, workers=None):
    manifest, curve, curve_json, tables = load_anchors(anchors)
    if curve is None:
        log(f'{tape}: anchors edition has no curve ({manifest.get("result")}); nothing to refine')
        return None
    settings = dict(sg_audio.AUDIO, workers=workers or sg_audio.AUDIO['workers'])
    ref_manifest = read_json(Path(root) / 'sg_reference' / manifest['reference'] / 'manifest.json')
    g_lo, g_hi = float(curve.get(np.array([0.0]))[0]), float(curve.get(np.array([manifest['duration_s']]))[0])
    refs, audio_ids = [], []
    for s in ref_manifest['source_records']:
        if not any(seg['get_end_s'] >= g_lo and seg['get_start_s'] <= g_hi for seg in s['segments']):
            continue
        if not Path(s['audio']).is_file():
            log(f'  reference audio missing: {s["audio"]}')
            continue
        if s['kind'] == 'ia':
            maps = read_json(DRIFT_MAPS)
            s = dict(s, reel=next(r for r in maps['reels'] if r['reel'] == s['name']))
        st = Path(s['audio']).stat()
        audio_ids.append((s['name'], st.st_size, st.st_mtime_ns))
        refs.append(sg_audio.ReferenceAudio(s))
    payload = dict(version=VERSION, anchors=Path(anchors).name, audio=audio_ids, settings={k: v for k, v in settings.items() if k != 'workers'},
                   code=code_sha())
    final = manifest['status'] == 'complete'
    folder = Path(anchors) / 'audio' / edition_name('audio', payload, final)
    if complete(folder):
        return folder
    channels = [c for c in tape_channels(tape) if not c['problem']]
    for c in channels:
        if c['review_sha256'] != manifest['reviews'].get(str(c['channel']), manifest['reviews'].get(c['channel'])):
            raise ValueError(f'{tape} CH{c["channel"]:02d}: 05b review changed since the anchors edition; rebuild anchors')

    def writer(tmp):
        began = time.monotonic()
        pairs, _ = tables
        P = {k: np.array([_f(r[k]) for r in pairs]) for k in ('t', 'stagger_s', 'kept', 'group', 'channel')}
        keep = P['kept'] > 0
        groups = curve_json['groups']
        windows = sg_audio.choose_windows(dict(t=P['t'], channel=P['channel'].astype(int)), keep, P['stagger_s'],
                                          groups, P['group'].astype(int), settings)
        log(f'{tape}: {len(windows):,} channel windows x {len(refs)} references')
        results = sg_audio.refine(windows, channels, curve, refs, settings, log, curve_json.get('b'))
        events, staggers = sg_audio.audio_events(results, curve_json['reference_group'], settings)
        write_tsv(tmp / 'windows.tsv', ('channel', 'group', 't_ref', 'stagger_s', 'predicted_get', 'ref_source', 'ref_kind',
                                        'ref_segment', 'bkey', 'rate', 'lag_s', 'ratio', 'second_ratio', 'polarity',
                                        'accepted', 'error'), results)
        write_tsv(tmp / 'events.tsv', ('t', 'g', 'sigma_s', 'delta_s', 'channels', 'agreement_s', 'ratio', 'ref_kind',
                                       'source', 'bkey'), events)
        write_tsv(tmp / 'stagger.tsv', ('channel', 't_ref', 'text_stagger_s', 'audio_stagger_s', 'source'), staggers)
        accepted = [r for r in results if r.get('accepted')]
        write_json(tmp / 'manifest.json', dict(
            status='complete' if final else 'provisional', edition=folder.name, step='audio', tape=tape,
            inputs=payload, windows=len(results), accepted=len(accepted), events=len(events),
            stagger_refinements=len(staggers),
            delta_s=dict(median=float(np.median([e['delta_s'] for e in events])) if events else None,
                         p90_abs=float(np.percentile(np.abs([e['delta_s'] for e in events]), 90)) if events else None),
            seconds=round(time.monotonic() - began, 1)))
        log(f'{tape}: {len(accepted):,}/{len(results):,} windows accepted -> {len(events):,} audio anchors, '
            f'{len(staggers):,} stagger refinements')

    publish(folder, writer)
    return folder


# -------------------------------------------------------------------- Quindar
def calibration(root=RETIMING_ROOT, log=print, workers=8):
    """NARA generator calibration on IRIG time; scans cached per exact mix file."""
    sources = nara_sources()
    ids = []
    for s in sources:
        st = Path(s['audio']).stat()
        ids.append(dict(name=s['name'], respeed=s['respeed_sha256'], size=st.st_size, mtime_ns=st.st_mtime_ns))
    payload = dict(version=VERSION, sources=ids, settings=sg_audio.QUINDAR, code=code_sha())
    path = Path(root) / 'sg_quindar' / f"calibration-{digest(payload, 16)}.json"
    if path.is_file():
        return read_json(path), sources
    cache = Path(root) / '_cache' / 'quindar' / 'nara'
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(workers) as pool:
        scans = list(pool.map(lambda pair: sg_audio.cached_scan(pair[0]['audio'], cache, pair[1]), zip(sources, ids)))
    events = {s['name']: e for s, e in zip(sources, scans)}
    cal = sg_audio.generator_calibration(events)
    bursts = {s['name']: sg_audio.sg_bursts(s, e) for s, e in zip(sources, scans)}
    record = dict(payload=payload, calibration=cal, bursts=bursts)
    write_json(path, record)
    m = cal['mission']
    log(f"Quindar generator (NARA, IRIG time): key {m['key_hz']:.2f} Hz, unkey {m['unkey_hz']:.2f} Hz from {len(m['tapes'])} tapes")
    return record, sources


def build_quindar(tape, anchors, root=RETIMING_ROOT, log=print, workers=8, include_ia=False):
    manifest, curve, curve_json, _ = load_anchors(anchors)
    if curve is None:
        log(f'{tape}: anchors edition has no curve; nothing to match')
        return None
    cal, _ = calibration(root, log, workers)
    channels = [c for c in tape_channels(tape) if not c['problem']]
    ids = {c['channel']: dict(review=c['review_sha256'], size=Path(c['wav']).stat().st_size,
                              mtime_ns=Path(c['wav']).stat().st_mtime_ns) for c in channels}
    payload = dict(version=VERSION, anchors=Path(anchors).name, channels=ids, calibration=cal['payload'],
                   include_ia=include_ia, settings=sg_audio.QUINDAR, code=code_sha())
    final = manifest['status'] == 'complete'
    folder = Path(anchors) / 'quindar' / edition_name('quindar', payload, final)
    if complete(folder):
        return folder

    def writer(tmp):
        began = time.monotonic()
        cache = Path(root) / '_cache' / 'quindar' / 'tape' / tape
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(workers) as pool:
            scans = list(pool.map(lambda c: sg_audio.cached_scan(c['wav'], cache, ids[c['channel']]), channels))
        bursts = sg_audio.tape_bursts({c['channel']: s for c, s in zip(channels, scans)}, channels)
        references = [b for rows in cal['bursts'].values() for b in rows]
        if include_ia:
            maps = read_json(DRIFT_MAPS)
            ia_cache = Path(root) / '_cache' / 'quindar' / 'ia'
            for s in ia_sources():
                st = Path(s['audio']).stat() if Path(s['audio']).is_file() else None
                if st is None:
                    continue
                events = sg_audio.cached_scan(s['audio'], ia_cache, dict(name=s['name'], sha256=s['audio_sha256']))
                references.extend(sg_audio.sg_bursts(s, events))
        bursts, witnesses = sg_audio.match_quindar(bursts, curve, curve_json['groups'], references,
                                                   cal['calibration']['mission'], b=curve_json.get('b'))
        check, totals = sg_audio.quindar_check(bursts)
        write_tsv(tmp / 'bursts.tsv', ('channel', 't', 'frequency_hz', 'family', 'curve_rate', 'matched_group', 'residual_s',
                                       'ref_source', 'ref_kind', 'ref_frequency_hz'), bursts)
        write_tsv(tmp / 'witnesses.tsv', ('t', 'rate', 'family', 'copies', 'basis', 'sigma'), witnesses)
        write_tsv(tmp / 'check.tsv', ('channel', 'group', 'matches', 'median_s', 'mad_s', 'within_0_25_s'), check)
        matched = [x for x in bursts if x.get('ref_kind') == 'nara']
        ratio = [x['frequency_hz'] / x['ref_frequency_hz'] / x['curve_rate'] for x in matched]
        write_json(tmp / 'manifest.json', dict(
            status='complete' if final else 'provisional', edition=folder.name, step='quindar', tape=tape,
            inputs=payload, **totals, witnesses=len(witnesses),
            nara_pitch_over_curve_rate=dict(median=float(np.median(ratio)), mad=float(np.median(np.abs(np.array(ratio) - np.median(ratio)))))
            if ratio else None, seconds=round(time.monotonic() - began, 1)))
        log(f"{tape}: {len(bursts):,} bursts, {totals['matched']:,} matched to SG, {len(witnesses):,} rate witnesses")

    publish(folder, writer)
    return folder


def latest_child(anchors, step):
    """The newest complete audio/quindar edition under an anchors edition (None if none)."""
    base = Path(anchors) / step
    found = [p for p in base.iterdir() if p.is_dir() and not p.name.startswith('.tmp-') and complete(p)] if base.is_dir() else []
    return max(found, key=lambda p: p.stat().st_mtime) if found else None


# ------------------------------------------------------------------- joint fit
def tape_evidence(tape, anchors):
    manifest, curve, curve_json, tables = load_anchors(anchors)
    if curve is None:
        return None, manifest, {}
    _, events = tables
    rows = [dict(t=_f(e['t']), g=_f(e['g']), ref_kind=e['ref_kind'], bkey=e['bkey'] or None, sigma_s=_f(e['sigma_s']),
                 kind='text') for e in events]
    used = dict(anchors=Path(anchors).name)
    audio = latest_child(anchors, 'audio')
    if audio:
        for e in read_tsv(audio / 'events.tsv'):
            rows.append(dict(t=_f(e['t']), g=_f(e['g']), ref_kind=e['ref_kind'], bkey=e['bkey'] or None,
                             sigma_s=_f(e['sigma_s']), kind='audio'))
        used['audio'] = audio.name
    rate = dict(t=[], r=[], s=[])
    quindar = latest_child(anchors, 'quindar')
    if quindar:
        for w in read_tsv(quindar / 'witnesses.tsv'):
            rate['t'].append(_f(w['t']))
            rate['r'].append(_f(w['rate']))
            rate['s'].append(_f(w['sigma']))
        used['quindar'] = quindar.name
    ev = sg_curve.TapeEvidence(tape=tape, duration=manifest['duration_s'], t=[r['t'] for r in rows], g=[r['g'] for r in rows],
                               sigma=[r['sigma_s'] for r in rows], bkey=[r['bkey'] for r in rows],
                               ref_kind=[r['ref_kind'] for r in rows], kind=[r['kind'] for r in rows],
                               cuts=curve_json['cuts_s'], rate_t=rate['t'], rate_r=rate['r'], rate_sigma=rate['s'])
    return ev, manifest, dict(used, curve=curve_json)


def plan_record(tape, curve, evidence, manifest, extras, validation, b, fit_name, final, duration):
    """A plan in the stage-06 shared layout (``quindar_tape.load_shared_plan`` / ``target_map``)."""
    channels = tape_channels(tape)
    references, targets, origins = [], [], set()
    for c in channels:
        review = read_json(c['review_path'])
        st = Path(c['wav']).stat()
        references.append(dict(id=f"{tape}:CH{c['channel']:02d}", channel=c['channel'], path=c['wav'], bytes=st.st_size,
                               mtime_ns=st.st_mtime_ns, review_sha256=c['review_sha256'], start_seconds=c['origin_s']))
        targets.append(dict(review['source'], flutter_map_sha256=review.get('map_sha256')))
        origins.add(review['source']['absolute_start'] - review['source'].get('acquisition_start_seconds', review['source']['start_seconds']))
    if len(origins) != 1:
        raise ValueError(f'{tape}: channels disagree on the tape-clock origin')
    groups = extras['curve']['groups']
    ref_group = extras['curve']['reference_group']
    return dict(
        schema=2, status='complete' if final else 'provisional', input_stage='defluttered', tape=tape,
        scope='one SG-anchored tape-motion curve applied identically to every channel of this tape',
        method='sg_text_audio_pspline', version=VERSION, fit_edition=fit_name, evidence_editions=extras,
        origin_filename_seconds=origins.pop(), input_duration_seconds=duration,
        complete_defluttered_coverage=all(not c['problem'] for c in channels) and bool(channels),
        reference_count=len(references), references=references, targets=targets, clock_anchor_source=None,
        synchronization_claim=False,
        get_placement=dict(rule='GET = output seconds + segment get_equals_output_plus_s, for the reference stagger group; '
                                'a channel in another group is that stagger later (stagger = channel - reference)',
                           segments=curve.segments(duration), reference_group=ref_group,
                           stagger_groups_s=groups, reference_channel=manifest['reference_channel']),
        support=validation['support'], heldout=validation['heldout'], residual=validation['residual'],
        smoothing=dict(lam=validation['lam'], basis=validation['lambda_basis'], rate_wiggle=validation['rate_wiggle'],
                       rate_problems=validation['rate_problems'], rate_review=validation['rate_review']),
        ia_segment_offsets_s={k: v for k, v in b.items() if any(k == x for x in evidence.bkey)},
        timing_offsets_policy='Capture offsets (stage 02) are in the canonical origins and are preserved; stagger is '
                              'metadata. One inverse map for every channel; no channel is fitted, anchored or bypassed alone.',
        time_basis='Canonical post-05b tape seconds (review start_seconds + local seconds)',
        review_flags=dict(rate=validation['rate_review'], capture_offset=manifest.get('capture_offset_flags', []),
                          mixed_channels=sorted(int(c) for c, v in manifest.get('channel_stagger', {}).items() if v.get('mixed')),
                          recorder_cuts_s=list(curve.cuts)),
        open_decisions=OPEN_DECISIONS, listening_approved=False,
        caveats=['Before the first and after the last position anchor the rate is held; gaps between anchors are '
                 'modelled (rate ramps), see support.extrapolated_s and support.gaps.'])


def build_fit(tapes, root=RETIMING_ROOT, log=print, allow_provisional=False, anchors_by_tape=None):
    """Joint fit over ``tapes`` (their current anchors editions plus the newest audio/quindar children)."""
    evidences, manifests, extras, bypassed = {}, {}, {}, {}
    for tape, anchors in anchors_by_tape.items():
        ev, manifest, extra = tape_evidence(tape, anchors)
        if ev is None or len(ev.t) < sg_curve.FIT['min_events']:
            log(f'{tape}: no curve ({manifest.get("result")}); bypassed')
            bypassed[tape] = dict(result=manifest.get('result'), duration_s=manifest.get('duration_s'), anchors=Path(anchors).name)
            continue
        evidences[tape], manifests[tape], extras[tape] = ev, manifest, extra
    if not evidences:
        raise ValueError('No tape has SG evidence')
    final = all(m['status'] == 'complete' for m in manifests.values())
    if not final and not allow_provisional:
        raise Provisional('Some anchors editions are provisional; pass --allow-provisional for a provisional fit')
    ref_name = {m['reference'] for m in manifests.values()}
    if len(ref_name) != 1:
        raise ValueError(f'Tapes use different SG reference editions: {sorted(ref_name)}')
    ref_manifest = read_json(Path(root) / 'sg_reference' / ref_name.pop() / 'manifest.json')
    bprior = bprior_of(ref_manifest['segments'])
    payload = dict(version=VERSION, tapes={t: extras[t] | dict(curve=None) for t in sorted(evidences)}, bypassed=bypassed, fit=sg_curve.FIT,
                   code=code_sha())
    name = edition_name('fit', payload, final)
    folder = Path(root) / 'sg_fit' / name
    if complete(folder):
        return folder

    def writer(tmp):
        began = time.monotonic()
        lambdas, validations, heldouts = {}, {}, {}
        nara_tapes = [t for t, ev in evidences.items() if (ev.ref_kind == 'nara').sum() >= sg_curve.FIT['min_nara_events']]
        for tape in nara_tapes:
            lambdas[tape], _, validations[tape], heldouts[tape] = sg_curve.choose_lambda(evidences[tape], bprior)
        # IA-only tapes take the NARA tapes' median choice, once enough NARA tapes are fitted.
        learned = float(10 ** np.median(np.log10([validations[t]['cv_chosen_lam'] for t in nara_tapes])))             if len(nara_tapes) >= sg_curve.FIT['min_nara_tapes'] else None
        for tape, ev in evidences.items():
            if tape not in lambdas:
                lambdas[tape], _, validations[tape], heldouts[tape] = sg_curve.choose_lambda(ev, bprior, fixed=learned)
        for tape in evidences:
            validation = validations[tape]
            h = validation['heldout'].get('all', {})
            log(f"  {tape}: lambda {lambdas[tape]:g} ({validation['lambda_basis']}); held-out median {h.get('median_abs_s', float('nan')):.3f} s, "
                f"p95 {h.get('p95_abs_s', float('nan')):.2f} s")
        sol = sg_curve.solve(list(evidences.values()), lambdas, bprior)
        coverage_rows, b_rows = [], []
        for k in sorted(sol.b):
            tapes_k = sorted(t for t, ev in evidences.items() if k in set(ev.bkey))
            n = sum(int(sum(1 for x in evidences[t].bkey if x == k)) for t in tapes_k)
            seg = next(g for g in ref_manifest['segments'] if g['key'] == k)
            b_rows.append(dict(key=k, offset_s=sol.b[k], se_s=sol.b_se.get(k), prior_sigma_s=bprior.get(k), events=n,
                               tapes=tapes_k, constant_s=seg['constant_s'], proposed_constant_s=seg['constant_s'] - sol.b[k]))
        write_tsv(tmp / 'ia_offsets.tsv', ('key', 'offset_s', 'se_s', 'prior_sigma_s', 'events', 'tapes', 'constant_s',
                                           'proposed_constant_s'), b_rows)
        write_json(tmp / 'ia_segment_revision.json', dict(
            status='proposed', applied=False, drift_maps_sha256=sha256_file(DRIFT_MAPS),
            rule='the drift map places IA audio at Phi + offset_s; proposed constant_s = constant_s - offset_s. '
                 'Review before any revision of catalog/sg_drift',
            segments=b_rows))
        total_s = 0.0
        union = {120: 0.0, 600: 0.0}
        for tape, ev in evidences.items():
            curve = sol.curves[tape]
            w = sol.weights[tape]
            res = sol.residuals[tape]
            duration = manifests[tape]['duration_s']
            used = ev.t[w > 0]
            sup = {r: sg_curve.clip_intervals(sg_curve.support_intervals(used, r), 0, duration) for r in (120, 600)}
            gaps = [[a[1], b[0]] for a, b in zip(sup[600][:-1], sup[600][1:])]
            support = dict(intervals_600_s=sup[600], fraction_120=sg_curve.covered(sup[120]) / duration,
                           fraction_600=sg_curve.covered(sup[600]) / duration, gaps=gaps,
                           extrapolated_s=[[0.0, curve.first_s], [curve.last_s, duration]],
                           first_anchor_s=curve.first_s, last_anchor_s=curve.last_s)
            total_s += duration
            for r in (120, 600):
                union[r] += sg_curve.covered(sup[r])
            residual = {}
            for label in ('text', 'audio'):
                sel = (ev.kind == label) & (w > 0)
                if sel.any():
                    residual[label] = dict(events=int(sel.sum()), median_abs_s=float(np.median(np.abs(res[sel]))),
                                           p90_abs_s=float(np.percentile(np.abs(res[sel]), 90)))
            validation = dict(support=support, heldout=validations[tape]['heldout'], residual=residual,
                              lam=lambdas[tape], lambda_basis=validations[tape]['lambda_basis'],
                              rate_problems=sg_curve.rate_violations(curve), rate_wiggle=sg_curve.rate_wiggle(curve),
                              rate_review=sg_curve.rate_violations(curve, review=True))
            out = tmp / 'tapes' / tape
            out.mkdir(parents=True)
            grid, output, speed = curve.output_map(duration)
            np.savez_compressed(out / 'timing_map.npz', input_seconds=grid, output_seconds=output, recorded_speed_ratio=speed)
            anchors = [dict(input_seconds=float(t), get_s=float(g), kind=k, ref_kind=rk, residual_s=float(r), weight=float(x))
                       for t, g, k, rk, r, x in zip(ev.t, ev.g, ev.kind, ev.ref_kind, res, w) if x > 0]
            anchors.sort(key=lambda a: a['input_seconds'])
            model = dict(schema=1, method='sg_text_audio_pspline', curve=curve.to_json(), anchors=anchors,
                         kernel_sigma_seconds=float(sg_curve.FIT['knot_spacing_s'] * max(1.0, lambdas[tape]) ** (1 / 6)),
                         smoothing_note='kernel_sigma_seconds is the equivalent width of the P-spline penalty (knot spacing x lambda^(1/6))',
                         chosen_validation=validations[tape]['heldout'], support=support,
                         input_duration_seconds=duration, output_duration_seconds=float(output[-1]),
                         edge_policy='Rate held before the first and after the last position anchor',
                         gap_policy='Rate ramps across gaps (third-difference penalty)')
            write_json(out / 'curve.json', model)
            plan = plan_record(tape, curve, ev, manifests[tape], extras[tape], validation, sol.b, name, final, duration)
            plan['artifacts'] = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in out.iterdir()
                                 if p.name in ('curve.json', 'timing_map.npz')}
            write_json(out / 'plan.json', plan)
            coverage_rows.append(dict(tape=tape, duration_s=duration, fraction_120=support['fraction_120'],
                                      fraction_600=support['fraction_600'], first_anchor_s=curve.first_s,
                                      last_anchor_s=curve.last_s, text_events=int(((ev.kind == 'text') & (w > 0)).sum()),
                                      audio_events=int(((ev.kind == 'audio') & (w > 0)).sum()), rate_witnesses=len(ev.rate_t),
                                      heldout_median_s=validations[tape]['heldout'].get('all', {}).get('median_abs_s'),
                                      heldout_p95_s=validations[tape]['heldout'].get('all', {}).get('p95_abs_s'),
                                      rate_review='; '.join(validation['rate_review']),
                                      capture_offset_flags=','.join(f"CH{f['channel']:02d}" for f in manifests[tape].get('capture_offset_flags', [])),
                                      recorder_cuts=len(curve.cuts),
                                      reference_group_s=extras[tape]['curve']['groups'][extras[tape]['curve']['reference_group']],
                                      stagger_groups_s=extras[tape]['curve']['groups']))
        write_tsv(tmp / 'coverage.tsv', ('tape', 'duration_s', 'fraction_120', 'fraction_600', 'first_anchor_s', 'last_anchor_s',
                                         'text_events', 'audio_events', 'rate_witnesses', 'heldout_median_s', 'heldout_p95_s',
                                         'rate_review', 'capture_offset_flags', 'recorder_cuts', 'reference_group_s',
                                         'stagger_groups_s'), coverage_rows)
        write_json(tmp / 'manifest.json', dict(
            status='complete' if final else 'provisional', edition=name, step='fit', version=VERSION, inputs=payload,
            reference=ref_manifest['edition'], tapes=sorted(evidences), lambdas=lambdas, learned_lambda=learned,
            bypassed=bypassed,
            coverage=dict(tape_seconds=total_s, union_within_120_s=union[120] / total_s, union_within_600_s=union[600] / total_s,
                          all_tape_seconds=total_s + sum(b['duration_s'] or 0.0 for b in bypassed.values()),
                          all_union_within_120_s=union[120] / (total_s + sum(b['duration_s'] or 0.0 for b in bypassed.values())),
                          all_union_within_600_s=union[600] / (total_s + sum(b['duration_s'] or 0.0 for b in bypassed.values())),
                          rule='union of supported tape time per tape (any channel); all_* also count the bypassed tapes'),
            ia_segments=len(b_rows), seconds=round(time.monotonic() - began, 1)))
        log(f'Fit {name}: {len(evidences)} tapes, {len(b_rows)} IA segment offsets; supported within 600 s: '
            f'{union[600] / total_s:.1%} of fitted tape time')

    publish(folder, writer)
    return folder
