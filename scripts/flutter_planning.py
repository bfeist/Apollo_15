"""Dense, resumable regional deflutter planning for one canonical tape."""
import hashlib
import html
import json
import os
import re
import time
import warnings
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from multiprocessing import get_context
from pathlib import Path

import numpy as np
import soundfile as sf

from audio_processing import DEFAULT_BROADBAND_CAP_DB, TONAL_CLEANUP_METHOD
from flutter_plan_store import load_saved_plan
from flutter_policy import (METHODOLOGY, LIMITS, policy_digest, implementation_digest,
                            shifted_curve, evidence_strength, numeric_pass)
from io_metrics import read_bytes as os_read_bytes, difference as io_difference
from flutter_tracking import (RATE, beeps, burst_track, discover, evaluate_events, harmonic_map,
                              signed_map, spans, trajectory)

SCHEMA = 1
STAMP = re.compile(r'_(\d+-\d+-\d+-\d+)_(\d+-\d+-\d+-\d+)\.wav$', re.I)
CHANNEL = re.compile(r'_CH(\d+)_', re.I)


def write_json(path, value):
    temporary = path.with_suffix(path.suffix+'.partial')
    temporary.write_text(json.dumps(value, indent=2, allow_nan=False), encoding='utf-8')
    temporary.replace(path)


def seconds(value):
    day, hour, minute, second = map(int, value.split('-'))
    if day < 0 or not 0 <= hour <= 24 or not 0 <= minute < 60 or not 0 <= second < 60:
        raise ValueError('Invalid filename timestamp')
    return day*86400+hour*3600+minute*60+second


def inventory(folder):
    """One canonical mono WAV per channel; filename clocks preserve offsets."""
    folder = Path(folder).resolve()
    if not folder.is_dir():
        raise ValueError('Tape input must be a canonical tape folder')
    if list(folder.rglob('*.partial')):
        raise ValueError('Tape contains incomplete .partial files')
    rows, seen, excluded = [], set(), []
    files = sorted(p for p in folder.rglob('*') if p.suffix.lower() == '.wav')
    for path in files:
        ch, stamp = CHANNEL.search(path.name), STAMP.search(path.name)
        # Some deliveries include an extra recording with an explicitly empty
        # channel label. Keep it unassigned without blocking numbered channels.
        if not ch and stamp and re.fullmatch(r'A15_T\d+_HR\d+[UL]_', path.name[:stamp.start()], re.I):
            reason = 'Unlabelled recording has no channel number; excluded from channel processing'
            stat = path.stat()
            excluded.append(dict(path=str(path), bytes=stat.st_size, mtime_ns=stat.st_mtime_ns, reason=reason))
            warnings.warn(f'{reason}: {path.name}', stacklevel=2)
            continue
        if not ch or not stamp:
            raise ValueError(f'Expected a timestamped canonical channel WAV: {path.name}')
        channel = int(ch[1])
        if not 1 <= channel <= 30 or channel in seen:
            raise ValueError('Use one concatenated file per channel, numbered 1 through 30')
        seen.add(channel)
        info, stat = sf.info(path), path.stat()
        if info.channels != 1 or not 6000 <= info.samplerate <= 48000 or info.samplerate % 1000:
            raise ValueError(f'Unsupported mono/rate format: {path}')
        start, end = seconds(stamp[1]), seconds(stamp[2])
        if end <= start or abs(info.duration-(end-start)) > 1:
            raise ValueError(f'Filename range disagrees with audio duration: {path}')
        head = re.search(r'_(HR\d+[UL])_', path.name)
        rows.append(dict(channel=channel, path=str(path), head=head[1] if head else None,
                         bytes=stat.st_size, mtime_ns=stat.st_mtime_ns,
                         sample_rate=info.samplerate, frames=info.frames,
                         absolute_start=start, duration_seconds=info.duration,
                         role='timing_only' if 'IRIG' in path.name.upper() else 'reference_and_target'))
    if not rows:
        raise ValueError('No canonical WAV channels found')
    origin = min(r['absolute_start'] for r in rows)
    for r in rows:
        r['start_seconds'] = r['absolute_start']-origin
        r['end_seconds'] = r['start_seconds']+r['duration_seconds']
    result = dict(tape=folder.name, input=str(folder), origin_filename_seconds=origin,
                time_basis='Canonical filename timeline; not authenticated GET or proven head alignment',
                duration_seconds=max(r['end_seconds'] for r in rows),
                missing_channels=sorted(set(range(1, 31))-seen), channels=sorted(rows, key=lambda r: r['channel']))
    if excluded:
        result['excluded_files'] = excluded
    return result


def read_channel(row, start, end):
    path = Path(row['path'])
    def unchanged():
        s = path.stat()
        return s.st_size == row['bytes'] and s.st_mtime_ns == row['mtime_ns']
    if not unchanged():
        raise ValueError(f'Source changed since inventory: {path}')
    sr = row['sample_rate']
    if start < row['start_seconds'] or end > row['end_seconds']+1/sr:
        raise ValueError('Channel does not cover the analysis interval')
    with sf.SoundFile(path) as f:
        f.seek(round((start-row['start_seconds'])*sr))
        x = f.read(round((end-start)*sr), dtype='float64')
    if len(x) != round((end-start)*sr) or not np.isfinite(x).all() or not unchanged():
        raise ValueError(f'Changed, short or non-finite source read: {path}')
    return x, sr, hashlib.sha256(x.tobytes()).hexdigest()


def make_regions(inv, start=0, duration=None, region_seconds=60, context_seconds=4):
    end = inv['duration_seconds'] if duration is None else min(inv['duration_seconds'], start+duration)
    if not np.isfinite([start, end, region_seconds, context_seconds]).all() or not 0 <= start < end:
        raise ValueError('Invalid analysis range')
    if not 15 <= region_seconds <= 120 or not 2 <= context_seconds <= 15:
        raise ValueError('Use 15-120 second regions and 2-15 seconds of context')
    # Channel starts/ends are mandatory boundaries; never collapse a missing span.
    cuts = {start, end}
    cuts.update(float(v) for v in np.arange(start+region_seconds, end, region_seconds))
    cuts.update(r[k] for r in inv['channels'] for k in ('start_seconds', 'end_seconds')
                if start < r[k] < end)
    edges = sorted(cuts)
    result = []
    for i, (a, b) in enumerate(zip(edges, edges[1:])):
        covering = [r for r in inv['channels'] if r['role'] != 'timing_only'
                    and r['start_seconds'] <= a and r['end_seconds'] >= b]
        lower = max((r['start_seconds'] for r in covering), default=0.)
        upper = min((r['end_seconds'] for r in covering), default=inv['duration_seconds'])
        result.append(dict(id=f'r{i:05d}', start_seconds=a, end_seconds=b,
                           analysis_start=max(lower, a-context_seconds),
                           analysis_end=min(upper, b+context_seconds), status='pending'))
    return result


def loss(metrics):
    values = [m['residual_ratio'] for b in metrics for m in b['margins']]
    return float(np.median(values)) if values else None


def split_events(events, region):
    start = region['analysis_start']
    events = sorted((b for b in events if b['start'] >= region['start_seconds']-start+2
                     and b['end'] <= region['end_seconds']-start-2), key=lambda b: b['start'])
    separated = []
    for event in events:
        if not separated or event['start']-separated[-1]['start'] >= LIMITS['independent_window_seconds']:
            separated.append(event)
    events = separated
    # Split by event order, so an early cluster still has genuinely unused events.
    n = max(1, len(events)//2)
    return events[:n], events[n:]


def fair_shortlist(candidates, screening, limit):
    """Greedy coverage of target winners; a channel number never sets priority."""
    eligible = {c['key']: c for c in candidates if c['eligible']}
    scores = {}
    for s in screening:
        if s['proxy_loss'] is not None:
            scores.setdefault(s['target_channel'], []).append(s)
    winners = {ch: min(rows, key=lambda r: (r['proxy_loss'], r['key']))['key'] for ch, rows in scores.items()}
    chosen = []
    while eligible and len(chosen) < limit:
        best = max(eligible.values(), key=lambda c: (sum(k == c['key'] for k in winners.values()), c['rank'], c['key']))
        chosen.append(best)
        eligible.pop(best['key'])
        winners = {ch: k for ch, k in winners.items() if k != best['key']}
    return chosen, dict(limit=limit, eligible_candidates=len(chosen)+len(eligible),
                        exhausted=bool(eligible), targets_without_winner=sorted(winners),
                        untried_candidates=sorted(eligible),
                        interpretation='Budget exhaustion is not exhausted evidence; increase reference-limit for calibration before freezing holdouts')


def attach_survey(inv, path):
    from carrier_survey import load_survey
    from flutter_policy import digest
    index, rows = load_survey(path)
    if Path(index['source']).resolve() != Path(inv['input']).resolve():
        raise ValueError('Carrier survey belongs to different originals')
    expected = {r['path'] for r in inv['channels']} | {r['path'] for r in inv.get('excluded_files', [])}
    if {r['source']['path'] for r in rows} != expected:
        raise ValueError('Carrier survey must cover every canonical and unlabelled original')
    numbered = {r['path']: r for r in inv['channels']}
    extra = []
    for i, scan in enumerate(rows):
        source = scan['source']
        row = numbered.get(source['path'])
        if row is None:
            stamp = STAMP.search(Path(source['path']).name)
            absolute = seconds(stamp[1])
            row = dict(source, channel=f'unlabelled_{i}', role='timing_only', head=None,
                       absolute_start=absolute, start_seconds=absolute-inv['origin_filename_seconds'])
            row['end_seconds'] = row['start_seconds']+row['frames']/row['sample_rate']
            extra.append(row)
        row['carrier_windows'] = scan['islands']
    inv['carrier_sources'] = extra
    survey_path = Path(path)/'survey.json' if Path(path).is_dir() else Path(path)
    inv['carrier_survey'] = dict(path=str(survey_path.resolve()), sha256=digest(survey_path))
    return inv


def calibration_proxy(x, sr, curve, events):
    """Cheap signed-frequency screen; final selection uses actual rendered audio."""
    values = []
    for b in events:
        lo, hi = int((b['start']-.15)*sr), int((b['end']+.15)*sr)
        _, freq = burst_track(x[lo:hi], sr)
        tt = lo/sr+np.arange(len(freq))/1000
        keep = (tt > b['start']+.04) & (tt < b['end']-.04)
        e = np.interp(tt[keep], np.arange(len(curve))/RATE, curve)
        if keep.sum() >= 40:
            values.append(float(np.std(freq[keep]/(1+e))/max(np.std(freq[keep]), 1e-9)))
    return float(np.median(values)) if values else None


def select_trial(trials):
    """Prefer A unless calibration shows a material benefit from wider tracking."""
    eligible = [r for r in trials if r.get('calibration_loss') is not None]
    if not eligible:
        return None
    best = min(eligible, key=lambda r: r['calibration_loss'])
    harmonics = [r for r in eligible if r['method'] == 'harmonic_3']
    if harmonics:
        first = min(harmonics, key=lambda r: r['calibration_loss'])
        if first['calibration_loss'] <= max(best['calibration_loss']*1.15, best['calibration_loss']+.05):
            return first
    return best


def transfer_passes(main, wrong, shifted):
    """Each margin must improve; controls must lose. This is local numeric evidence."""
    ratios = [m['residual_ratio'] for b in main for m in b['margins']]
    return bool(ratios and len(main) == len(wrong) == len(shifted)
                and all(len(b['margins']) == 3 for b in main+wrong+shifted)
                and max(ratios) <= .95 and np.median(ratios) <= .8
                and loss(main)+.10 < min(loss(wrong), loss(shifted)))


def quality_spans(gate, region):
    lo = round((region['start_seconds']-region['analysis_start'])*RATE)
    hi = round((region['end_seconds']-region['analysis_start'])*RATE)
    local = gate[lo:hi]
    states = np.where(local >= .95, 2, np.where(local > 0, 1, 0))
    result = []
    for state, action in ((2, 'use_reference'), (1, 'fade_correction'), (0, 'leave_uncorrected')):
        for a, b in spans(states == state):
            result.append(dict(start_seconds=region['start_seconds']+a/RATE,
                               end_seconds=min(region['end_seconds'], region['start_seconds']+b/RATE),
                               action=action,
                               reason='supported_reference' if state == 2 else 'dropout_or_phase_slip'))
    # Include any fractional last sample in the final span.
    result.sort(key=lambda r: r['start_seconds'])
    if result:
        result[-1]['end_seconds'] = region['end_seconds']
    else:
        result = [dict(start_seconds=region['start_seconds'], end_seconds=region['end_seconds'],
                       action='leave_uncorrected', reason='Less than one tracking sample')]
    return result


def analyze_region(inv, region, output, candidates_per_channel=4, reference_limit=4, quiet=False):
    wall, cpu = time.perf_counter(), time.process_time()
    io_start = os_read_bytes()
    read_bytes = 0
    start, end = region['analysis_start'], region['analysis_end']
    data, coverage, candidates, traces, event_rows = {}, [], [], {}, {}
    core_lo = round((region['start_seconds']-start)*RATE)
    core_hi = round((region['end_seconds']-start)*RATE)
    if end-start < 10:
        return dict(region, status='analyzed', decision='leave_uncorrected',
                    reason='Less than ten seconds of available tracking context', coverage=[],
                    candidates=[], targets=[dict(channel=r['channel'], action='leave_uncorrected',
                        selected=None, reason='Insufficient tracking context') for r in inv['channels']
                        if r['role'] != 'timing_only'], maps={}, reference_spans=[])
    for row in inv['channels']+inv.get('carrier_sources', []):
        ch = row['channel']
        if row['role'] == 'timing_only' and not row.get('carrier_windows'):
            coverage.append(dict(channel=ch, status='screened_no_candidate' if 'carrier_windows' in row else 'not_scanned', reason='Independent carrier survey required; decoding is separate'))
            continue
        # Context may not cross a channel's actual coverage edge. Missing context
        # is explicit, rather than padding silence and inventing a reference.
        if start < row['start_seconds'] or end > row['end_seconds']+1/row['sample_rate']:
            coverage.append(dict(channel=ch, status='uncovered', reason='Missing source or boundary context'))
            continue
        x, sr, digest = read_channel(row, start, end)
        read_bytes += x.nbytes
        data[ch] = (x, sr)
        coverage.append(dict(channel=ch, status='scanned', excerpt_float64_sha256=digest))
        event_rows[ch] = beeps(x, sr) if row['role'] != 'timing_only' else []
        discovered = discover(x, sr, candidates_per_channel) if row['role'] != 'timing_only' else []
        local_windows = [w for w in row.get('carrier_windows', [])
                         if row['start_seconds']+w['end_sample']/sr > start
                         and row['start_seconds']+w['start_sample']/sr < end]
        for w in local_windows:
            if not any(abs(c['carrier_hz']-w['carrier_hz']) < 40 for c in discovered):
                discovered.append(dict(carrier_hz=w['carrier_hz'], contrast_db=w['contrast_db'],
                    occupancy=1., from_carrier_survey=True, identity='generic_reference'))
        for candidate in discovered:
            hz = candidate['carrier_hz']
            capture = min(140., max(9., hz*.025), hz*.4)
            key = f'c{ch}_{hz:.2f}'
            mask = None
            if candidate.get('from_carrier_survey'):
                times = start+np.arange(round((end-start)*RATE))/RATE
                mask = np.zeros(len(times), dtype=bool)
                for w in local_windows:
                    if abs(w['carrier_hz']-hz) < 40:
                        mask |= ((times >= row['start_seconds']+w['start_sample']/sr)
                                 & (times < row['start_seconds']+w['end_sample']/sr))
            try:
                # Acquire widely before narrow-capture failure can reject a tone.
                wide_capture = min(180., max(12., hz*.09), hz*.45)
                wide = trajectory(x, sr, hz, wide_capture, mask)
                try:
                    trace = trajectory(x, sr, hz, capture, mask)
                except ValueError:
                    trace = wide
                if np.mean(wide[5][core_lo:core_hi]) > np.mean(trace[5][core_lo:core_hi]):
                    trace = wide
            except ValueError as exc:
                candidates.append(dict(candidate, key=key, channel=ch, rank=0.,
                                       eligible=False, rejection=str(exc)))
                continue
            e = signed_map(trace)
            rms = float(np.std(e[core_lo:core_hi])) if core_hi > core_lo else 0.
            support = float(np.mean(trace[5][core_lo:core_hi] >= .95)) if core_hi > core_lo else 0.
            rank = candidate['occupancy']*support*min(candidate['contrast_db']/30, 1)
            rank *= min(rms/.004, 1)*max(.05, trace[-1]['concentration'])
            record = dict(candidate, key=key, channel=ch, tracking=trace[-1],
                          speed_rms_percent=100*rms, core_support=support, rank=rank,
                          eligible=bool((support >= .5 or (mask is not None and support*(region['end_seconds']-region['start_seconds']) >= 2)) and .0002 < rms < .04),
                          carrier_status='carrier_supported' if support > 0 else 'carrier_candidate',
                          acquisition_masked=mask is not None)
            candidates.append(record)
            traces[key] = trace
            traces[key+'__mask'] = mask
        if not quiet:
            print(f"  {region['id']} channel {ch}: {len(discovered)} references, {len(event_rows[ch])} check events", flush=True)
    candidates.sort(key=lambda c: c['rank'], reverse=True)
    # All discovered candidates get a target-calibration screen. This prevents a
    # strong but irrelevant hum family from consuming the expensive shortlist.
    screening = []
    for ch, events in event_rows.items():
        cal, _ = split_events(events, region)
        if not cal:
            continue
        x, sr = data[ch]
        scores = []
        for c in candidates:
            if not c['eligible'] or c['channel'] == ch:
                continue
            score = calibration_proxy(x, sr, signed_map(traces[c['key']]), cal)
            scores.append(dict(key=c['key'], target_channel=ch, proxy_loss=score))
        screening.extend(sorted(scores, key=lambda s: float('inf') if s['proxy_loss'] is None else s['proxy_loss']))
    chosen, search_budget = fair_shortlist(candidates, screening, reference_limit)
    variants, maps, gates = [], {}, {}
    for c in chosen:
        trace = traces[c['key']]
        x, sr = data[c['channel']]
        wide_capture = min(180., max(12., c['carrier_hz']*.09), c['carrier_hz']*.45)
        wide = trajectory(x, sr, c['carrier_hz'], wide_capture, traces[c['key']+'__mask'])
        specs = [('harmonic_3', 'harmonic_3', trace, harmonic_map(trace), None),
                 ('harmonic_3_wide', 'harmonic_3', wide, harmonic_map(wide), None),
                 ('signed_2_15', 'signed_2_15', wide, signed_map(wide, (2, 15)), [2, 15]),
                 ('signed_2_30', 'signed_2_30', wide, signed_map(wide), [2, 30])]
        for suffix, method, tr, e, band in specs:
            key = c['key']+'_'+suffix
            maps[key], gates[key] = e, tr[5]
            variants.append(dict(key=key, reference_channel=c['channel'], method=method,
                                 carrier_hz=c['carrier_hz'], capture_hz=tr[-1]['capture_hz'],
                                 modulation_band_hz=band or [4, 30], harmonic_seed_band_hz=[4, 10] if method == 'harmonic_3' else None,
                                 speed_limit_saturation_fraction=float(np.mean(abs(e) >= .049-1e-9)),
                                 residual_reference_motion_rms_percent=float(100*np.std((tr[1]-e)[tr[5] >= .95])) if np.any(tr[5] >= .95) else None,
                                 harmonics=3 if method == 'harmonic_3' else None,
                                 reference_rank=c['rank'], tracking=tr[-1]))
    # Default for targets without check events remains the strongest shortlisted
    # reference by reference quality, using A. Target-trained ranking is separate.
    primary = max((v for v in variants if v['method'] == 'harmonic_3'),
                  key=lambda v: v['reference_rank'], default=None)
    targets = []
    for row in inv['channels']:
        ch = row['channel']
        if row['role'] == 'timing_only':
            continue
        target = dict(channel=ch, action='leave_uncorrected', selected=None,
                      reason='No usable original reference', calibration_trials=[])
        if ch not in data:
            target['reason'] = 'Source or required context unavailable'
        elif variants:
            x, sr = data[ch]
            cal, hold = split_events(event_rows[ch], region)
            target['evidence'] = evidence_strength(cal, hold)
            target['reserved_events'] = [b for b in event_rows[ch]
                if b['start'] >= region['start_seconds']-start and b['end'] <= region['end_seconds']-start
                and (b['start'] < region['start_seconds']-start+2 or b['end'] > region['end_seconds']-start-2)]
            for v in variants:
                if v['reference_channel'] == ch:
                    continue  # A reference cannot validate its own transfer.
                measured = evaluate_events(x, sr, maps[v['key']], cal)
                target['calibration_trials'].append(dict(key=v['key'], method=v['method'],
                    calibration_loss=loss(measured), events=measured))
            selected = select_trial(target['calibration_trials'])
            v = next((v for v in variants if selected and v['key'] == selected['key']), primary)
            target.update(selected=v['key'], reference_channel=v['reference_channel'],
                          action='validate_transfer', reason='Insufficient separate calibration and holdout events',
                          calibration_event_count=len(cal), holdout_event_count=len(hold))
            if selected and cal and hold:
                e = maps[v['key']]
                metrics = evaluate_events(x, sr, e, hold)
                wrong = evaluate_events(x, sr, -e, hold)
                shifted = {str(shift): evaluate_events(x, sr, shifted_curve(e, shift), hold)
                           for shift in LIMITS['wrong_time_seconds']}
                no_motion = evaluate_events(x, sr, np.zeros_like(e), hold)
                # AM-derived phase control tests amplitude coupling separately.
                tr = traces[next(c['key'] for c in chosen if v['key'].startswith(c['key']+'_'))]
                amplitude = tr[3]/max(float(np.median(tr[3])), 1e-12)-1
                am = amplitude*float(np.std(e))/max(float(np.std(amplitude)), 1e-12)
                am *= gates[v['key']]
                am_control = evaluate_events(x, sr, np.clip(am, -.049, .049), hold)
                passed = numeric_pass(metrics, [wrong, no_motion, am_control], list(shifted.values()))
                supported_events = all(np.min(gates[v['key']][round(b['start']*RATE):round(b['end']*RATE)]) >= LIMITS['minimum_support'] for b in cal+hold)
                strong = target['evidence']['level'] == 'multiple_time_windows'
                passed = passed and supported_events
                low_baseline = bool(metrics) and all(m['before_rms_hz'] < LIMITS['low_baseline_hz'] for b in metrics for m in b['margins'])
                target.update(holdout=metrics, wrong_sign=wrong, wrong_time_controls=shifted,
                              no_motion_control=no_motion, amplitude_control=am_control,
                              numeric_transfer_passed=passed,
                              action=('render_and_review' if strong else 'validate_transfer') if passed else 'leave_uncorrected',
                              reason='Local held-out event improvement; speech/head provenance still needs review'
                              if passed else 'Frozen calibration choice failed held-out transfer or controls')
                if low_baseline:
                    target.update(action='leave_uncorrected', reason='Low baseline flutter: no-op/no-harm policy', no_op=True)
            target['reference_spans'] = quality_spans(gates[v['key']], region)
        targets.append(target)
    # Freeze one donor and one distinct target carrier using reference quality
    # only. Never cycle alternatives after inspecting group holdouts.
    from flutter_groups import demonstrate_group, SETTINGS as GROUP_SETTINGS
    donors = [t for t in targets if t['action'] == 'render_and_review']
    for target in targets:
        if target['action'] != 'validate_transfer' or target.get('holdout_event_count', 0):
            continue
        donor = next((t for t in donors if t['selected'] == target['selected']), None)
        if donor is None:
            continue
        variant = next(v for v in variants if v['key'] == donor['selected'])
        own = next((c for c in candidates if c['channel'] == target['channel'] and c['eligible']
                    and abs(c['carrier_hz']-variant['carrier_hz']) >= GROUP_SETTINGS['minimum_frequency_separation_hz']), None)
        if own is None:
            continue
        group = demonstrate_group(maps[donor['selected']], gates[donor['selected']], traces[own['key']], region)
        group.update(direct_donor_channel=donor['channel'], independent_target_reference=own['key'])
        target['transfer_group'] = group
        if group['passed']:
            target.update(action='group_render_and_review', reason='Bounded independently checked playback-motion group; listening still required')
            target['evidence']['transfer_group_supported'] = True
            target['reserved_events'] = [dict(e, carrier_hz=own['carrier_hz'],
                capture_hz=traces[own['key']][-1]['capture_hz'], evidence_kind='independent_target_carrier') for e in group['reserved_events']]
    used = {t['selected'] for t in targets if t['selected']}
    if primary:
        used.add(primary['key'])
    output.mkdir(parents=True, exist_ok=True)
    map_path = output/(region['id']+'_maps.npz')
    if used:
        np.savez_compressed(map_path, **{k: maps[k] for k in sorted(used)},
                            **{k+'__support': gates[k] for k in sorted(used)})
    return dict(region, status='analyzed', decision='regional_candidates' if primary else 'leave_uncorrected',
                search_budget=search_budget,
                performance=dict(decoded_read_bytes=read_bytes, os_read_bytes=io_difference(io_start),
                                 wall_seconds=time.perf_counter()-wall, cpu_seconds=time.process_time()-cpu),
                coverage=coverage, candidates=candidates, calibration_screening=screening,
                variants=variants, targets=targets,
                primary=primary['key'] if primary else None,
                maps=dict(file=map_path.name, rate=RATE,
                          sha256=hashlib.sha256(map_path.read_bytes()).hexdigest()) if used else {},
                events=event_rows,
                reference_spans=quality_spans(gates[primary['key']], region) if primary else [],
                next_action='Render cleaned speech with selected maps and review transfer/boundaries'
                if primary else 'Keep this region uncorrected; inspect alternate carriers or add a supported estimator')


def transitions(regions, folder):
    result = []
    for left, right in zip(regions, regions[1:]):
        if left['status'] != 'analyzed' or right['status'] != 'analyzed':
            continue
        record = dict(at_seconds=right['start_seconds'], left=left['id'], right=right['id'], targets=[])
        if not left.get('maps') or not right.get('maps'):
            record.update(action='fade_to_identity', reason='One side has no supported reference')
            result.append(record)
            continue
        with np.load(folder/left['maps']['file']) as lm, np.load(folder/right['maps']['file']) as rm:
            by_channel = {t['channel']: t for t in left['targets']}
            a = max(left['analysis_start']+2, right['analysis_start']+2)
            b = min(left['analysis_end']-2, right['analysis_end']-2)
            for target in right['targets']:
                previous = by_channel.get(target['channel'])
                if not previous or not previous['selected'] or not target['selected'] or b <= a:
                    continue
                li = slice(round((a-left['analysis_start'])*RATE), round((b-left['analysis_start'])*RATE))
                ri = slice(round((a-right['analysis_start'])*RATE), round((b-right['analysis_start'])*RATE))
                le, re = lm[previous['selected']][li], rm[target['selected']][ri]
                supported = (lm[previous['selected']+'__support'][li] >= .95) & (rm[target['selected']+'__support'][ri] >= .95)
                disagreement = float(np.sqrt(np.mean((le[supported]-re[supported])**2))) if supported.any() else None
                usable = supported.mean() >= .9 and disagreement is not None and disagreement <= .005
                transfer_ready = all(t['action'] in {'render_and_review', 'group_render_and_review'} for t in (previous, target))
                record['targets'].append(dict(channel=target['channel'],
                    reference_or_method_changed=previous['selected'] != target['selected'],
                    overlap_speed_rms_difference=disagreement,
                    transfer_ready=transfer_ready,
                    action=('blend_speed_then_integrate' if usable else 'fade_to_identity_and_review')
                           if transfer_ready else 'validate_transfer_before_join',
                    overlap_seconds=[a, b], blend_seconds=2))
        record['action'] = 'review_target_transitions'
        result.append(record)
    return result


def save_plan(plan, output):
    regions = plan['regions']
    plan['complete'] = (all(r['status'] == 'analyzed' for r in regions)
                        and plan.get('source_validation') == 'verified')
    plan['analyzed_seconds'] = sum(r['end_seconds']-r['start_seconds'] for r in regions if r['status'] == 'analyzed')
    plan.setdefault('transitions', [])
    plan['summary'] = dict(regions=len(regions), analyzed=sum(r['status'] == 'analyzed' for r in regions),
        target_actions={action: sum(t['action'] == action for r in regions for t in r.get('targets', []))
                        for action in ('render_and_review', 'group_render_and_review', 'validate_transfer', 'leave_uncorrected')})
    coverage = []
    for row in plan['inventory']['channels']:
        if row['role'] == 'timing_only':
            continue
        seconds_by_category = dict(direct_numeric=0., demonstrated_group=0., proposal=0., rejected=0., no_evidence=0., pending=0.)
        switches, exhausted, previous = 0, 0, None
        for region in regions:
            duration = max(0., min(row['end_seconds'], region['end_seconds'])-max(row['start_seconds'], region['start_seconds']))
            if not duration:
                continue
            target = next((t for t in region.get('targets', []) if t['channel'] == row['channel']), None)
            if region['status'] != 'analyzed':
                category = 'pending'
            elif target is None or not target.get('selected'):
                category = 'no_evidence'
            else:
                category = {'render_and_review': 'direct_numeric', 'group_render_and_review': 'demonstrated_group', 'validate_transfer': 'proposal', 'leave_uncorrected': 'rejected'}[target['action']]
                if previous is not None and previous != target['selected']:
                    switches += 1
                previous = target['selected']
            seconds_by_category[category] += duration
            exhausted += int(region.get('search_budget', {}).get('exhausted', False))
        coverage.append(dict(channel=row['channel'], seconds=seconds_by_category,
                             reference_switches=switches, search_budget_exhausted_regions=exhausted,
                             listening_approved=False, production_accepted=False,
                             final_map_diagnostics='Measured by continuous renderer before acceptance'))
    plan['summary']['duration_weighted_coverage'] = coverage
    plan['summary']['measurement'] = 'Channel-region duration; within-region support gaps are reported separately, not implied corrected'
    # Keep the frequently checkpointed index small. Detailed evidence is written
    # once per region, avoiding quadratic rewriting on a 17-hour tape.
    index = dict(plan, regions=[])
    for r in regions:
        entry = {k: v for k, v in r.items() if k not in
                 {'coverage', 'candidates', 'calibration_screening', 'variants', 'events', 'targets', 'reference_spans'}}
        entry['targets'] = [{k: t[k] for k in ('channel', 'action', 'selected', 'reason') if k in t}
                            for t in r.get('targets', [])]
        index['regions'].append(entry)
    write_json(output/'plan.json', index)
    rows = []
    for r in regions:
        methods = ', '.join(sorted({v['method'] for v in r.get('variants', [])
                                   if v['key'] in {t['selected'] for t in r.get('targets', [])}}))
        actions = ', '.join(f"CH{t['channel']}: {t['action']}" for t in r.get('targets', []))
        label = f'<a href="{r["detail_file"]}">{r["id"]}</a>' if r.get('detail_file') else r['id']
        rows.append(f"<tr><td>{label}</td><td>{r['start_seconds']:.3f}–{r['end_seconds']:.3f}</td>"
                    f"<td>{r['status']}</td><td>{html.escape(methods)}</td><td>{html.escape(actions)}</td></tr>")
    page = '<!doctype html><meta charset="utf-8"><title>Regional deflutter plan</title>'
    page += '<style>body{font:15px/1.5 system-ui;max-width:1400px;margin:30px auto;padding:20px}td,th{padding:10px;border-bottom:1px solid #ccc;text-align:left}a{color:#1766ab}</style>'
    page += f"<h1>{html.escape(plan['inventory']['tape'])}: regional deflutter plan</h1>"
    page += f"<p>{plan['summary']['analyzed']} / {len(regions)} regions analyzed. Full tape coverage: {plan['full_tape'] and plan['complete']}.</p>"
    page += '<p>Harmonic tracking is the first candidate. Wider tracking is selected when calibration earns it. Each target has its own transfer decision. Open a region for its evidence, settings and gaps. Numeric event evidence still requires speech and transition listening.</p>'
    page += '<p><a href="plan.json">Complete plan, source hashes, settings, gaps, controls and transition instructions</a></p>'
    page += '<table><tr><th>Region</th><th>Tape seconds</th><th>Status</th><th>Methods</th><th>Target actions</th></tr>'+''.join(rows)+'</table>'
    (output/'index.html').write_text(page, encoding='utf-8')


def plan_tape(source, output, *, region_seconds=60, context_seconds=4, start=0,
              duration=None, candidates_per_channel=4, reference_limit=4,
              max_regions=None, resume=False, workers=1, reuse=False, carrier_survey=None):
    source, output = Path(source).resolve(), Path(output).resolve()
    if source == output or source in output.parents or output in source.parents:
        raise ValueError('Input and output must be non-overlapping')
    if candidates_per_channel < 1 or reference_limit < 1 or (max_regions is not None and max_regions < 1):
        raise ValueError('Candidate/reference/region limits must be positive')
    if not isinstance(workers, int) or workers < 0:
        raise ValueError('Workers must be a nonnegative integer (0 selects automatically)')
    if workers == 0:
        workers = min(8, max(1, (os.cpu_count() or 2)//2))
    inv = inventory(source)
    if carrier_survey is not None:
        attach_survey(inv, carrier_survey)
    params = dict(region_seconds=region_seconds, context_seconds=context_seconds, start=start,
                  duration=duration, candidates_per_channel=candidates_per_channel, reference_limit=reference_limit,
                  carrier_survey=inv.get('carrier_survey'))
    code_hash = implementation_digest()
    if reuse and (output/'plan.json').is_file():
        saved = load_saved_plan(output/'plan.json', require_complete=False)
        if saved.get('methodology') != METHODOLOGY or saved.get('implementation_sha256') != code_hash or saved.get('policy_sha256') != policy_digest():
            raise ValueError('Stored plan methodology or implementation changed; preserve baseline and choose a new planning destination')
        if saved['inventory'] != inv or saved['parameters'] != params:
            raise ValueError('Stored plan uses different sources or settings; choose a different output for a new plan')
        if saved.get('complete') and saved.get('source_validation') == 'verified':
            # Only an exact compatible completed result is reusable.
            print(f"Reusing completed tape plan: {output/'plan.json'} (no analysis rerun)", flush=True)
            return saved
        resume = True
    if resume:
        plan = json.loads((output/'plan.json').read_text(encoding='utf-8'))
        if plan['inventory'] != inv or plan['parameters'] != params or plan['implementation_sha256'] != code_hash:
            raise ValueError('Cannot resume: sources, parameters or implementation changed')
        for r in plan['regions']:
            if r.get('maps') and hashlib.sha256((output/r['maps']['file']).read_bytes()).hexdigest() != r['maps']['sha256']:
                raise ValueError('Saved timing map changed')
        for i, r in enumerate(plan['regions']):
            if r.get('detail_file'):
                raw = (output/r['detail_file']).read_bytes()
                if hashlib.sha256(raw).hexdigest() != r['detail_sha256']:
                    raise ValueError('Saved region evidence changed')
                detail = json.loads(raw)
                detail.update(detail_file=r['detail_file'], detail_sha256=r['detail_sha256'])
                plan['regions'][i] = detail
    else:
        # Bounded audits live beneath the tape folder independently of its
        # full-tape plan. Their presence must not block the first full plan.
        if output.exists() and any(p.name != 'audits' or not p.is_dir()
                                   for p in output.iterdir()):
            raise ValueError('Use a fresh output folder, or --resume with identical settings')
        regions = make_regions(inv, start, duration, region_seconds, context_seconds)
        output.mkdir(parents=True, exist_ok=True)
        plan = dict(schema=SCHEMA, methodology=METHODOLOGY, policy_sha256=policy_digest(),
                    acceptance_limits=LIMITS, acceptance_status='development_requires_pilot_and_listening',
                    implementation_sha256=code_hash, inventory=inv, parameters=params,
                    full_tape=start == 0 and (duration is None or duration >= inv['duration_seconds']),
                    policy=dict(first_method='harmonic_3', alternatives=['signed_2_15', 'signed_2_30'],
                        selection='Reference quality plus early-event calibration shortlist; frozen per-target choice tested on later unused events',
                        gaps='No extrapolation: unsupported reference spans remain uncorrected',
                        transfer='Never infer shared flutter from channel/head names; preserve per-target evidence',
                        boundaries='Blend supported speed curves in overlap before integration; otherwise fade to identity and review',
                        integration='One continuous monotonic inverse timing map per target group; preserve final frame count. Do not concatenate independently anchored excerpt maps.',
                        cleanup=dict(stage_03=dict(tonal_method=TONAL_CLEANUP_METHOD,
                                                   broadband_cap_db=DEFAULT_BROADBAND_CAP_DB)),
                        execution='Planning artifact; continuous full-tape audio rendering is a separate step'), regions=regions)
    plan['source_validation'] = 'in_progress'
    save_plan(plan, output)
    indices = [i for i, r in enumerate(plan['regions']) if r['status'] != 'analyzed']
    if max_regions is not None:
        indices = indices[:max_regions]
    workers = min(workers, max(1, len(indices)))
    started = time.perf_counter()
    processed_seconds = 0.
    print(f"Planning {inv['tape']}: {len(indices)} regions, {workers} worker(s)", flush=True)

    def accept(i, result):
        nonlocal processed_seconds
        region = plan['regions'][i]
        detail_path = output/(region['id']+'.json')
        write_json(detail_path, result)
        result.update(detail_file=detail_path.name, detail_sha256=hashlib.sha256(detail_path.read_bytes()).hexdigest())
        plan['regions'][i] = result
        # Workers may finish out of order. Add each join exactly once once both
        # neighbours are present; serialize the plan in timeline order.
        existing = {(t['left'], t['right']) for t in plan['transitions']}
        for left in (i-1, i):
            if 0 <= left < len(plan['regions'])-1:
                pair = plan['regions'][left:left+2]
                if (pair[0]['id'], pair[1]['id']) not in existing:
                    plan['transitions'].extend(transitions(pair, output))
        plan['transitions'].sort(key=lambda t: t['at_seconds'])
        processed_seconds += region['end_seconds']-region['start_seconds']
        elapsed = time.perf_counter()-started
        remaining = sum(r['end_seconds']-r['start_seconds'] for r in plan['regions'] if r['status'] != 'analyzed')
        plan['performance'] = dict(workers=workers, invocation_elapsed_seconds=elapsed,
            decoded_read_bytes=sum(r.get('performance', {}).get('decoded_read_bytes', 0) for r in plan['regions']),
            os_read_bytes=sum(r.get('performance', {}).get('os_read_bytes') or 0 for r in plan['regions']),
            worker_cpu_seconds=sum(r.get('performance', {}).get('cpu_seconds', 0.) for r in plan['regions']),
            invocation_analyzed_seconds=processed_seconds,
            projected_remaining_seconds=remaining*elapsed/processed_seconds,
            projection_basis='Observed throughput in this invocation; varies with events, disk and CPU load')
        save_plan(plan, output)
        print(f"  {plan['summary']['analyzed']}/{len(plan['regions'])} regions complete; "
              f"{region['id']}; remaining estimate {remaining*elapsed/processed_seconds/60:.1f} min", flush=True)

    if workers == 1:
        for i in indices:
            accept(i, analyze_region(inv, plan['regions'][i], output,
                                     candidates_per_channel, reference_limit, quiet=True))
    else:
        # Bound queued work and memory to the worker count, including on failure.
        # Each worker writes only its own map; the parent owns checkpoints/joins.
        with ProcessPoolExecutor(max_workers=workers, mp_context=get_context('spawn')) as pool:
            todo = iter(indices)
            pending = {}
            def submit_next():
                i = next(todo, None)
                if i is not None:
                    future = pool.submit(analyze_region, inv, plan['regions'][i], output,
                                         candidates_per_channel, reference_limit, True)
                    pending[future] = i
            for _ in range(workers):
                submit_next()
            try:
                while pending:
                    completed, _ = wait(pending, return_when=FIRST_COMPLETED)
                    for future in sorted(completed, key=lambda f: pending[f]):
                        i = pending.pop(future)
                        accept(i, future.result())
                        submit_next()
            finally:
                for future in pending:
                    future.cancel()
    # Detect source changes even if they happened after the last channel read.
    final_inventory = inventory(source)
    if carrier_survey is not None:
        attach_survey(final_inventory, carrier_survey)
    if final_inventory != inv:
        plan['source_validation'] = 'changed'
        save_plan(plan, output)
        raise ValueError('Sources changed during planning; results require a new run')
    plan['source_validation'] = 'verified'
    save_plan(plan, output)
    print(f"Plan written: {output/'plan.json'}; {plan['summary']['analyzed']}/{len(plan['regions'])} regions analyzed", flush=True)
    return plan
