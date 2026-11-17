"""Pass 2: inventory and assess an immutable detection census over the archive.

Reads ``deflutter_detection_census/<run-id>/`` and writes a separate,
versioned bundle ``deflutter_analysis/<run-id>/<analysis-id>/``. The analysis
ID binds this code, its settings, the qualification policy, the sample seed and
the input manifest; changing any of them makes a new bundle and reuses the
census unchanged. Every scheduled tape is accounted for: missing, stale,
failed or excluded inputs are reported apart from analysed intervals that have
no usable correction. Coverage is always a union of tape time per tape, never
channel-seconds or summed overlapping windows. Repeated measurements of one
check beep (overlapping windows, duplicate feeds, simultaneous channels) are
collapsed into one physical event before any robustness statistic.
"""
from collections import Counter, defaultdict
from functools import wraps
import json
import math
from pathlib import Path
import time

import numpy as np

import flutter_census as census
import flutter_detection as detection
import qualification_policy as policy_rules
from flutter_plan_store import planning_lock

HERE = Path(__file__).resolve().parent
DEFAULT_ANALYSIS_ROOT = detection.DATA/'deflutter_analysis'
SCHEMA = 1
METHODOLOGY = 'tape-qualification-v1'
IMPLEMENTATION = ('qualification_policy.py', 'flutter_analysis.py', 'flutter_followup.py', 'flutter_qualification.py')
DEFAULT_SEED = 20261117
SETTINGS = dict(
    events=dict(simultaneous_seconds=.05, delayed_copy_seconds=[1.2, 1.45]),
    overlap=dict(min_seconds=5.),
)
OUTPUTS = ('input_manifest.json', 'archive_summary.json', 'tapes.json', 'windows.jsonl', 'events.jsonl',
           'followup_queue.json')


class AnalysisError(ValueError):
    """Analysis inputs or saved artifacts do not match; never silently repaired."""


sha256, digest, read_json, atomic_json = detection.sha256, detection.digest, detection.read_json, detection.atomic_json


def implementation():
    return {name: sha256(HERE/name) for name in IMPLEMENTATION}


def bundle_writer(function):
    """Serialize publications for an analysis across independent CLI processes."""
    @wraps(function)
    def write(analysis_dir, *args, **kwargs):
        with planning_lock(analysis_dir):
            return function(analysis_dir, *args, **kwargs)
    return write


def write_jsonl(path, rows):
    path = Path(path)
    pending = path.with_name(path.name+'.partial')
    with pending.open('w', encoding='utf-8') as stream:
        for row in rows:
            stream.write(json.dumps(row, sort_keys=True, allow_nan=False, default=detection._json_default)+'\n')
    pending.replace(path)


def read_jsonl(path):
    with Path(path).open(encoding='utf-8') as stream:
        return [json.loads(line) for line in stream if line.strip()]


# ---------------------------------------------------------------- intervals

def union(intervals):
    out = []
    for a, b in sorted((float(a), float(b)) for a, b in intervals if b > a):
        if out and a <= out[-1][1]+1e-9:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return out


def intersect(first, second):
    out = []
    second = union(second)
    for a, b in union(first):
        for c, d in second:
            lo, hi = max(a, c), min(b, d)
            if hi > lo:
                out.append([lo, hi])
    return union(out)


def subtract(first, second):
    out = []
    second = union(second)
    for a, b in union(first):
        cursor = a
        for c, d in second:
            if d <= cursor or c >= b:
                continue
            if c > cursor:
                out.append([cursor, c])
            cursor = max(cursor, d)
            if cursor >= b:
                break
        if cursor < b:
            out.append([cursor, b])
    return union(out)


def total(intervals):
    return float(sum(b-a for a, b in union(intervals)))


def clip(intervals, lo, hi):
    return intersect(intervals, [[lo, hi]])


def window_id(window):
    return f'{window["block"]}@{window["start_seconds"]:.3f}'


def valid(window):
    return [window['valid_start_seconds'], window['valid_end_seconds']]


def percentiles(values, q=(5, 25, 50, 75, 95)):
    values = [v for v in values if v is not None and math.isfinite(v)]
    if not values:
        return None
    return dict(n=len(values), **{f'p{k}': round(float(v), 4) for k, v in zip(q, np.percentile(values, q))})


# ---------------------------------------------------------------- inputs and identity

def scheduled_tape(census_dir, tape, originals, check_sources, failed):
    folder = census_dir/tape
    if not (folder/'inventory.json').exists():
        return dict(status='failed' if failed else 'incomplete', reason='Scheduled tape has no census inventory')
    record = read_json(folder/'inventory.json')
    audit = record['duplicate_audit']
    blocks = len(record['blocks'])
    published = sum((folder/'blocks'/b['block']).is_dir() for b in record['blocks'])
    row = dict(blocks=blocks, published_blocks=published, duration_seconds=record['inventory']['duration_seconds'],
               inventory_sha256=record['inventory_sha256'])
    if audit['distinct'] == 1 and audit['channels'] > 1:
        row.update(status='excluded', reason=audit['flag'])
    elif (folder/'evidence.json').is_file():
        bundle = detection.load_census(census_dir, tape)
        if check_sources:
            from flutter_planning import inventory
            if not (originals/tape).is_dir():
                raise detection.IdentityError(f'Missing originals: {originals/tape}')
            if detection.source_identity(inventory(originals/tape)) != bundle['evidence']['source_identity']:
                raise detection.IdentityError('originals or offsets changed since detection')
        row.update(status='complete', evidence_sha256=bundle['evidence_sha256'],
                   stream_sha256=bundle['evidence']['stream']['sha256'])
    elif failed:
        row.update(status='failed', reason='census session recorded a failure; see cluster/logs/')
    else:
        row.update(status='incomplete', reason=f'{published}/{blocks} blocks published')
    return row


def input_manifest(census_dir, originals=None, exclude=(), check_sources=True):
    """Account for every canonical tape and every scheduled census tape."""
    census_dir = Path(census_dir).resolve()
    config = detection.load_run(census_dir)
    originals = Path(originals or config['identity']['originals'])
    if check_sources and not originals.is_dir():
        raise AnalysisError(f'Originals root unavailable: {originals}. For an offline assessment use --no-source-check.')
    found = {p.name for p in originals.glob('A15_T*') if p.is_dir()} if originals.is_dir() else set()
    scheduled = {p.name for p in census_dir.glob('A15_T*') if p.is_dir()}
    status_path = census_dir/'cluster'/'status.json'
    failures = read_json(status_path).get('failures', []) if status_path.is_file() else []
    failed = {str(f.get('key', '')).split('/')[0] for f in failures if f.get('key')}
    rows = []
    for tape in sorted(found | scheduled | failed):
        row = dict(tape=tape)
        if tape in exclude:
            row.update(status='excluded', reason='excluded by the analysis command')
        elif tape not in scheduled and tape not in failed:
            row.update(status='not_scheduled', reason='no census inventory for this tape')
        else:
            try:
                row.update(scheduled_tape(census_dir, tape, originals, check_sources, tape in failed))
            except (OSError, KeyError, TypeError, ValueError, RuntimeError) as error:
                row.update(status='stale', reason=str(error))
        rows.append(row)
    return dict(census_run_id=config['run_id'], census_dir=str(census_dir),
                census_config_sha256=sha256(census_dir/'config.json'), originals=str(originals),
                excluded=sorted(exclude), tapes=rows)


def analysis_identity(manifest, policy, seed, settings=SETTINGS):
    value = dict(schema=SCHEMA, methodology=METHODOLOGY, census_run_id=manifest['census_run_id'],
                 census_config_sha256=manifest['census_config_sha256'],
                 input_manifest_sha256=digest(manifest), implementation=implementation(),
                 settings=policy_rules.normalized(settings), policy=policy_rules.normalized(policy),
                 policy_sha256=policy_rules.policy_digest(policy), seed=int(seed))
    return 'analysis-'+digest(value)[:16], value


def open_analysis(analysis_dir):
    """Validate an existing bundle's identity and its complete outputs."""
    analysis_dir = Path(analysis_dir).resolve()
    config = read_json(analysis_dir/'config.json')
    if (config.get('kind') != 'flutter_analysis' or config.get('analysis_id') != analysis_dir.name
            or 'analysis-'+digest(config['identity'])[:16] != config['analysis_id']
            or analysis_dir.parent.name != config['identity']['census_run_id']):
        raise AnalysisError(f'Invalid or relocated analysis bundle: {analysis_dir}')
    path = analysis_dir/'assessment.json'
    if not path.is_file():
        raise AnalysisError(f'Pass-2 assessment is incomplete: {analysis_dir}')
    assessment = read_json(path)
    if assessment.get('complete') is not True or assessment.get('analysis_id') != config['analysis_id']:
        raise AnalysisError(f'Pass-2 assessment is incomplete: {analysis_dir}')
    if set(assessment['files']) != set(OUTPUTS):
        raise AnalysisError(f'Assessment file manifest changed: {analysis_dir}')
    for name, expected in assessment['files'].items():
        if sha256(analysis_dir/name) != expected:
            raise AnalysisError(f'Analysis artifact changed: {name}')
    if digest(read_json(analysis_dir/'input_manifest.json')) != config['identity']['input_manifest_sha256']:
        raise AnalysisError('Analysis input manifest changed')
    return dict(dir=analysis_dir, config=config, assessment=assessment,
                census_dir=Path(read_json(analysis_dir/'input_manifest.json')['census_dir']))


def check_code(config):
    if implementation() != config['identity']['implementation']:
        raise AnalysisError('Analysis implementation changed since this bundle was created; '
                            'its outputs stay valid, new work needs a new analysis ID')


# ---------------------------------------------------------------- events

def channel_groups(block_groups, known):
    parent = {}

    def root(c):
        parent.setdefault(c, c)
        while parent[c] != c:
            c = parent[c]
        return c
    for group in [*block_groups, *known]:
        for c in group[1:]:
            a, b = root(group[0]), root(c)
            if a != b:
                parent[max(a, b)] = min(a, b)
    return root


def event_measurement(items):
    base = np.maximum([m['identity'] for m in items], 1e-9)
    return dict(channels=sorted({m['channel'] for m in items}),
                identity_hz=round(float(np.median([m['identity'] for m in items])), 3),
                curve_hz=round(float(np.median([m['curve'] for m in items])), 3),
                ratio=round(float(np.median([m['curve'] for m in items]/base)), 4),
                wrong_sign_ratio=round(float(np.median([m['wrong_sign'] for m in items]/base)), 4),
                shift_ratio=round(float(np.median([m['shift'] for m in items]/base)), 4))


def collapse_events(tape, windows, block_groups, known, seed, policy, settings=SETTINGS):
    """Group beep measurements into physical events; keep uncertain links explicit."""
    rows = []
    agreeing = {window_id(w): set(w.get('agreeing_channels', [])) for w in windows}
    for w in windows:
        for e in (w.get('beep_audit') or {}).get('events', []):
            rows.append(dict(window=window_id(w), block=w['block'], channel=int(e['channel']),
                             start=float(e['start']), centre=sum(valid(w))/2,
                             **{k: float(e[k]) for k in ('identity', 'curve', 'wrong_sign', 'shift')}))
    rows.sort(key=lambda r: (r['start'], r['channel'], r['window']))
    clusters = []
    for row in rows:
        if clusters and row['start']-clusters[-1][0]['start'] <= settings['events']['simultaneous_seconds']:
            clusters[-1].append(row)
        else:
            clusters.append([row])
    events = []
    for members in clusters:
        first = members[0]['start']
        roots = {m['channel']: channel_groups(block_groups.get(m['block'], []), known)(m['channel']) for m in members}
        channels = sorted(roots)
        groups = sorted(set(roots.values()))
        association = ('single_channel' if len(channels) == 1 else
                       'duplicate_feed' if len(groups) == 1 else 'simultaneous')
        event_id = f'{tape}@{first:.3f}'
        measured = {}
        for m in members:
            measured.setdefault(m['window'], []).append(m)
        windows_measured = {}
        for wid, items in measured.items():
            windows_measured[wid] = event_measurement(items)
            outside = [m for m in items if m['channel'] not in agreeing[wid]]
            if outside:
                windows_measured[wid]['transfer'] = event_measurement(outside)
        primary = min(measured, key=lambda wid: (abs(measured[wid][0]['centre']-first), wid))
        events.append(dict(tape=tape, id=event_id, start=round(first, 3), channels=channels, groups=groups,
                           association=association,
                           partition=policy_rules.event_partition(seed, tape, event_id, policy),
                           measurements=len(members), primary_window=primary, windows=windows_measured,
                           **{k: windows_measured[primary][k] for k in ('ratio', 'wrong_sign_ratio', 'shift_ratio',
                                                                       'identity_hz', 'curve_hz')}))
    uncertain = []
    low, high = settings['events']['delayed_copy_seconds']
    for i, a in enumerate(events):
        for j in range(i+1, len(events)):
            b = events[j]
            delay = b['start']-a['start']
            if delay > high:
                break
            if delay >= low and not set(a['channels']) & set(b['channels']):
                uncertain.append(dict(first=a['id'], second=b['id'], delay_seconds=round(delay, 3),
                                      note='Possible delayed crosstalk copy; not merged'))
    return events, uncertain


def physical_summary(events, wid, partition=None):
    """One value per physical event measured in this window."""
    rows = [e['windows'][wid] for e in events if wid in e['windows']
            and (partition is None or e['partition'] == partition)]
    if not rows:
        return dict(n=0, median_ratio=None, median_wrong_sign_ratio=None, median_shift_ratio=None, worse=0)
    return dict(n=len(rows), median_ratio=round(float(np.median([r['ratio'] for r in rows])), 4),
                median_wrong_sign_ratio=round(float(np.median([r['wrong_sign_ratio'] for r in rows])), 4),
                median_shift_ratio=round(float(np.median([r['shift_ratio'] for r in rows])), 4),
                worse=int(sum(r['ratio'] > 1 for r in rows)))


def event_statistics(events, partition=None):
    rows = [e for e in events if partition is None or e['partition'] == partition]
    if not rows:
        return dict(events=0)
    ratio = [e['ratio'] for e in rows]
    return dict(events=len(rows), median_ratio=round(float(np.median(ratio)), 4),
                worse_fraction=round(float(np.mean([r > 1 for r in ratio])), 4),
                ratio_distribution=percentiles(ratio),
                median_hz=dict(identity=round(float(np.median([e['identity_hz'] for e in rows])), 3),
                               curve=round(float(np.median([e['curve_hz'] for e in rows])), 3)),
                controls=dict(wrong_sign_ratio=round(float(np.median([e['wrong_sign_ratio'] for e in rows])), 4),
                              shift_ratio=round(float(np.median([e['shift_ratio'] for e in rows])), 4)),
                associations=dict(Counter(e['association'] for e in rows)))


# ---------------------------------------------------------------- tape assessment

class Curves:
    """Lazy census window curves from verified block files."""
    def __init__(self, folder):
        self.folder, self.cache = Path(folder), {}

    def __call__(self, window):
        block = window['block']
        if block not in self.cache:
            self.cache[block] = np.load(self.folder/'blocks'/block/'curves.npy', allow_pickle=False)
        return self.cache[block][window['curve_index']].astype(np.float64)


def overlap_agreement(windows, curves, settings=SETTINGS):
    """Correlation of overlapping eligible window curves on the shared grid."""
    eligible = sorted((w for w in windows if w['eligible']), key=lambda w: w['valid_start_seconds'])
    pairs = []
    for i, a in enumerate(eligible):
        for j in range(i+1, len(eligible)):
            b = eligible[j]
            lo, hi = b['valid_start_seconds'], min(a['valid_end_seconds'], b['valid_end_seconds'])
            if lo >= a['valid_end_seconds']:
                break
            if hi-lo < settings['overlap']['min_seconds']:
                continue
            ia = int(round((lo-a['valid_start_seconds'])*census.RATE))
            ib = int(round((lo-b['valid_start_seconds'])*census.RATE))
            n = int(round((hi-lo)*census.RATE))
            x, y = curves(a)[ia:ia+n], curves(b)[ib:ib+n]
            if len(x) != n or len(y) != n:
                continue
            pairs.append(dict(first=window_id(a), second=window_id(b), seconds=round(hi-lo, 3),
                              r=round(census.correlation(x, y), 4),
                              rms_difference_percent=round(100*float(np.sqrt(np.mean((x-y)**2))), 4)))
    return pairs


def assess_tape(bundle, policy, seed, settings=SETTINGS):
    evidence = bundle['evidence']
    tape = evidence['tape']
    gates = detection.settings_of(bundle['config'])['gates']
    duration = float(evidence['inventory']['duration_seconds'])
    windows = sorted(evidence['windows'], key=lambda w: (w['valid_start_seconds'], w['block']))
    curves = Curves(bundle['run_dir']/tape)
    block_groups = {b['block']: b['duplicate_groups'] for b in evidence['blocks']}
    known = evidence['duplicate_audit']['duplicate_groups']
    pairs = overlap_agreement(windows, curves, settings)
    worst = {}
    for p in pairs:
        for k in ('first', 'second'):
            worst[p[k]] = min(worst.get(p[k], 1.), p['r'])
    events, uncertain = collapse_events(tape, windows, block_groups, known, seed, policy, settings)
    events_by_window = defaultdict(list)
    for event in events:
        for wid in event['windows']:
            events_by_window[wid].append(event)
    span = [[0., duration]]
    analysed = clip([valid(w) for w in windows], 0., duration)
    supported = clip([valid(w) for w in windows if w['eligible']], 0., duration)
    unsupported = subtract(analysed, supported)
    unmeasured = subtract(span, analysed)
    near = policy['boundary_seconds']
    not_supported = subtract(span, supported)
    rows, queue = [], []
    for w in windows:
        wid = window_id(w)
        around = [[w['valid_start_seconds']-near, w['valid_end_seconds']+near]]
        boundary = (total(intersect(around, not_supported)) > 0 if w['eligible']
                    else total(intersect(around, supported)) > 0)
        physical = physical_summary(events_by_window[wid], wid, 'decision')
        flags = policy_rules.window_flags(w, physical, worst.get(wid), boundary, gates, policy)
        state = policy_rules.classify(w, flags)
        concentration = w.get('concentration_audit') or []
        row = dict(tape=tape, id=wid, block=w['block'], start_seconds=w['start_seconds'],
                   end_seconds=w['end_seconds'], valid=valid(w), eligible=bool(w['eligible']),
                   reasons=w.get('reasons', []), curve_index=w.get('curve_index'), random_audit=w.get('random_audit'),
                   agreeing_groups=len(w.get('agreeing_groups', [])), agreeing_channels=w.get('agreeing_channels', []),
                   families=w.get('families', []), hum_cluster=w.get('hum_cluster'),
                   consensus_fm_percent=w.get('consensus_fm_percent'), cluster_fm_percent=w.get('cluster_fm_percent'),
                   clipped_fraction=w.get('clipped_fraction'), sign_conflict=bool(w.get('sign_conflict')),
                   overlap_min_r=worst.get(wid), physical_decision=physical,
                   physical_held_out=physical_summary(events_by_window[wid], wid, 'held_out'),
                   concentration=dict(lines=len(concentration),
                                      concentrates=sum(bool(c['concentrates']) for c in concentration),
                                      reversed=sum(c['wrong_sign'] > c['curve'] for c in concentration)),
                   flags=flags, **{'class': state})
        rows.append(row)
        if flags:
            queue.append(dict(tape=tape, window=wid, kind='flag', reasons=flags, eligible=row['eligible'],
                              start_seconds=w['start_seconds'], end_seconds=w['end_seconds']))
    sample = policy['sample']
    for kind, fraction, chosen in (('candidate', sample['candidate_fraction'], 'candidate'),
                                   ('unsupported', sample['unsupported_fraction'], 'unsupported')):
        picks = [r for r in rows if r['class'] == chosen and not r['flags']
                 and policy_rules.sampled(seed, tape, r['id'], kind, fraction)]
        picks.sort(key=lambda r: policy_rules.unit_hash(seed, 'order', kind, tape, r['id']))
        for r in picks[:sample['max_per_tape']]:
            queue.append(dict(tape=tape, window=r['id'], kind='sample', reasons=[f'random_{kind}'],
                              eligible=r['eligible'], start_seconds=r['start_seconds'], end_seconds=r['end_seconds']))
    queue.sort(key=lambda q: (q['start_seconds'], q['window']))
    eligible = [w for w in windows if w['eligible']]
    audited = [e for e in events]
    channel_use = Counter(ch for w in eligible for ch in w.get('agreeing_channels', []))
    concentration_lines = [c for w in windows for c in (w.get('concentration_audit') or [])]
    covered_by_events = union([[e['start']-1, e['start']+1] for e in events])
    tape_row = dict(
        tape=tape, status='complete', duration_seconds=duration,
        partition=policy_rules.tape_partition(seed, tape, policy),
        intervals=dict(analysed_seconds=total(analysed), candidate_supported_seconds=total(supported),
                       unsupported_seconds=total(unsupported), unmeasured_seconds=total(unmeasured),
                       qualified_seconds=None,
                       analysed_fraction=total(analysed)/duration, supported_fraction=total(supported)/duration,
                       basis='Union of window valid regions on this tape; never channel-seconds'),
        windows=dict(total=len(windows), eligible=len(eligible), **Counter(r['class'] for r in rows)),
        gate_failures=dict(Counter(reason for w in windows for reason in w.get('reasons', []))),
        sign_conflict_windows=sum(bool(w.get('sign_conflict')) for w in windows),
        clipped_windows=sum((w.get('clipped_fraction') or 0) > 0 for w in eligible),
        hum_only_windows=sum(bool(w.get('hum_cluster')) for w in eligible),
        agreeing_groups=percentiles([len(w.get('agreeing_groups', [])) for w in eligible]),
        families=dict(Counter(len(w.get('families', [])) for w in eligible)),
        consensus_fm_percent=percentiles([w.get('consensus_fm_percent') for w in eligible]),
        channel_participation={str(k): round(v/max(len(eligible), 1), 4) for k, v in sorted(channel_use.items())},
        overlap=dict(pairs=len(pairs), r=percentiles([p['r'] for p in pairs]),
                     below_threshold=sum(p['r'] < policy['overlap']['min_r'] for p in pairs)),
        beep_audit=dict(measurements=sum(e['measurements'] for e in audited),
                        all=event_statistics(events), decision=event_statistics(events, 'decision'),
                        held_out=event_statistics(events, 'held_out'), uncertain_associations=len(uncertain),
                        audited_supported_seconds=total(intersect(supported, covered_by_events)),
                        unaudited_supported_seconds=total(subtract(supported, covered_by_events)),
                        note='Statistics count physical events; channel copies and overlapping windows count once'),
        concentration_audit=dict(lines=len(concentration_lines),
                                 concentrates=sum(bool(c['concentrates']) for c in concentration_lines),
                                 reversed=sum(c['wrong_sign'] > c['curve'] for c in concentration_lines)),
        flags=dict(Counter(f for r in rows for f in r['flags'])),
        queue=dict(Counter(q['kind'] for q in queue)),
        supported_intervals=supported, unsupported_intervals=unsupported, unmeasured_intervals=unmeasured)
    return tape_row, rows, events, uncertain, queue


def archive_summary(manifest, tapes, rows, events):
    complete = [t for t in tapes if t['status'] == 'complete']
    durations = sum(t['duration_seconds'] for t in complete)
    sums = {k: sum(t['intervals'][k] for t in complete)
            for k in ('analysed_seconds', 'candidate_supported_seconds', 'unsupported_seconds', 'unmeasured_seconds')}
    eligible = [r for r in rows if r['eligible']]
    return dict(
        census_run_id=manifest['census_run_id'],
        tapes=dict(Counter(t['status'] for t in manifest['tapes'])),
        not_analysed=[dict(tape=t['tape'], status=t['status'], reason=t.get('reason'))
                      for t in manifest['tapes'] if t['status'] != 'complete'],
        tape_time=dict(complete_tape_seconds=durations, **sums,
                       qualified_seconds=None, qualified_note='Unknown until qualification has run',
                       basis='Sum over tapes of each tape\'s own tape-time union'),
        coverage_distribution=percentiles([t['intervals']['supported_fraction'] for t in complete]),
        windows=dict(total=len(rows), eligible=len(eligible), **Counter(r['class'] for r in rows)),
        gate_failures=dict(sum((Counter(t['gate_failures']) for t in complete), Counter())),
        flags=dict(Counter(f for r in rows for f in r['flags'])),
        sign_conflict_windows=sum(r['sign_conflict'] for r in rows),
        clipped_windows=sum((r['clipped_fraction'] or 0) > 0 for r in eligible),
        agreeing_groups=percentiles([r['agreeing_groups'] for r in eligible]),
        families=dict(Counter(len(r['families']) for r in eligible)),
        overlap_min_r=percentiles([r['overlap_min_r'] for r in eligible]),
        beep_audit=dict(all=event_statistics(events), decision=event_statistics(events, 'decision'),
                        held_out=event_statistics(events, 'held_out'),
                        per_tape_median_ratio=percentiles([t['beep_audit']['all'].get('median_ratio') for t in complete]),
                        tapes_without_events=sum(t['beep_audit']['all']['events'] == 0 for t in complete)),
        concentration_audit=dict(lines=sum(t['concentration_audit']['lines'] for t in complete),
                                 concentrates=sum(t['concentration_audit']['concentrates'] for t in complete),
                                 reversed=sum(t['concentration_audit']['reversed'] for t in complete)),
        outliers=dict(adverse_windows=sum('adverse_audit' in r['flags'] for r in rows),
                      reversed_windows=sum('reversed_audit' in r['flags'] for r in rows),
                      inconsistent_overlap_windows=sum('overlap_inconsistent' in r['flags'] for r in rows)),
        interpretation='Detection evidence and audits only; not render permission or listening approval')


# ---------------------------------------------------------------- pass 2 driver

def assess(census_dir, *, root=DEFAULT_ANALYSIS_ROOT, policy=None, seed=DEFAULT_SEED, exclude=(),
           originals=None, check_sources=True, progress=print):
    """Create (or verify and reuse) the pass-2 bundle for the current census state."""
    policy = policy or policy_rules.POLICY
    manifest = input_manifest(census_dir, originals, exclude, check_sources)
    analysis_id, identity = analysis_identity(manifest, policy, seed)
    folder = Path(root).resolve()/manifest['census_run_id']/analysis_id
    with planning_lock(folder):
        return _write_assessment(folder, manifest, analysis_id, identity, policy, seed, progress)


def _write_assessment(folder, manifest, analysis_id, identity, policy, seed, progress):
    path = folder/'config.json'
    if path.exists():
        config = read_json(path)
        if config.get('identity') != identity:
            raise AnalysisError(f'{folder} has a different identity')
        if (folder/'assessment.json').is_file():
            return open_analysis(folder)
    else:
        folder.mkdir(parents=True, exist_ok=True)
        config = dict(schema=SCHEMA, kind='flutter_analysis', analysis_id=analysis_id, identity=identity,
                      created=time.strftime('%Y-%m-%dT%H:%M:%S%z'),
                      interpretation='Archive analysis of immutable census evidence; not listening approval')
        atomic_json(path, config)
    atomic_json(folder/'input_manifest.json', manifest)
    tapes, rows, events, queue = [], [], [], []
    for item in manifest['tapes']:
        if item['status'] != 'complete':
            tapes.append(dict(tape=item['tape'], status=item['status'], reason=item.get('reason')))
            continue
        bundle = detection.load_census(manifest['census_dir'], item['tape'])
        if bundle['evidence_sha256'] != item['evidence_sha256']:
            raise AnalysisError(f'{item["tape"]} census evidence changed during analysis')
        tape_row, window_rows, tape_events, tape_uncertain, tape_queue = assess_tape(bundle, policy, seed)
        tape_row.update(evidence_sha256=item['evidence_sha256'], uncertain_associations=tape_uncertain)
        tapes.append(tape_row)
        rows += window_rows
        events += tape_events
        queue += tape_queue
        progress(f'{item["tape"]}: {tape_row["windows"]["eligible"]}/{tape_row["windows"]["total"]} eligible, '
                 f'{len(tape_events)} physical beep events, {len(tape_queue)} queued')
    atomic_json(folder/'tapes.json', tapes)
    write_jsonl(folder/'windows.jsonl', rows)
    write_jsonl(folder/'events.jsonl', events)
    atomic_json(folder/'followup_queue.json', dict(analysis_id=analysis_id, items=queue,
                counts=dict(Counter(q['kind'] for q in queue)),
                interpretation='Analysis states, not render permissions; samples detect failures outside flags'))
    atomic_json(folder/'archive_summary.json', archive_summary(manifest, tapes, rows, events))
    atomic_json(folder/'assessment.json', dict(schema=SCHEMA, kind='flutter_assessment', complete=True,
                analysis_id=analysis_id, files={name: sha256(folder/name) for name in OUTPUTS}))
    return open_analysis(folder)


def tape_rows(analysis):
    return {t['tape']: t for t in read_json(analysis['dir']/'tapes.json')}


def window_rows(analysis, tape=None):
    return [r for r in read_jsonl(analysis['dir']/'windows.jsonl') if tape is None or r['tape'] == tape]


def event_rows(analysis, tape=None):
    return [e for e in read_jsonl(analysis['dir']/'events.jsonl') if tape is None or e['tape'] == tape]


def queue_items(analysis):
    return read_json(analysis['dir']/'followup_queue.json')['items']
