"""Evidence-triggered reference lifetimes with persistent original-sample trackers.

Monitoring windows collect evidence. They neither restart a working tracker nor
end its reference span. Candidate search runs only for unresolved targets; a
failed frozen validation is not retried on the same passage.
"""
import hashlib
from copy import deepcopy
import json
import math
from pathlib import Path
import uuid

import numpy as np
import soundfile as sf
from scipy import signal

from continuous_tracking import CarrierStream, METHODS, RATE, track_array
from continuous_acquisition import AcquisitionEvidence, local_roles
from continuous_quality import classify_numeric_quality, IMPROVEMENT_LIMITS
from carrier_capture import capture_candidates
from flutter_planning import inventory, read_channel, calibration_proxy, fair_shortlist, loss, write_json
from flutter_tracking import beeps, evaluate_events, discover, trajectory, signed_map
from flutter_policy import (METHODOLOGY, LIMITS, digest, implementation_digest,
                            policy_digest, numeric_pass, shifted_curve, evidence_strength)
from flutter_plan_store import load_saved_plan, checked_artifact
from flutter_groups import compare_motion, SETTINGS as GROUP_SETTINGS

SETTINGS = dict(monitor_seconds=60., context_seconds=4., minimum_support=.90,
                maximum_unchecked_seconds=300., transition_seconds=.5,
                reference_limit=8, candidates_per_channel=8,
                final_event_stride=5, calibration_tie_relative=.10, calibration_tie_absolute=.01,
                minimum_group_amplitude_modulation=.01)


def allocate_session_key(folder, sequence):
    # A crashed, uncommitted acquisition may leave a stream. Preserve it and
    # choose another identity; never append new tracking to orphan bytes.
    while (Path(folder)/f's{sequence:06d}.f64').exists():
        sequence += 1
    return f's{sequence:06d}', sequence+1


class Session:
    """One reference/method lifetime, append-only map and resumable filter state."""
    def __init__(self, folder, key, row, start, hz, capture, amplitude, method, chunk_seconds, *, create=True):
        self.folder, self.key, self.row = Path(folder), key, row
        self.start = start
        self.offset = round((start-row['start_seconds'])*row['sample_rate'])
        self.read_frames = self.frames = 0
        self.chunk_seconds = chunk_seconds
        self.tracker = CarrierStream(row['sample_rate'], hz, capture, amplitude, method)
        self.file = key+'.f64'
        if create:
            with (self.folder/self.file).open('xb'):
                pass
        self.hasher = hashlib.sha256()
        self.finished = False

    def append(self, result):
        ids = result['indices']
        keep = ids >= self.frames
        ids = ids[keep]
        if not len(ids):
            return
        if ids[0] != self.frames or np.any(np.diff(ids) != 1):
            raise ValueError('Streaming map lost sample continuity')
        values = np.column_stack((result['speed'][keep], result['support'][keep])).astype('<f8')
        raw = values.tobytes()
        with (self.folder/self.file).open('ab') as handle:
            handle.write(raw)
        self.hasher.update(raw)
        self.frames += len(values)

    def advance(self, end):
        if self.finished:
            return
        sr = self.row['sample_rate']
        wanted = min(self.row['frames']-self.offset, math.ceil((end-self.start+2)*sr))
        with sf.SoundFile(self.row['path']) as stream:
            stream.seek(self.offset+self.read_frames)
            while self.read_frames < wanted:
                count = min(wanted-self.read_frames, max(1, round(self.chunk_seconds*sr)))
                samples = stream.read(count, dtype='float64')
                if len(samples) != count:
                    raise ValueError('Reference changed during streaming')
                self.append(self.tracker.push(samples))
                self.read_frames += count
        if self.offset+self.read_frames == self.row['frames']:
            self.append(self.tracker.finish())
            self.finished = True

    def values(self, times):
        if not self.frames:
            return np.zeros(len(times)), np.zeros(len(times))
        data = np.memmap(self.folder/self.file, dtype='<f8', mode='r', shape=(self.frames, 2))
        positions = (np.asarray(times)-self.start)*RATE
        # Fetch only the requested local section, independent of tape length.
        low = max(0, int(np.floor(positions.min()))-1)
        high = min(self.frames, int(np.ceil(positions.max()))+2)
        if high <= low:
            return np.zeros(len(times)), np.zeros(len(times))
        grid = np.arange(low, high)
        return tuple(np.interp(positions, grid, data[low:high, col], left=0., right=0.) for col in (0, 1))

    def descriptor(self):
        return dict(file=self.file, sha256=self.hasher.hexdigest(), frames=self.frames,
                    start_seconds=self.start, columns=['speed', 'support'], dtype='<f8')

    def checkpoint(self, generation=None):
        state = self.tracker
        arrays = ('zi_alias', 'zi_capture', 'zi_motion', 'zi_unsafe', 'phase_history')
        generation = generation or uuid.uuid4().hex
        state_file = f'{self.key}_state_{generation}.npz'
        with (self.folder/state_file).open('xb') as handle:
            np.savez(handle, **{name: getattr(state, name) for name in arrays})
        return dict(key=self.key, row=self.row, start=self.start, offset=self.offset,
                    read_frames=self.read_frames, frames=self.frames, finished=self.finished,
                    chunk_seconds=self.chunk_seconds, descriptor=self.descriptor(),
                    tracker=dict(hz=state.hz, capture=state.capture,
                        amplitude=state.amplitude_reference, method=state.method,
                        raw_samples=state.raw_samples, base_samples=state.base_samples,
                        tracking_samples=state.tracking_samples, last_phase=state.last_phase,
                        frequency_history=state.frequency_history, finished=state.finished),
                    pending_clips=state.pending_clips,
                    state_file=state_file, state_sha256=digest(self.folder/state_file))

    @classmethod
    def restore(cls, folder, saved, chunk_seconds):
        s = saved['tracker']
        result = cls(folder, saved['key'], saved['row'], saved['start'], s['hz'], s['capture'],
                     s['amplitude'], s['method'], chunk_seconds, create=False)
        file = result.folder/result.file
        committed_bytes = saved['frames']*16
        if file.stat().st_size < committed_bytes:
            raise ValueError('Checkpoint stream changed')
        with file.open('rb') as handle:
            remaining = committed_bytes
            while remaining:
                raw = handle.read(min(remaining, 4*1024*1024))
                if not raw:
                    raise ValueError('Checkpoint stream is truncated')
                result.hasher.update(raw)
                remaining -= len(raw)
        if result.hasher.hexdigest() != saved['descriptor']['sha256']:
            raise ValueError('Checkpoint stream changed')
        state_file, _ = checked_artifact(result.folder, saved['state_file'], saved['state_sha256'])
        with np.load(state_file, allow_pickle=False) as archive:
            for name in archive.files:
                setattr(result.tracker, name, archive[name].copy())
        for name in ('raw_samples', 'base_samples', 'tracking_samples', 'last_phase', 'frequency_history', 'finished'):
            setattr(result.tracker, name, s[name])
        result.tracker.pending_clips = saved.get('pending_clips', [])
        for name in ('offset', 'read_frames', 'frames', 'finished'):
            setattr(result, name, saved[name])
        # Bytes appended after the last committed plan belong to an interrupted
        # generation. Verify the committed prefix before removing that suffix.
        if file.stat().st_size > committed_bytes:
            with file.open('r+b') as handle:
                handle.truncate(committed_bytes)
        return result


def event_roles(x, sr, origin, low, high):
    events = [b for b in beeps(x, sr) if low <= origin+b['start'] and origin+b['end'] <= high]
    separated = []
    for event in events:
        if not separated or event['start']-separated[-1]['start'] >= LIMITS['independent_window_seconds']:
            separated.append(event)
    final = separated[SETTINGS['final_event_stride']-1::SETTINGS['final_event_stride']]
    development = [e for i, e in enumerate(separated) if (i+1) % SETTINGS['final_event_stride']]
    middle = len(development)//2
    return development[:middle], development[middle:], final


def add_span(target, low, high, key, reason, variant=None):
    if high <= low:
        return
    spans = target['reference_spans']
    action = 'use_reference' if key else 'leave_uncorrected'
    kind = 'transfer_group' if target['action'] == 'group_render_and_review' else 'direct_numeric'
    if spans and spans[-1].get('selected') == key and spans[-1]['reason'] == reason and (not key or spans[-1].get('evidence_kind') == kind) and abs(spans[-1]['end_seconds']-low) < 1e-7:
        spans[-1]['end_seconds'] = high
        return
    span = dict(start_seconds=low, end_seconds=high, action=action, selected=key, reason=reason)
    if variant:
        span.update(reference_channel=variant['reference_channel'], method=variant['method'],
                    evidence_kind=kind, target_action=target['action'],
                    evidence=deepcopy(target['evidence']),
                    numeric_transfer_passed=target['numeric_transfer_passed'],
                    quality_level=target.get('quality_level', 'restoration_target_met'),
                    restoration_target_met=target.get('restoration_target_met', False))
        if kind == 'transfer_group':
            span['transfer_group'] = deepcopy(target.get('transfer_group', {}))
    spans.append(span)


def reopen_pending_span(target, low):
    """Backfill only unqualified waiting audio once its acquisition validates."""
    retained = []
    for span in target['reference_spans']:
        if span['end_seconds'] <= low:
            retained.append(span)
        else:
            if span.get('selected'):
                raise ValueError('Acquisition cannot replace an already qualified reference span')
            if span['start_seconds'] < low:
                retained.append(dict(span, end_seconds=low))
    target['reference_spans'] = retained


def local_data(inv, low, high):
    data = {}
    for row in inv['channels']:
        a, b = max(row['start_seconds'], low), min(row['end_seconds'], high)
        if b-a < 10:
            continue
        x, sr, _ = read_channel(row, a, b)
        data[row['channel']] = (x, sr, a, b)
    return data


def projected(session, item):
    x, sr, origin, end = item
    return session.values(origin+np.arange(round(len(x)*RATE/sr))/RATE)


def supported_interval(session, low, high):
    """First physical supported run; never silently resume after a loss."""
    times = low+np.arange(max(0, math.ceil((high-low)*RATE)))/RATE
    _, support = session.values(times)
    safe = support >= .95
    indices = np.flatnonzero(safe)
    if not len(indices):
        return low, low
    first = indices[0]
    missing = np.flatnonzero(~safe[first:])
    last = first+missing[0] if len(missing) else len(safe)
    return low+first/RATE, min(high, low+last/RATE)


def retain_span(target, session, low, high, key, variant, allow_leading_gap=False):
    a, b = supported_interval(session, low, high)
    if not allow_leading_gap and a > low+1/RATE:
        a = b = low
    add_span(target, low, a, None, 'Reference acquisition context is unsupported')
    add_span(target, a, b, key, 'Validated continuous reference', variant)
    add_span(target, b, high, None, 'Reference support lost; reacquisition required')
    return b >= high-1e-8


def monitor_pass(metrics, quality_level='restoration_target_met'):
    margins = [m for event in metrics for m in event['margins']]
    if quality_level == 'supported_improvement':
        return bool(margins and all(len(e['margins']) == 3 for e in metrics)
            and max(m['residual_ratio'] for m in margins) <= IMPROVEMENT_LIMITS['max_event_ratio']
            and np.median([m['residual_ratio'] for m in margins]) <= IMPROVEMENT_LIMITS['median_event_ratio'])
    return bool(margins and all(len(e['margins']) == 3 for e in metrics)
                and max(m['residual_ratio'] for m in margins) <= 1.05
                and np.median([m['residual_ratio'] for m in margins]) <= LIMITS['median_event_ratio']
                and max(m['after_rms_hz'] for m in margins) <= LIMITS['max_residual_hz']
                and max(m['after_rms_cents'] for m in margins) <= LIMITS['max_residual_cents'])


def competing_carriers(x, sr, hz, capture):
    """Conservative ambiguity evidence outside the expected near-carrier FM core."""
    frequencies, power = signal.welch(x, sr, nperseg=min(2*sr, len(x)))
    band = np.flatnonzero(abs(frequencies-hz) <= capture)
    if not len(band):
        return []
    strongest = band[np.argmax(power[band])]
    peaks, _ = signal.find_peaks(power)
    separation = max(40., hz*.04)
    return [float(frequencies[k]) for k in peaks
            if abs(frequencies[k]-hz) <= capture
            and abs(frequencies[k]-frequencies[strongest]) >= separation
            and power[k] >= .5*power[strongest]]


def group_evidence(applied, independent, support, amplitude, lo, hi):
    modulation = float(np.std(amplitude[lo:hi])/max(float(np.median(amplitude[lo:hi])), 1e-12)) if hi > lo else 0.
    # Correlation of a nearly constant envelope is numerically unstable and
    # phase-filter ripple is not evidence of meaningful AM coupling.
    if modulation < SETTINGS['minimum_group_amplitude_modulation']:
        amplitude = np.ones_like(amplitude)
    windows = compare_motion(applied, independent, support, amplitude, lo, hi)
    middle = len(windows)//2
    passed = (min(middle, len(windows)-middle) >= GROUP_SETTINGS['minimum_windows_per_split']
              and all(window['passed'] for window in windows))
    return dict(passed=bool(passed), calibration=windows[:middle], holdout=windows[middle:],
                reserved_events=[dict(start=w['end'], end=w['start']+GROUP_SETTINGS['window_seconds']) for w in windows],
                amplitude_modulation_rms=modulation, content_lag_fitted=False)


def acquire(inv, data, roles, low, high, reference_limit, candidates_per_channel, incumbents=()):
    """Calibrate all shortlisted variants; validate exactly one frozen choice per target."""
    candidates, traces, screening = [], {}, []
    for row in inv['channels']:
        ch = row['channel']
        if ch not in data:
            continue
        x, sr, origin, end = data[ch]
        for candidate in discover(x, sr, candidates_per_channel):
            hz = candidate['carrier_hz']
            options = []
            for capture in capture_candidates(hz, sr, (2., 30.)):
                if competing_carriers(x, sr, hz, capture):
                    continue
                try:
                    candidate_trace = trajectory(x, sr, hz, capture)
                except ValueError:
                    continue
                options.append(candidate_trace)
            if not options:
                continue
            trace = max(options, key=lambda t: (float(np.mean(t[5] >= .95)), t[-1]['capture_hz']))
            key = f'c{ch}_{hz:.2f}'
            support = float(np.mean(trace[5] >= .95))
            candidates.append(dict(candidate, channel=ch, key=key, eligible=support >= .5,
                                   rank=support*min(candidate['contrast_db']/30., 1.)))
            traces[key] = trace
    for ch, (cal, _, _) in roles.items():
        if not cal:
            continue
        x, sr, origin, end = data[ch]
        times = origin+np.arange(round(len(x)*RATE/sr))/RATE
        for candidate in candidates:
            if candidate['channel'] == ch or not candidate['eligible']:
                continue
            source_origin = data[candidate['channel']][2]
            trace = traces[candidate['key']]
            curve = np.interp(times-source_origin, trace[0], signed_map(trace), left=0., right=0.)
            screening.append(dict(key=candidate['key'], target_channel=ch,
                                   proxy_loss=calibration_proxy(x, sr, curve, cal)))
    shortlist, budget = fair_shortlist(candidates, screening, reference_limit)
    trials, curves = [], {}
    for candidate in shortlist:
        ch, hz = candidate['channel'], candidate['carrier_hz']
        x, sr, origin, end = data[ch]
        for method, band in METHODS.items():
            for capture in capture_candidates(hz, sr, band):
                if competing_carriers(x, sr, hz, capture):
                    continue
                try:
                    trace = trajectory(x, sr, hz, capture)
                except ValueError:
                    continue
                amplitude = max(float(np.median(trace[3][400:-400])), 1e-12)
                key = candidate['key']+'_'+method+f'_cap{capture:g}'
                e, support = track_array(x, sr, hz, capture, amplitude, method)
                curves[key] = (e, support, origin)
                trials.append(dict(key=key, reference_channel=ch, carrier_hz=hz,
                                   capture_hz=capture, amplitude_reference=amplitude,
                                   method=method, modulation_band_hz=list(band),
                                   acquisition_start=origin, rank=candidate['rank'],
                                   amplitude_curve=trace[3]/amplitude-1.))
    retained_donors = []
    for donor_channel, session_key, variant, session in incumbents:
        if variant['reference_channel'] not in data:
            continue
        source_item = data[variant['reference_channel']]
        e, support = projected(session, source_item)
        key = 'retained_'+session_key
        curves[key] = (e, support, source_item[2])
        trials.append(dict(variant, key=key, acquisition_start=source_item[2],
            session_key=session_key, rank=1., amplitude_curve=np.zeros(len(e))))
        retained_donors.append((donor_channel, key))
    decisions = {}
    for ch, (cal, validation, final) in roles.items():
        if min(len(cal), len(validation)) < LIMITS['minimum_events_per_split']:
            continue
        x, sr, origin, end = data[ch]
        tt = origin+np.arange(round(len(x)*RATE/sr))/RATE
        scores = []
        unsupported_calibration = 0
        for trial in trials:
            if trial['reference_channel'] == ch:
                continue
            e, support, ref_origin = curves[trial['key']]
            e = np.interp(tt-ref_origin, np.arange(len(e))/RATE, e, left=0., right=0.)
            calibration_support = np.interp(tt-ref_origin, np.arange(len(support))/RATE, support, left=0., right=0.)
            first_cal = round(min(event['start'] for event in cal)*RATE)
            last_cal = round(max(event['end'] for event in cal)*RATE)
            if not np.all(calibration_support[first_cal:last_cal] >= .95):
                unsupported_calibration += 1
                continue
            measured = evaluate_events(x, sr, e, cal)
            score = loss(measured)
            if score is not None:
                scores.append((score, trial['key'], trial, e))
        if not scores:
            decisions[ch] = dict(passed=False, key=None, quality_level='unqualified',
                reason='No candidate has uninterrupted calibration support',
                quality_diagnostics=dict(failed_predicates=['calibration_support'],
                    unsupported_calibration_candidates=unsupported_calibration))
            continue
        best_loss = min(value[0] for value in scores)
        comparable = [value for value in scores if value[0] <= best_loss+max(SETTINGS['calibration_tie_absolute'], best_loss*SETTINGS['calibration_tie_relative'])]
        timing_only = {row['channel'] for row in inv['channels'] if row['role'] == 'timing_only'}
        selected_loss, _, selected, e = max(comparable, key=lambda value: (
            value[2]['rank'], value[2]['reference_channel'] in timing_only, -value[0], value[1]))
        raw_e, support, ref_origin = curves[selected['key']]
        support = np.interp(tt-ref_origin, np.arange(len(support))/RATE, support, left=0., right=0.)
        supported = all(np.min(support[round(event['start']*RATE):round(event['end']*RATE)]) >= .95 for event in cal+validation)
        # A later safe island does not inherit validation from before a dropout.
        first = round(min(event['start'] for event in cal+validation)*RATE)
        last = round(max(event['end'] for event in cal+validation)*RATE)
        supported = supported and bool(np.all(support[first:last] >= .95))
        metrics = evaluate_events(x, sr, e, validation)
        no_motion = evaluate_events(x, sr, np.zeros_like(e), validation)
        wrong = evaluate_events(x, sr, -e, validation)
        am = np.interp(tt-selected['acquisition_start'], np.arange(len(selected['amplitude_curve']))/RATE,
                       selected['amplitude_curve'], left=0., right=0.)
        am *= float(np.std(e))/max(float(np.std(am)), 1e-12)
        amplitude_control = evaluate_events(x, sr, np.clip(am, -.049, .049), validation)
        shifted = [evaluate_events(x, sr, shifted_curve(e, shift), validation) for shift in LIMITS['wrong_time_seconds']]
        evidence = evidence_strength(cal, validation)
        quality = classify_numeric_quality(metrics,
            dict(wrong_sign=wrong, no_motion=no_motion, amplitude=amplitude_control),
            dict(zip(map(str, LIMITS['wrong_time_seconds']), shifted)),
            supported=supported, independent_windows=evidence['level'] == 'multiple_time_windows')
        supported_from = first
        while supported_from > 0 and support[supported_from-1] >= .95:
            supported_from -= 1
        decisions[ch] = dict(passed=quality['passed'], key=selected['key'],
            calibration_loss=selected_loss, best_calibration_loss=best_loss,
            holdout=metrics, evidence=evidence, quality_level=quality['quality_level'],
            restoration_target_met=quality['restoration_target_met'], quality_diagnostics=quality,
            supported_from_seconds=origin+supported_from/RATE,
            acquisition_start_seconds=origin, acquisition_end_seconds=end,
            reason=('Independent frozen-choice restoration goal met' if quality['restoration_target_met'] else
                    'Strong independently supported improvement; restoration goal not met' if quality['passed'] else
                    'Frozen choice failed: '+', '.join(quality['failed_predicates'])))
    # Freeze one donor and the target's best distinct carrier before examining
    # group validation. A failed group never cycles through alternatives here.
    donors = sorted([(ch, d['key']) for ch,d in decisions.items() if d['passed']]+retained_donors)
    for ch, (cal, validation, final) in roles.items():
        if cal or validation or ch not in data or not donors:
            continue
        donor_channel, reference_key = donors[0]
        selected = next(trial for trial in trials if trial['key'] == reference_key)
        own = max((candidate for candidate in candidates if candidate['channel'] == ch
                   and candidate['eligible'] and abs(candidate['carrier_hz']-selected['carrier_hz']) >= GROUP_SETTINGS['minimum_frequency_separation_hz']),
                  key=lambda c:(c['rank'],c['key']), default=None)
        if own is None:
            continue
        x, sr, origin, end = data[ch]
        trace = traces[own['key']]
        own_key = own['key']+'_stream_signed_2_30'
        if own_key not in curves:
            amplitude = max(float(np.median(trace[3][400:-400])), 1e-12)
            e, gate = track_array(x, sr, own['carrier_hz'], trace[-1]['capture_hz'], amplitude)
            curves[own_key] = (e, gate, origin)
            trials.append(dict(key=own_key, reference_channel=ch, carrier_hz=own['carrier_hz'],
                capture_hz=trace[-1]['capture_hz'], amplitude_reference=amplitude,
                method='stream_signed_2_30', modulation_band_hz=[2.,30.],
                acquisition_start=origin, rank=own['rank'], amplitude_curve=trace[3]/amplitude-1.))
        independent, own_gate, _ = curves[own_key]
        e, gate, ref_origin = curves[reference_key]
        times = origin+np.arange(len(independent))/RATE
        applied = np.interp(times-ref_origin, np.arange(len(e))/RATE, e, left=0., right=0.)
        gate = np.interp(times-ref_origin, np.arange(len(gate))/RATE, gate, left=0., right=0.)
        lo = max(round((low-origin)*RATE), 2*RATE)
        hi = min(len(independent)-RATE, round((high-origin)*RATE))
        group = group_evidence(applied, independent, np.minimum(gate, own_gate), trace[3], lo, hi)
        decisions[ch] = dict(passed=group['passed'], key=reference_key, support_key=own_key,
            quality_level='supported_improvement' if group['passed'] else 'unqualified', restoration_target_met=False,
            donor_channel=donor_channel, transfer_group=group,
            evidence=dict(level='multiple_time_windows', transfer_group_supported=group['passed'],
                          listening_approved=False, production_accepted=False),
            reason='Independent playback-motion group passed' if group['passed'] else 'Frozen playback group failed; passage not retried')
    clean_trials = [{k: v for k, v in trial.items() if k != 'amplitude_curve'} for trial in trials]
    return decisions, clean_trials, budget


def plan_tape(source, output, *, start=0., duration=None, monitor_seconds=60.,
              chunk_seconds=7., context_seconds=4., candidates_per_channel=8,
              reference_limit=8, max_windows=None, resume=False, reuse=True):
    source, output = Path(source).resolve(), Path(output).resolve()
    if source == output or source in output.parents or output in source.parents:
        raise ValueError('Input and output must be non-overlapping')
    if not np.isfinite([start, monitor_seconds, chunk_seconds, context_seconds]).all() or start < 0 or monitor_seconds < 20 or chunk_seconds <= 0 or context_seconds < 4:
        raise ValueError('Use nonnegative start, monitor >=20 s, positive I/O chunks and context >=4 s')
    if duration is not None and (not math.isfinite(duration) or duration < 15):
        raise ValueError('Planning duration must be at least 15 seconds')
    if candidates_per_channel < 1 or reference_limit < 1 or (max_windows is not None and max_windows < 1):
        raise ValueError('Search/window limits must be positive')
    inv = inventory(source)
    end = min(inv['duration_seconds'], start+duration) if duration is not None else inv['duration_seconds']
    if start >= end:
        raise ValueError('Planning range is outside tape coverage')
    parameters = dict(start=start, duration=duration, monitor_seconds=monitor_seconds,
                      context_seconds=context_seconds, candidates_per_channel=candidates_per_channel,
                      reference_limit=reference_limit)
    signature = dict(inventory=inv, parameters=parameters, methodology=METHODOLOGY,
                     implementation_sha256=implementation_digest(), policy_sha256=policy_digest())
    output.mkdir(parents=True, exist_ok=True)
    if (output/'plan.json').exists():
        saved = load_saved_plan(output/'plan.json', require_complete=False)
        if any(saved.get(k) != v for k, v in signature.items()):
            raise ValueError('Stored continuous plan sources/settings/implementation changed; use a new destination')
        if saved.get('complete') and reuse:
            return saved
        if not resume and not reuse:
            raise ValueError('Use --resume for an incomplete continuous plan')
        _, state_raw = checked_artifact(output, saved.get('checkpoint_file', 'checkpoint.json'),
                                        saved['checkpoint_sha256'])
        state = json.loads(state_raw)
        sessions = {row['key']: Session.restore(output, row, chunk_seconds) for row in state['sessions']}
        targets, variants, active = state['targets'], state['variants'], {int(k):v for k,v in state['active'].items()}
        cursor, sequence = state['cursor'], state['sequence']
        records, final_events = state['records'], state['final_events']
        last_checked = {int(k):v for k,v in state['last_checked'].items()}
        groups = {int(k):v for k,v in state.get('groups', {}).items()}
        last_group_search = {int(k):v for k,v in state.get('last_group_search', {}).items()}
        evidence_ledger = AcquisitionEvidence.restore(state['acquisition_evidence'])
    else:
        if any(p.name != 'audits' or not p.is_dir() for p in output.iterdir()):
            raise ValueError('Use a fresh output folder for continuous planning')
        sessions, variants, active, records, final_events, last_checked = {}, [], {}, [], {}, {}
        targets = [dict(channel=row['channel'], action='leave_uncorrected', selected=None,
                         reason='No independently validated reference', reference_spans=[],
                         numeric_transfer_passed=False,
                         evidence=dict(level='sparse', listening_approved=False, production_accepted=False))
                   for row in inv['channels'] if row['role'] != 'timing_only']
        cursor, sequence = start, 0
        groups, last_group_search = {}, {}
        evidence_ledger = AcquisitionEvidence(context_seconds=context_seconds)
    by_channel = {row['channel']: row for row in inv['channels']}
    by_target = {row['channel']: row for row in targets}
    def publish(complete):
        generation = uuid.uuid4().hex
        state = dict(cursor=cursor, sequence=sequence, active=active, targets=targets,
                     sessions=[s.checkpoint(generation) for s in sessions.values()], variants=variants,
                     records=records, final_events=final_events, last_checked=last_checked,
                     groups=groups, last_group_search=last_group_search, acquisition_evidence=evidence_ledger.state())
        checkpoint_file = f'checkpoint_{generation}.json'
        write_json(output/checkpoint_file, state)
        detail = dict(id='continuous', kind='continuous_reference', status='analyzed' if complete else 'pending',
                      start_seconds=start, end_seconds=end, analysis_start=start, analysis_end=end,
                      targets=targets, variants=variants, monitor_records=records, final_holdouts=final_events,
                      maps=dict(rate=RATE, streams={k:s.descriptor() for k,s in sessions.items()}))
        detail_file = f'continuous_{generation}.json'
        write_json(output/detail_file, detail)
        summary = dict(detail, detail_file=detail_file, detail_sha256=digest(output/detail_file))
        for name in ('monitor_records', 'final_holdouts'):
            summary.pop(name)
        plan = dict(signature, schema=2, kind='continuous_reference', complete=complete,
                    source_validation='verified', full_tape=start == 0 and duration is None,
                    regions=[summary], checkpoint_file=checkpoint_file, checkpoint_sha256=digest(output/checkpoint_file),
                    summary=dict(regions=1, analyzed=int(complete), reference_sessions=len(sessions),
                        monitor_windows=len(records), duration_weighted_coverage=[dict(channel=t['channel'],
                            seconds=dict(direct_numeric=sum(s['end_seconds']-s['start_seconds'] for s in t['reference_spans'] if s['selected'] and s.get('evidence_kind') != 'transfer_group'),
                                         transfer_group=sum(s['end_seconds']-s['start_seconds'] for s in t['reference_spans'] if s['selected'] and s.get('evidence_kind') == 'transfer_group'),
                                         no_evidence=sum(s['end_seconds']-s['start_seconds'] for s in t['reference_spans'] if not s['selected'])),
                            reference_switches=sum(a != b for a,b in zip(
                                [s['selected'] for s in t['reference_spans'] if s['selected']],
                                [s['selected'] for s in t['reference_spans'] if s['selected']][1:]))) for t in targets]))
        expected = sum(max(0., min(cursor, r['end_seconds'])-max(start, r['start_seconds']))
                       for r in inv['channels'] if r['role'] != 'timing_only')
        accepted = sum(s['end_seconds']-s['start_seconds'] for t in targets for s in t['reference_spans'] if s.get('selected'))
        partial = sum(s['end_seconds']-s['start_seconds'] for t in targets for s in t['reference_spans']
                      if s.get('selected') and s.get('quality_level') == 'supported_improvement')
        plan['optimization_outcome'] = dict(status='no_supported_correction' if not accepted else 'supported_correction_available',
            analyzed_channel_seconds=expected, supported_channel_seconds=accepted,
            supported_improvement_seconds=partial, restoration_goal_seconds=accepted-partial,
            correction_coverage=accepted/expected if expected else 0., production_accepted=False)
        write_json(output/'plan.json', plan)

    # Establish a commit point before acquisition can leave orphan files.
    if not (output/'plan.json').exists():
        publish(False)
    count = 0
    while cursor < end-1e-8 and (max_windows is None or count < max_windows):
        high = min(end, cursor+monitor_seconds)
        data = local_data(inv, max(0., cursor-context_seconds), min(inv['duration_seconds'], high+context_seconds))
        roles = {}
        for ch, (x, sr, origin, stop) in data.items():
            if ch not in by_target:
                continue
            # Include a beep straddling a monitor boundary once it is complete;
            # ingestion deduplicates repeated overlapping-context detections.
            events = [e for e in beeps(x, sr) if origin+e['start'] >= max(start, by_channel[ch]['start_seconds'], cursor-.5)
                      and origin+e['end'] <= high]
            fresh = evidence_ledger.ingest(ch, events, origin=origin)
            roles[ch] = tuple([dict(e, start=e['start_seconds']-origin, end=e['end_seconds']-origin)
                               for e in fresh if e['role'] == name]
                              for name in ('calibration', 'validation', 'final'))
        observations, unresolved, donors_for_window, health = [], [], [], {}
        for target in targets:
            ch = target['channel']
            low, stop = max(cursor, by_channel[ch]['start_seconds']), min(high, by_channel[ch]['end_seconds'])
            if stop <= low:
                continue
            if ch not in data:
                add_span(target, low, stop, None, 'Insufficient original context')
                active.pop(ch, None)
                groups.pop(ch, None)
                continue
            cal, validation, final = roles[ch]
            origin = data[ch][2]
            final_events.setdefault(str(ch), []).extend(dict(e, start_seconds=origin+e['start'], end_seconds=origin+e['end']) for e in final)
            key = active.get(ch)
            if key:
                evidence_ledger.mark_attempt(ch, high)
                session = sessions[key]
                session.advance(high+context_seconds)
                e, support = projected(session, data[ch])
                core = support[max(0, round((low-origin)*RATE)):min(len(support), round((stop-origin)*RATE))]
                metrics = evaluate_events(data[ch][0], data[ch][1], e, cal+validation)
                passed = len(core) > 0 and float(np.mean(core >= .95)) >= SETTINGS['minimum_support']
                reason = 'Continuing validated reference'
                if key not in health:
                    ref_data = data.get(session.row['channel'])
                    health[key] = competing_carriers(ref_data[0], ref_data[1], session.tracker.hz,
                        session.tracker.capture) if ref_data else []
                if health[key]:
                    passed = False
                    reason = 'Competing reference peaks detected; fresh acquisition required'
                group = None
                lifetime_stop = stop
                if ch in groups:
                    own = sessions[groups[ch]['support_key']]
                    own.advance(high+context_seconds)
                    independent, own_support = projected(own, data[ch])
                    own_a, own_b = supported_interval(own, low, stop)
                    ref_a, ref_b = supported_interval(session, low, stop)
                    lifetime_stop = min(own_b, ref_b) if max(own_a, ref_a) <= low+1/RATE else low
                    try:
                        trace = trajectory(data[ch][0], data[ch][1], own.tracker.hz, own.tracker.capture)
                        group = group_evidence(e, independent, np.minimum(support, own_support), trace[3],
                            max(0, round((low-origin)*RATE)), min(len(e), round((lifetime_stop-origin)*RATE)))
                        passed = passed and group['passed']
                    except ValueError:
                        passed = False
                    reason = 'Playback group membership lost; reacquisition required'
                    if passed:
                        last_checked[ch] = high
                        final_events[str(ch)].extend(dict(event, start_seconds=origin+event['start'],
                            end_seconds=origin+event['end'], carrier_hz=own.tracker.hz,
                            capture_hz=own.tracker.capture) for event in group['reserved_events'])
                elif metrics:
                    passed = passed and monitor_pass(metrics, target.get('quality_level', 'restoration_target_met'))
                    last_checked[ch] = high
                    if not monitor_pass(metrics, target.get('quality_level', 'restoration_target_met')):
                        reason = 'Independent check deterioration; correction bypassed and search deferred to fresh passage'
                elif high-last_checked.get(ch, cursor) > SETTINGS['maximum_unchecked_seconds']:
                    passed, reason = False, 'Independent evidence expired'
                if passed:
                    variant = next(v for v in variants if v['key'] == key)
                    if ch not in groups:
                        donors_for_window.append((ch, key, variant, session))
                    continued = retain_span(target, session, low, lifetime_stop, key, variant)
                    if lifetime_stop < stop-1e-8:
                        add_span(target, lifetime_stop, stop, None, 'Playback group carrier support lost; reacquisition required')
                        continued = False
                    if not continued:
                        active.pop(ch)
                        if ch in groups:
                            last_group_search[ch] = high-300.
                        groups.pop(ch, None)
                    observations.append(dict(channel=ch, selected=key, action='continue', metrics=metrics,
                                             support_fraction=float(np.mean(core >= .95)),
                                             physical_support_lost=not continued))
                    continue
                active.pop(ch)
                if ch in groups:
                    last_group_search[ch] = high-300.
                groups.pop(ch, None)
                add_span(target, low, stop, None, reason if reason != 'Continuing validated reference'
                         else 'Reference support lost; reacquisition required')
                observations.append(dict(channel=ch, selected=key, action='stop', reason=reason, metrics=metrics))
                # Failure is retained. Search restarts on the next unused passage.
                continue
            unresolved.append(ch)
        if unresolved:
            proposals = {ch: evidence_ledger.propose(ch, min(high, by_channel[ch]['end_seconds']),
                available_start=max(start, by_channel[ch]['start_seconds']), available_end=by_channel[ch]['end_seconds'])
                for ch in unresolved if ch in roles}
            ready = {ch:p for ch,p in proposals.items() if p['status'] == 'ready_for_search'}
            incumbents = donors_for_window
            group_search = bool(incumbents) and any(high-last_group_search.get(ch, start-300.) >= 300. for ch in unresolved)
            if ready or group_search:
                search_low = min((p['analysis_start'] for p in ready.values()), default=max(0., cursor-context_seconds))
                search_data = local_data(inv, search_low, min(inv['duration_seconds'], high+context_seconds))
                wanted_roles = {ch: local_roles(proposals[ch], search_data[ch][2]) if ch in ready else tuple(
                    [dict(e, start=e['start_seconds']-search_data[ch][2], end=e['end_seconds']-search_data[ch][2]) for e in part]
                    for part in roles[ch]) for ch in unresolved if ch in search_data and ch in roles}
                decisions, trials, budget = acquire(inv, search_data, wanted_roles,
                    min((p['start_seconds'] for p in ready.values()), default=cursor), high,
                    reference_limit, candidates_per_channel, incumbents)
                budget['acquisition_horizons'] = {str(ch):p['horizon_seconds'] for ch,p in ready.items()}
                for ch in ready:
                    evidence_ledger.mark_attempt(ch, high)
                last_group_search.update({ch:high for ch in unresolved})
            else:
                search_data = data
                decisions, trials, budget = {}, [], dict(exhausted=False, reason='No new qualifying independent evidence; discovery skipped',
                    evidence_waiting={str(ch):p['counts_by_horizon'] for ch,p in proposals.items()})
            local_to_session = {}
            for ch in unresolved:
                target, row = by_target[ch], by_channel[ch]
                low, stop = max(cursor, row['start_seconds']), min(high, row['end_seconds'])
                decision = decisions.get(ch)
                if decision and decision['passed']:
                    if ch in ready and not decision.get('support_key'):
                        low = max(row['start_seconds'], ready[ch]['start_seconds'], decision['supported_from_seconds'])
                        reopen_pending_span(target, low)
                    local_key = decision['key']
                    trial = next(v for v in trials if v['key'] == local_key)
                    if trial.get('session_key'):
                        local_to_session[local_key] = trial['session_key']
                    if local_key not in local_to_session:
                        key, sequence = allocate_session_key(output, sequence)
                        ref = by_channel[trial['reference_channel']]
                        session = Session(output, key, ref, trial['acquisition_start'], trial['carrier_hz'],
                                          trial['capture_hz'], trial['amplitude_reference'], trial['method'], chunk_seconds)
                        sessions[key] = session
                        session.advance(high+context_seconds)
                        local_to_session[local_key] = key
                        variants.append(dict(trial, key=key, reference_identity=dict(path=ref['path'],
                                             bytes=ref['bytes'], mtime_ns=ref['mtime_ns'])))
                    key = local_to_session[local_key]
                    active[ch] = key
                    last_checked[ch] = high
                    if decision.get('support_key'):
                        own_trial = next(v for v in trials if v['key'] == decision['support_key'])
                        own_key, sequence = allocate_session_key(output, sequence)
                        own = Session(output, own_key, row, own_trial['acquisition_start'], own_trial['carrier_hz'],
                                      own_trial['capture_hz'], own_trial['amplitude_reference'], own_trial['method'], chunk_seconds)
                        own.advance(high+context_seconds)
                        sessions[own_key] = own
                        variants.append(dict(own_trial, key=own_key))
                        groups[ch] = dict(support_key=own_key, donor_channel=decision['donor_channel'])
                        final_events[str(ch)].extend(dict(event, start_seconds=search_data[ch][2]+event['start'],
                            end_seconds=search_data[ch][2]+event['end'], carrier_hz=own_trial['carrier_hz'],
                            capture_hz=own_trial['capture_hz']) for event in decision['transfer_group']['reserved_events'])
                        target['transfer_group'] = decision['transfer_group']
                    target.update(action='group_render_and_review' if ch in groups else 'render_and_review', selected=target['selected'] or key,
                                  reason='Independently validated continuous reference spans',
                                  numeric_transfer_passed=True, evidence=decision['evidence'],
                                  quality_level=decision.get('quality_level', 'supported_improvement'),
                                  restoration_target_met=decision.get('restoration_target_met', False))
                    variant = next(v for v in variants if v['key'] == key)
                    continued = retain_span(target, sessions[key], low, stop, key, variant, allow_leading_gap=True)
                    if not continued:
                        active.pop(ch)
                        groups.pop(ch, None)
                else:
                    add_span(target, low, stop, None, decision['reason'] if decision else 'Insufficient independent calibration/validation events')
                observations.append(dict(channel=ch, action='acquire' if ch in active else 'bypass', decision=decision))
        else:
            budget = dict(exhausted=False, reason='Incumbents retained; no candidate search')
        record_file = f'monitor_{len(records):06d}.json'
        write_json(output/record_file, dict(start_seconds=cursor, end_seconds=high,
                    observations=observations, search_budget=budget, final_holdouts_evaluated=False))
        records.append(dict(file=record_file, sha256=digest(output/record_file)))
        cursor, count = high, count+1
        complete = cursor >= end-1e-8
        if inventory(source) != inv:
            raise ValueError('Originals changed during continuous planning')
        publish(complete)
        rejected = {}
        accepted_improvements = 0
        for observation in observations:
            decision = observation.get('decision')
            if decision and decision.get('passed'):
                accepted_improvements += int(decision.get('quality_level') == 'supported_improvement')
            elif decision:
                for predicate in decision.get('quality_diagnostics', {}).get('failed_predicates', []):
                    rejected[predicate] = rejected.get(predicate, 0)+1
        search_status = budget.get('reason', f"searched {budget.get('eligible_candidates', 0)} eligible candidates")
        print(f'{inv["tape"]}: continuous evidence through {cursor:.3f}s; {len(active)} active targets, '
              f'{len(sessions)} reference lifetimes; {search_status}; '
              f'new supported improvements={accepted_improvements}; rejected={rejected}', flush=True)
    return load_saved_plan(output/'plan.json', require_complete=False)
