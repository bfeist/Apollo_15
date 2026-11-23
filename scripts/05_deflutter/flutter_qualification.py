"""Qualification: one tape-level decision per interval, validation and the QA batch.

Decisions are ``apply``, ``bypass_unsupported``, ``bypass_unresolved_contradiction``
or ``incomplete`` (unfinished analysis; never silently a bypass). They are
resolved once on the canonical tape-time grid: incomplete and contradicted
intervals take precedence, so an overlapping passing window cannot leak
correction back into them. Applied windows' census curves are joined once into
``qualified.f64``; stage 05a imports that stream with the contradiction
intervals as exclusions for every source, integrates once and applies the same
inverse map to every output channel. Missing beeps never prevent application
and there is no per-output-channel gate or sign choice.
"""
from collections import Counter, defaultdict
import math
from pathlib import Path
import tempfile
import time

import numpy as np

import flutter_analysis as analysis
import flutter_census as census
import flutter_detection as detection
import flutter_followup as followup
import qualification_policy as rules

RATE = census.RATE
SCHEMA = 1
DEFAULT_CORPUS = analysis.HERE.parents[1]/'reports'/'flutter_census_20261115'
REGRESSION_SETS = dict(confirmation=('p1_confirm_v2',), sweep3=('p1_sweep3', 'p1_sweep3_rerun'))
MATCH_SECONDS = .1

sha256, digest, read_json, atomic_json = detection.sha256, detection.digest, detection.read_json, detection.atomic_json
union, subtract, intersect, total = analysis.union, analysis.subtract, analysis.intersect, analysis.total


# ---------------------------------------------------------------- decisions

def decision_beeps(result, events):
    """Match one reread per physical decision event on a measured channel."""
    matches = {}
    for beep in result.get('beeps', []):
        nearby = [(abs(e['start']-beep['start']), e['id'], e) for e in events
                  if abs(e['start']-beep['start']) <= MATCH_SECONDS]
        if nearby:
            distance, eid, event = min(nearby)
            if event['partition'] != 'decision' or beep['channel'] not in event['windows'][result['window']]['channels']:
                continue
            rank = (distance, beep['channel'])
            if eid not in matches or rank < matches[eid][0]:
                matches[eid] = rank, beep
    return [beep for _, beep in matches.values()]


def segments(duration, layers):
    """Label canonical tape time; earlier layers take precedence."""
    points = {0., float(duration)}
    for _, intervals in layers:
        for a, b in intervals:
            points.update((max(0., a), min(duration, b)))
    points = sorted(p for p in points if 0 <= p <= duration)
    out = []
    for a, b in zip(points, points[1:]):
        if b-a <= 1e-9:
            continue
        middle = (a+b)/2
        label = next((name for name, intervals in layers if any(x <= middle < y for x, y in intervals)), 'unmeasured')
        if out and out[-1]['label'] == label and abs(out[-1]['end_seconds']-a) < 1e-9:
            out[-1]['end_seconds'] = b
        else:
            out.append(dict(label=label, start_seconds=a, end_seconds=b))
    return out


def mask_on_grid(intervals, frames):
    mask = np.zeros(frames, bool)
    for a, b in intervals:
        lo, hi = math.ceil(a*RATE-1e-9), math.ceil(b*RATE-1e-9)
        mask[max(0, lo):min(frames, hi)] = True
    return mask


def qualify_tape(bundle, tape_row, rows, events, results, out_folder):
    config = bundle['config']['identity']
    policy, seed = config['policy'], config['seed']
    tape = tape_row['tape']
    census_bundle = detection.load_census(bundle['census_dir'], tape)
    if census_bundle['evidence_sha256'] != tape_row['evidence_sha256']:
        raise analysis.AnalysisError(f'{tape} census evidence changed since the assessment')
    evidence = census_bundle['evidence']
    settings = detection.settings_of(census_bundle['config'])
    duration = tape_row['duration_seconds']
    events_by_window = defaultdict(list)
    for event in events:
        for wid in event['windows']:
            events_by_window[wid].append(event)
    decisions = []
    for row in rows:
        result = results.get((tape, row['id']))
        if result is not None and result.get('status') == 'complete':
            beeps = decision_beeps(result, events_by_window[row['id']])
            result = dict(result, beep_state=rules.pooled_beeps(beeps, followup.LAGS, policy))
        decision = rules.resolve(row, result, policy)
        decisions.append(dict(window=row['id'], block=row['block'], start_seconds=row['start_seconds'],
                              end_seconds=row['end_seconds'], valid=row['valid'], curve_index=row['curve_index'],
                              eligible=row['eligible'], flags=row['flags'], followup_status=None if result is None
                              else result.get('status'), followup_job=None if result is None else result.get('job_key'),
                              beep_state=None if result is None else result.get('beep_state'),
                              hypothesis='census_consensus' if decision['decision'] == 'apply' else None,
                              **{'class': row['class']}, **decision))
    by = {name: union([d['valid'] for d in decisions if d['decision'] == name]) for name in rules.DECISIONS}
    analysed = union([r['valid'] for r in rows])
    applied = subtract(by['apply'], union(by['incomplete']+by['bypass_unresolved_contradiction']))
    labelled = segments(duration, [('incomplete', by['incomplete']),
                                   ('bypass_unresolved_contradiction', by['bypass_unresolved_contradiction']),
                                   ('apply', applied), ('bypass_unsupported', analysed)])
    for s in labelled:
        s['decision'] = 'bypass_unsupported' if s['label'] == 'unmeasured' else s['label']
        s['reason'] = 'not analysable by the census windows' if s['label'] == 'unmeasured' else None
        del s['label']
    curves = analysis.Curves(bundle['census_dir']/tape)
    chosen = [d for d in decisions if d['decision'] == 'apply']
    speed, support = census.join_windows([(d['valid'][0], curves(dict(block=d['block'], curve_index=d['curve_index'])))
                                          for d in chosen], duration, settings)
    keep = mask_on_grid(applied, len(speed))
    speed, support = np.where(keep, speed, 0.), np.where(keep, support, 0.)
    out_folder.mkdir(parents=True, exist_ok=True)
    pending = out_folder/'qualified.f64.partial'
    np.column_stack((speed, support)).astype('<f8').tofile(pending)
    pending.replace(out_folder/'qualified.f64')
    seconds = {name: round(sum(s['end_seconds']-s['start_seconds'] for s in labelled if s['decision'] == name), 6)
               for name in rules.DECISIONS}
    record = dict(schema=SCHEMA, kind='tape_qualification', analysis_id=bundle['config']['analysis_id'],
                  census_run_id=evidence['run_id'], tape=tape, policy_version=policy['version'],
                  policy_sha256=config['policy_sha256'], seed=seed, partition=tape_row['partition'],
                  evidence_sha256=census_bundle['evidence_sha256'], inventory_sha256=evidence['inventory_sha256'],
                  inventory=evidence['inventory'], duration_seconds=duration, segments=labelled, seconds=seconds,
                  qualified_seconds=total(applied), qualified_fraction=total(applied)/duration if duration else 0.,
                  complete=seconds['incomplete'] == 0,
                  stream=dict(file='qualified.f64', sha256=sha256(out_folder/'qualified.f64'), frames=len(speed),
                              rate=RATE, start_seconds=0., dtype='<f8', columns=['speed', 'support']),
                  windows=decisions, counts=dict(Counter(d['decision'] for d in decisions)),
                  interpretation=('One tape-level decision per interval for every output channel; '
                                  'not listening approval'))
    atomic_json(out_folder/'decisions.json', record)
    return record


@analysis.bundle_writer
def qualify(analysis_dir, *, allow_incomplete=False, progress=print):
    bundle = analysis.open_analysis(analysis_dir)
    folder = bundle['dir']/'qualification'
    if (folder/'summary.json').is_file():
        return verify_qualification(bundle)
    analysis.check_code(bundle['config'])
    queue = analysis.queue_items(bundle)
    status_path = bundle['dir']/'followup'/'status.json'
    finished = status_path.is_file() and read_json(status_path)['complete']
    if queue and not finished and not allow_incomplete:
        raise analysis.AnalysisError('The follow-up pass is not complete; run it first, or pass --allow-incomplete '
                                     'to freeze unfinished windows as incomplete')
    preview = bool(queue) and not finished
    if preview:
        previews = bundle['dir']/'qualification_previews'
        previews.mkdir(exist_ok=True)
        folder = Path(tempfile.mkdtemp(prefix='partial-', dir=previews))
    folder.mkdir(exist_ok=True)
    queued = defaultdict(set)
    for item in queue:
        queued[item['tape']].add((item['tape'], item['window']))
    tapes = analysis.tape_rows(bundle)
    rows = analysis.window_rows(bundle)
    events = analysis.event_rows(bundle)
    files, tape_summary = {}, []
    for tape, tape_row in sorted(tapes.items()):
        if tape_row['status'] != 'complete':
            tape_summary.append(dict(tape=tape, status=tape_row['status'], reason=tape_row.get('reason')))
            continue
        results = followup.load_results(bundle, tape=tape, require_complete=bool(queued[tape]) and not preview)
        for key in queued[tape] - results.keys():
            if not preview:
                raise analysis.AnalysisError(f'No follow-up result for {key}')
            results[key] = dict(status='missing')
        record = qualify_tape(bundle, tape_row, [r for r in rows if r['tape'] == tape],
                              [e for e in events if e['tape'] == tape], results, folder/tape)
        files[f'{tape}/decisions.json'] = sha256(folder/tape/'decisions.json')
        files[f'{tape}/qualified.f64'] = record['stream']['sha256']
        tape_summary.append(dict(tape=tape, status='qualified' if record['complete'] else 'incomplete',
                                 partition=record['partition'], duration_seconds=record['duration_seconds'],
                                 seconds=record['seconds'], qualified_fraction=record['qualified_fraction'],
                                 counts=record['counts']))
        progress(f'{tape}: qualified {record["qualified_fraction"]:.1%}, '
                 + ', '.join(f'{k} {v:.0f} s' for k, v in record['seconds'].items() if v))
    done = [t for t in tape_summary if 'seconds' in t]
    seconds = {name: sum(t['seconds'][name] for t in done) for name in rules.DECISIONS}
    summary = dict(schema=SCHEMA, kind='qualification_summary', analysis_id=bundle['config']['analysis_id'],
                   policy_version=bundle['config']['identity']['policy']['version'],
                   complete=bool(done) and not preview and all(t['status'] in ('qualified', 'excluded') for t in tape_summary),
                   tapes=tape_summary,
                   seconds=seconds, tape_seconds=sum(t['duration_seconds'] for t in done),
                   qualified_fraction_distribution=analysis.percentiles([t['qualified_fraction'] for t in done]),
                   followup_complete=finished or not queue, files=files,
                   basis='Sums of per-tape tape-time unions; never channel-seconds')
    if preview:
        summary['preview_dir'] = str(folder)
    save_frozen(folder/'summary.json', summary)
    if preview:
        return summary
    return verify_qualification(bundle)


def verify_qualification(bundle, tape=None):
    path = bundle['dir']/'qualification'/'summary.json'
    if not path.is_file():
        raise analysis.AnalysisError('Qualification has not been run for this analysis')
    summary = read_frozen(path, bundle)
    if summary['kind'] != 'qualification_summary':
        raise analysis.AnalysisError('Qualification summary belongs to another analysis')
    files = summary['files'] if tape is None else [f'{tape}/decisions.json', f'{tape}/qualified.f64']
    for name in files:
        if name not in summary['files']:
            raise analysis.AnalysisError(f'No qualification artifact for {name}')
        if sha256(bundle['dir']/'qualification'/name) != summary['files'][name]:
            raise analysis.AnalysisError(f'Qualification artifact changed: {name}')
    return summary


def load_qualified(analysis_dir, tape):
    """Validated tape decisions and stream for stage 05a; incomplete is an error."""
    bundle = analysis.open_analysis(analysis_dir)
    summary = verify_qualification(bundle, tape)
    name = f'{tape}/decisions.json'
    path = bundle['dir']/'qualification'/name
    decisions = read_json(path)
    if decisions.get('kind') != 'tape_qualification' or decisions.get('tape') != tape \
            or decisions.get('analysis_id') != bundle['config']['analysis_id']:
        raise analysis.AnalysisError(f'Qualification decisions for {tape} belong to another analysis')
    if not decisions['complete']:
        raise analysis.AnalysisError(f'{tape} has incomplete qualification intervals; finish the follow-up pass')
    census_bundle = detection.load_census(bundle['census_dir'], tape)
    if census_bundle['evidence_sha256'] != decisions['evidence_sha256']:
        raise analysis.AnalysisError(f'{tape} census evidence changed since qualification')
    stream = path.with_name(decisions['stream']['file'])
    if (stream.name != 'qualified.f64' or stream.stat().st_size != decisions['stream']['frames']*16
            or summary['files'][f'{tape}/qualified.f64'] != decisions['stream']['sha256']):
        raise analysis.AnalysisError(f'{tape} qualified stream changed')
    return dict(analysis=bundle, decisions=decisions, decisions_path=path.resolve(),
                decisions_sha256=sha256(path), stream_path=stream.resolve(), census=census_bundle)


# ---------------------------------------------------------------- validation

def event_table(events):
    if not events:
        return dict(events=0)
    ratio = [e['ratio'] for e in events]
    return dict(events=len(events), median_ratio=round(float(np.median(ratio)), 4),
                worse_fraction=round(float(np.mean([r > 1 for r in ratio])), 4),
                median_hz=dict(identity=round(float(np.median([e['identity_hz'] for e in events])), 3),
                               normal=round(float(np.median([e['curve_hz'] for e in events])), 3)),
                controls=dict(reversed_ratio=round(float(np.median([e['wrong_sign_ratio'] for e in events])), 4),
                              shift_ratio=round(float(np.median([e['shift_ratio'] for e in events])), 4)))


def inside(events, intervals):
    return [e for e in events if any(a <= e['start'] < b for a, b in intervals)]


def applied_measurement(event, accepted):
    """Use the nearest contributing window that actually measured this event."""
    candidates = [wid for wid in event['windows'] if wid in accepted
                  and accepted[wid][0] <= event['start'] < accepted[wid][1]]
    if candidates:
        wid = min(candidates, key=lambda w: (abs(sum(accepted[w])/2-event['start']), w))
        return event['windows'][wid]


def save_frozen(path, record):
    record['record_sha256'] = digest(record)
    atomic_json(path, record)
    return record


def read_frozen(path, bundle):
    record = read_json(path)
    expected = record.pop('record_sha256', None)
    if record.get('analysis_id') != bundle['config']['analysis_id'] or digest(record) != expected:
        raise analysis.AnalysisError(f'Frozen analysis artifact changed: {path}')
    record['record_sha256'] = expected
    return record


@analysis.bundle_writer
def validate(analysis_dir):
    """Held-out evaluation of the frozen policy; written once per analysis ID."""
    bundle = analysis.open_analysis(analysis_dir)
    path = bundle['dir']/'validation.json'
    summary = verify_qualification(bundle)
    if path.is_file():
        report = read_frozen(path, bundle)
        if report['qualification_sha256'] != summary['record_sha256']:
            raise analysis.AnalysisError('Validation belongs to different qualification decisions')
        return report
    analysis.check_code(bundle['config'])
    partitions = {}
    collect_validation(bundle, summary, partitions)
    policy = bundle['config']['identity']['policy']
    criteria = policy['validation']
    report = dict(schema=SCHEMA, kind='qualification_validation', analysis_id=bundle['config']['analysis_id'],
                  qualification_sha256=summary['record_sha256'],
                  policy_version=policy['version'], policy_sha256=bundle['config']['identity']['policy_sha256'],
                  seed=bundle['config']['identity']['seed'], criteria=criteria, partitions={},
                  interpretation=('Held-out half of physical check-beep events only; decision-half events '
                                  'selected decisions and are development evidence. Ratios use the census '
                                  'window curve that measured each event. Development tapes include the '
                                  'November 15/17 inspected examples. Not listening approval.'))
    report['qualification_complete'] = summary['complete']
    score_validation(report, partitions)
    return save_frozen(path, report)


def collect_validation(bundle, summary, partitions, selected=None):
    """Pool held-out outcomes once per tape, including selections spanning immutable census runs."""
    rows = {(r['tape'], r['id']): r for r in analysis.window_rows(bundle)}
    events = [e for e in analysis.event_rows(bundle) if e['partition'] == 'held_out']
    for tape in summary['tapes']:
        if 'seconds' not in tape or selected is not None and tape['tape'] not in selected:
            continue
        decisions = read_json(bundle['dir']/'qualification'/tape['tape']/'decisions.json')
        part = partitions.setdefault(decisions['partition'], dict(tapes=[], duration=0., seconds=Counter(),
                                     applied=[], contradicted=[], unsupported=[], transfer=[], leaks=0,
                                     applied_segments=0, applied_windows=[], unscored=0))
        tape_events = [e for e in events if e['tape'] == tape['tape']]
        intervals = {name: [[s['start_seconds'], s['end_seconds']] for s in decisions['segments'] if s['decision'] == name]
                     for name in rules.DECISIONS}
        part['tapes'].append(tape['tape'])
        part['duration'] += decisions['duration_seconds']
        part['seconds'].update(decisions['seconds'])
        accepted = {d['window']: d['valid'] for d in decisions['windows'] if d['decision'] == 'apply'}
        for event in inside(tape_events, intervals['apply']):
            measured = applied_measurement(event, accepted)
            if measured is None:
                part['unscored'] += 1
                continue
            part['applied'].append(measured)
            if 'transfer' in measured:
                part['transfer'].append(measured['transfer'])
        part['contradicted'] += inside(tape_events, intervals['bypass_unresolved_contradiction'])
        part['unsupported'] += inside(tape_events, intervals['bypass_unsupported'])
        part['applied_segments'] += len(intervals['apply'])
        part['applied_windows'].extend(rows[(tape['tape'],wid)]['overlap_min_r'] for wid in accepted)
        stream = np.fromfile(bundle['dir']/'qualification'/tape['tape']/'qualified.f64', '<f8').reshape(-1, 2)
        outside = ~mask_on_grid(intervals['apply'], len(stream))
        part['leaks'] += int(np.count_nonzero(stream[outside, 1]))


def score_validation(report, partitions):
    criteria = report['criteria']
    for name, part in sorted(partitions.items()):
        applied = event_table(part['applied'])
        result = dict(tapes=sorted(part['tapes']), tape_seconds=part['duration'], seconds=dict(part['seconds']),
                      applied_coverage=part['seconds']['apply']/part['duration'] if part['duration'] else 0.,
                      held_out_applied=applied, applied_events_without_measurement=part['unscored'],
                      held_out_contradicted=event_table(part['contradicted']),
                      held_out_unsupported=event_table(part['unsupported']),
                      cross_channel_transfer=event_table(part['transfer']),
                      join=dict(applied_segments=part['applied_segments'],
                                overlap_min_r=analysis.percentiles(part['applied_windows']),
                                support_outside_applied_samples=part['leaks']))
        checks = dict(enough_events=applied['events'] >= criteria['min_held_out_events'],
                      degradation=applied.get('worse_fraction', 1.) <= criteria['max_worse_fraction'],
                      improvement=applied.get('median_ratio', math.inf) <= criteria['max_median_ratio'],
                      beats_controls=bool(applied['events'] and
                                          applied['median_ratio'] < min(applied['controls'].values())),
                      no_leaks=part['leaks'] == 0)
        result.update(checks=checks, passed=all(checks.values()))
        report['partitions'][name] = result
    report['passed'] = report['qualification_complete'] and report['partitions'].get('validation', {}).get('passed', False)



# ---------------------------------------------------------------- regression corpus

def load_corpus(folders):
    """Saved production-regression window records; later folders override."""
    windows = {}
    for folder in folders:
        for path in sorted(Path(folder).glob('A15_T*.json')):
            record = read_json(path)
            if 'window' not in record:
                continue
            w = dict(record['window'], block=f'regression:{record["tape"]}:{record["t0"]:g}')
            windows[(record['tape'], w['start_seconds'])] = (record['tape'], w, record.get('duplicate_groups', []))
    return list(windows.values())


def regression(corpus=DEFAULT_CORPUS, policy=None, seed=analysis.DEFAULT_SEED, gates=None):
    """Apply the pass-2 contradiction rule to the saved regression windows."""
    policy = policy or rules.POLICY
    gates = gates or census.SETTINGS['gates']
    report = dict(policy_version=policy['version'], policy_sha256=rules.policy_digest(policy), seed=seed,
                  corpus=str(corpus), sets={},
                  note=('No follow-up measurements exist for these single-read windows; contradicted windows are '
                        'presumptive bypasses pending follow-up. Overlap and boundary flags need a census.'))
    for name, folders in REGRESSION_SETS.items():
        items = load_corpus([Path(corpus)/f for f in folders])
        if not items:
            raise ValueError(f'No regression windows for {name} in {corpus}')
        by_tape = {}
        for tape, w, groups in items:
            by_tape.setdefault(tape, []).append((w, groups))
        kept, removed, rows = [], [], []
        for tape, entries in sorted(by_tape.items()):
            windows = [w for w, _ in entries]
            events, _ = analysis.collapse_events(tape, windows, {w['block']: g for w, g in entries}, [], seed, policy)
            accepted, contradicted_windows = {}, {}
            for w in windows:
                wid = analysis.window_id(w)
                physical = analysis.physical_summary(events, wid, 'decision')
                flags = rules.window_flags(w, physical, None, False, gates, policy)
                contradicted = bool(set(flags) & set(rules.CONTRADICTION_FLAGS))
                if w.get('eligible'):
                    (contradicted_windows if contradicted else accepted)[wid] = analysis.valid(w)
                rows.append(dict(tape=tape, start_seconds=w['start_seconds'], eligible=bool(w.get('eligible')),
                                 flags=flags, physical_decision=physical,
                                 presumptive=('bypass_unresolved_contradiction' if contradicted and w.get('eligible')
                                              else 'apply' if w.get('eligible') else 'bypass_unsupported')))
            for event in events:
                if event['partition'] != 'held_out':
                    continue
                adverse = any(a <= event['start'] < b for a, b in contradicted_windows.values())
                measured = applied_measurement(event, contradicted_windows if adverse else accepted)
                if measured is not None:
                    (removed if adverse else kept).append(measured)
        report['sets'][name] = dict(
            windows=len(rows), eligible=sum(r['eligible'] for r in rows),
            contradicted=[dict(tape=r['tape'], start_seconds=r['start_seconds'], flags=r['flags'],
                               physical_decision=r['physical_decision']) for r in rows
                          if r['presumptive'] == 'bypass_unresolved_contradiction'],
            flags=dict(Counter(f for r in rows for f in r['flags'])),
            held_out_kept=event_table(kept),
            held_out_removed=event_table(removed))
    return report


# ---------------------------------------------------------------- representative QA batch

def speech_features(x, rate):
    """The stage-07 voiced-speech features for one excerpt."""
    from scipy.ndimage import uniform_filter1d
    from speech_normalization import FRAME_SECONDS
    size = round(rate*FRAME_SECONDS)
    nfft = 2**math.ceil(math.log2(2*size))
    freq = np.fft.rfftfreq(nfft, 1/rate)
    band = (freq >= 150) & (freq <= 3400)
    window = np.hanning(size)
    count = len(x)//size
    frames = np.asarray(x[:count*size], np.float64).reshape(count, size)
    rms = np.sqrt(np.mean(frames*frames, axis=1))
    peak = np.max(np.abs(frames), axis=1)
    power = np.abs(np.fft.rfft((frames-frames.mean(axis=1, keepdims=True))*window, n=nfft))**2
    energy = power[:, band].sum(axis=1)+1e-30
    band_rms = np.sqrt(2*energy/(nfft*np.sum(window**2)))
    width = max(3, int(150*nfft/rate)//2*2+1)
    concentration = np.max(uniform_filter1d(power[:, band], width, axis=1)*width, axis=1)/energy
    fraction = energy/(power.sum(axis=1)+1e-30)
    power[:, ~band] = 0
    corr = np.fft.irfft(power, n=nfft)
    harmonic = np.max(corr[:, max(1, rate//400):rate//75+1], axis=1)/(corr[:, 0]+1e-30)
    return np.column_stack((rms, band_rms, peak, harmonic, concentration, fraction)), size


def excerpt_speech_seconds(path, row, start, duration):
    import soundfile as sf
    from speech_normalization import Settings, db, speech_mask
    sr = row['sample_rate']
    offset = round((start-row['start_seconds'])*sr)
    with sf.SoundFile(path) as stream:
        stream.seek(max(0, offset))
        x = stream.read(round(duration*sr), dtype='float32')
    if len(x) < sr:
        return 0.
    features, size = speech_features(x, sr)
    floor = float(np.percentile(db(features[:, 1]), 20))
    mask, _ = speech_mask(features, floor, Settings())
    return float(mask.sum()*size/sr)


def coverage_band(fraction):
    return 'low' if fraction < .2 else 'mid' if fraction < .6 else 'high'


def quality_band(events):
    if not events:
        return 'unaudited'
    return 'audited_improved' if float(np.median([e['ratio'] for e in events])) < .5 else 'audited_marginal'


@analysis.bundle_writer
def qa_batch(analysis_dir, audio_root, *, per_stratum=2, diagnostic=6, excerpt_seconds=64., min_speech_seconds=5.,
             speech=excerpt_speech_seconds):
    """Freeze one stratified random listening selection before any listening."""
    from shared_render import prepared_source
    bundle = analysis.open_analysis(analysis_dir)
    path = bundle['dir']/'qa'/'selection.json'
    if path.is_file():
        return read_frozen(path, bundle)
    analysis.check_code(bundle['config'])
    if per_stratum < 1 or diagnostic < 0 or not math.isfinite(excerpt_seconds) or excerpt_seconds <= 0:
        raise ValueError('QA needs positive excerpts per stratum and duration, and a nonnegative diagnostic count')
    summary = verify_qualification(bundle)
    seed = bundle['config']['identity']['seed']
    held_out = [e for e in analysis.event_rows(bundle) if e['partition'] == 'held_out']
    pool, flagged = {}, []
    inventories = {}
    for tape in summary['tapes']:
        if tape.get('status') != 'qualified':
            continue
        decisions = read_json(bundle['dir']/'qualification'/tape['tape']/'decisions.json')
        inventories[tape['tape']] = decisions['inventory']
        band = coverage_band(decisions['qualified_fraction'])
        accepted = {w['window']: w['valid'] for w in decisions['windows'] if w['decision'] == 'apply'}
        events = []
        for event in held_out:
            if event['tape'] == tape['tape']:
                measured = applied_measurement(event, accepted)
                if measured is not None:
                    events.append(dict(start=event['start'], ratio=measured['ratio']))
        for s in decisions['segments']:
            if s['decision'] != 'apply':
                continue
            length = min(excerpt_seconds, s['end_seconds']-s['start_seconds'])
            for k in range(max(1, int((s['end_seconds']-s['start_seconds'])//excerpt_seconds))):
                a = s['start_seconds']+k*excerpt_seconds
                quality = quality_band(inside(events, [[a, a+length]]))
                pool.setdefault((band, quality), []).append(dict(tape=tape['tape'], start_seconds=round(a, 3),
                                                                 duration_seconds=length,
                                                                 decision='apply', coverage_band=band,
                                                                 quality_band=quality))
        for d in decisions['windows']:
            if d['flags'] and d['eligible']:
                start = max(0., min(d['valid'][0], decisions['duration_seconds']-excerpt_seconds))
                flagged.append(dict(tape=tape['tape'], start_seconds=round(start, 3), decision=d['decision'],
                                    duration_seconds=min(excerpt_seconds, decisions['duration_seconds']),
                                    flags=d['flags'], window=d['window'], coverage_band=band, quality_band='flagged'))
    order = lambda item, kind: rules.unit_hash(seed, 'qa', kind, item['tape'], item['start_seconds'])
    selections = []
    for stratum in sorted(pool):
        candidates = sorted(pool[stratum], key=lambda item: order(item, 'main'))
        chosen, tapes = [], set()
        for item in candidates:  # Prefer distinct tapes within each stratum.
            if item['tape'] not in tapes:
                chosen.append(item)
                tapes.add(item['tape'])
            if len(chosen) == per_stratum:
                break
        for item in candidates:
            if len(chosen) == per_stratum:
                break
            if item not in chosen:
                chosen.append(item)
        selections += [dict(item, stratum='/'.join(stratum), diagnostic=False) for item in chosen]
    selections += [dict(item, stratum='diagnostic/flagged', diagnostic=True)
                   for item in sorted(flagged, key=lambda item: order(item, 'diagnostic'))[:diagnostic]]
    for index, item in enumerate(selections):
        length = item['duration_seconds']
        inv = inventories[item['tape']]
        rows = sorted((r for r in inv['channels'] if r.get('role') != 'timing_only'),
                      key=lambda r: rules.unit_hash(seed, 'qa-channel', item['tape'], item['start_seconds'], r['channel']))
        checked, choice = [], None
        for row in rows:
            if row['start_seconds'] > item['start_seconds'] or row['end_seconds'] < item['start_seconds']+length:
                continue
            try:
                source, _ = prepared_source(row, Path(audio_root)/item['tape']/f'CH{row["channel"]:02d}')
            except (OSError, ValueError, KeyError) as error:
                checked.append(dict(channel=row['channel'], error=str(error)))
                continue
            seconds = round(speech(source, row, item['start_seconds'], length), 2)
            checked.append(dict(channel=row['channel'], speech_seconds=seconds))
            if choice is None:
                choice = dict(channel=row['channel'], speech_seconds=seconds)
            if seconds >= min_speech_seconds:
                choice = dict(channel=row['channel'], speech_seconds=seconds)
                break
        item.update(index=index, channels_checked=checked,
                    channel=None if choice is None else choice['channel'],
                    speech_seconds=None if choice is None else choice['speech_seconds'],
                    speech_free=choice is not None and choice['speech_seconds'] < min_speech_seconds,
                    audio_unavailable=choice is None)
    record = dict(schema=SCHEMA, kind='qa_selection', analysis_id=bundle['config']['analysis_id'], seed=seed,
                  qualification_sha256=summary['record_sha256'],
                  policy_sha256=bundle['config']['identity']['policy_sha256'], frozen=time.strftime('%Y-%m-%dT%H:%M:%S%z'),
                  audio_root=str(audio_root), excerpt_seconds=excerpt_seconds, per_stratum=per_stratum,
                  diagnostic_limit=diagnostic, min_speech_seconds=min_speech_seconds,
                  strata={'/'.join(k): len(v) for k, v in sorted(pool.items())}, flagged_candidates=len(flagged),
                  counts=dict(selected=len(selections), speech_bearing=sum(not s['speech_free'] and not s['audio_unavailable']
                                                                           for s in selections),
                              speech_free=sum(s['speech_free'] for s in selections),
                              audio_unavailable=sum(s['audio_unavailable'] for s in selections),
                              diagnostic=sum(s['diagnostic'] for s in selections)),
                  selections=selections,
                  interpretation=('Frozen before listening: stratified random excerpts by tape coverage and held-out '
                                  'audit band, plus a separate diagnostic stratum of flagged windows. Report failures '
                                  'as well as improvements; speech-free selections are counted separately.'))
    path.parent.mkdir(exist_ok=True)
    return save_frozen(path, record)


def qa_render(analysis_dir, planning_root, audio_root, output, progress=print, *, skip_crosstalk=False):
    """Matched A/B WAVs for the frozen selection through each tape's qualified plan."""
    bundle = analysis.open_analysis(analysis_dir)
    path = bundle['dir']/'qa'/'selection.json'
    if not path.is_file():
        raise analysis.AnalysisError('Freeze the QA selection first')
    selection = read_frozen(path, bundle)
    summary = read_frozen(bundle['dir']/'qualification'/'summary.json', bundle)
    if summary['record_sha256'] != selection['qualification_sha256']:
        raise analysis.AnalysisError('QA selection belongs to different qualification decisions')
    output = Path(output).resolve()
    if output.exists():
        raise ValueError('QA render destination must be new; earlier listening files are immutable')
    output.parent.mkdir(exist_ok=True, parents=True)
    with tempfile.TemporaryDirectory(prefix='.qa-', dir=output.parent) as staging:
        batch = Path(staging)/'batch'
        batch.mkdir()
        renders = _render_selection(selection, summary['files'], planning_root, audio_root, batch, progress, skip_crosstalk)
        atomic_json(batch/'manifest.json', dict(selection_sha256=sha256(path), analysis_id=selection['analysis_id'],
                                                planning_root=str(planning_root), audio_root=str(audio_root), renders=renders))
        batch.rename(output)
    return renders


def _render_selection(selection, files, planning_root, audio_root, output, progress, skip_crosstalk):
    from shared_render import render_shared_channel
    from shared_tape import load_tape_plan
    renders = []
    for item in selection['selections']:
        if item['channel'] is None:
            renders.append(dict(index=item['index'], status='audio_unavailable'))
            continue
        plan_path = Path(planning_root)/item['tape']/'plan.json'
        source = Path(audio_root)/item['tape']/f'CH{item["channel"]:02d}'
        if (source/'crosstalk.json').is_file() == skip_crosstalk:
            raise ValueError(f'Expected {"v2 denoised" if skip_crosstalk else "stage-04 crosstalk"} input at {source}')
        _, plan, data = load_tape_plan(plan_path)
        del data
        provenance = plan.get('census') or {}
        if (plan['inventory']['tape'] != item['tape'] or provenance.get('kind') != 'qualified'
                or provenance.get('analysis_id') != selection['analysis_id']
                or provenance.get('decisions_sha256') != files[f'{item["tape"]}/decisions.json']):
            raise ValueError(f'{plan_path} was not compiled from this qualification analysis')
        folder = output/f'{item["index"]:02d}_{item["tape"]}_CH{item["channel"]:02d}'
        result = render_shared_channel(plan_path, item['channel'], source,
                                       folder, start=item['start_seconds'], duration=item['duration_seconds'],
                                       comparison=True, progress=progress)
        renders.append(dict(index=item['index'], folder=folder.name, map_sha256=result['map_sha256'],
                            files={k: v['sha256'] for k, v in result['files'].items()}))
    return renders
