"""One immutable tape-motion map, compiled from saved evidence on all tracks.

Historical plans are evidence import formats only. Output channels never take
part in selection, joining, integration, or endpoint anchoring.
"""
from collections import defaultdict
import json
import math
from pathlib import Path

import numpy as np
from scipy.integrate import cumulative_trapezoid
from scipy.ndimage import distance_transform_edt

from denoise_archive import atomic_json, sha256, bound_path, cleanup_version, denoised_audio_path
from flutter_plan_store import checked_artifact, checked_stream, load_saved_plan

METHODOLOGY = 'shared-tape-motion-v1'
RATE = 200
DEFAULT_PLANNING_ROOT = Path('D:/Apollo_15_30-Track/deflutter_planning_shared')
DEFAULT_EVIDENCE_ROOT = Path('D:/Apollo_15_30-Track/deflutter_planning_v5_allchannel')
DEFAULT_DEFLUTTERED_ROOT = Path('D:/Apollo_15_30-Track/defluttered_shared')
ALLOWED_OUTCOMES = {'insufficient_evidence', 'partial_improvement_at_measured_events',
                    'full_restoration_at_measured_events', 'restoration_at_measured_events',
                    'supported_improvement'}
REGIONAL_ACTIONS = {'render_and_review', 'group_render_and_review'}


def identity(inventory):
    return {r['channel']: {k: r[k] for k in
            ('path', 'bytes', 'mtime_ns', 'sample_rate', 'frames', 'start_seconds')}
            for r in inventory['channels']}


def bound(path):
    path = Path(path).resolve()
    binding = dict(path=str(path), sha256=sha256(path))
    manifest = path.with_name('denoised.json')
    if manifest.is_file():
        saved = json.loads(manifest.read_text(encoding='utf-8'))
        if path.name in {'denoised.json', 'cleanup.jsonl'} or path == denoised_audio_path(path.parent, saved):
            binding['cleanup_version'] = cleanup_version(saved)
    return binding


def verify_bindings(bindings):
    for item in bindings:
        if sha256(bound_path(item)) != item['sha256']:
            raise ValueError('Stale evidence: '+item['path'])


def index_path(path):
    path = Path(path).resolve()
    return path/'plan.json' if path.is_dir() else path


def continuous_candidates(path, assessment_path):
    """Union eligible saved witnesses; a destination track supplies no veto."""
    path = index_path(path)
    plan = load_saved_plan(path, expand_regions=False)
    plan_hash = sha256(path)
    if plan.get('kind') != 'continuous_reference':
        raise ValueError('Expected saved continuous tone evidence')
    assessment = json.loads(Path(assessment_path).read_text(encoding='utf-8'))
    if assessment.get('plan_sha256') != plan_hash:
        raise ValueError('Saved tone assessment belongs to another plan')
    channels = {r['channel']: r for r in assessment['channels']}
    candidates, rejected = [], []
    for summary in plan['regions']:
        _, raw = checked_artifact(path.parent, summary['detail_file'], summary['detail_sha256'])
        detail = json.loads(raw)
        variants = {r['key']: r for r in detail['variants']}
        available = defaultdict(list)
        for target in detail['targets']:
            checks = channels.get(target['channel'], {}).get('spans', [])
            for span in target.get('reference_spans', []):
                if span['action'] != 'use_reference' or not span.get('selected'):
                    continue
                check = next((c for c in checks if
                    abs(c['start_seconds']-span['start_seconds']) < 1e-6 and
                    abs(c['end_seconds']-span['end_seconds']) < 1e-6), None)
                reason = None
                if check is None:
                    reason = 'Missing saved local assessment'
                elif check['outcome'] not in ALLOWED_OUTCOMES:
                    reason = check['outcome']
                elif any(e.get('status') == 'failed' for e in check.get('event_metrics', [])):
                    reason = 'Failed saved event measurement'
                witness = dict(channel=target['channel'], start_seconds=span['start_seconds'],
                    end_seconds=span['end_seconds'], selected=span['selected'])
                if reason:
                    rejected.append(dict(witness, reason=reason))
                else:
                    available[(span['selected'], span['start_seconds'], span['end_seconds'])].append(
                        dict(channel=target['channel'], outcome=check['outcome']))
        for (key, a, b), witnesses in available.items():
            spec = detail['maps']['streams'][key]
            variant = variants[key]
            candidates.append(dict(id=plan_hash[:16]+'__'+key, start_seconds=a,
                end_seconds=b, priority=1, rank=float(variant.get('rank', 0)),
                reference_channel=variant.get('reference_channel'),
                carrier_hz=variant.get('carrier_hz'), method=variant.get('method'),
                witnesses=witnesses, source_kind='continuous', source_key=key,
                source_plan=str(path), source_plan_sha256=plan_hash,
                curve=dict(format='stream', path=str((path.parent/spec['file']).resolve()),
                    sha256=spec['sha256'], frames=spec['frames'], rate=detail['maps']['rate'],
                    start_seconds=spec['start_seconds'], dtype=spec.get('dtype'))))
    return plan, candidates, rejected, [bound(path), bound(assessment_path)]


def regional_candidates(path):
    """Import completed regional evidence without enabling transfer proposals."""
    path = index_path(path)
    plan = load_saved_plan(path, expand_regions=False)
    plan_hash = sha256(path)
    candidates = []
    for summary in plan['regions']:
        _, raw = checked_artifact(path.parent, summary['detail_file'], summary['detail_sha256'])
        detail = json.loads(raw)
        witnesses = defaultdict(list)
        variants = {v['key']: v for v in detail.get('variants', [])}
        for target in detail['targets']:
            if target['action'] in REGIONAL_ACTIONS and target.get('selected'):
                witnesses[target['selected']].append(dict(channel=target['channel'],
                    outcome=target['action']))
        for key, supporting in witnesses.items():
            variant = variants.get(key, {})
            spec = detail['maps']
            candidates.append(dict(id=plan_hash[:16]+'__'+detail['id']+'__'+key,
                start_seconds=detail['start_seconds'], end_seconds=detail['end_seconds'],
                priority=0, rank=float(variant.get('reference_rank', 0)),
                reference_support=float(variant.get('tracking', {}).get('support', 0)),
                reference_channel=variant.get('reference_channel'), carrier_hz=variant.get('carrier_hz'),
                method=variant.get('method'), witnesses=supporting, source_kind='regional',
                source_key=key, source_plan=str(path), source_plan_sha256=plan_hash,
                curve=dict(format='npz', path=str((path.parent/spec['file']).resolve()),
                    sha256=spec['sha256'], key=key, rate=spec['rate'],
                    frames=round((detail['analysis_end']-detail['analysis_start'])*RATE),
                    start_seconds=detail['analysis_start'])))
    return plan, candidates, bound(path)


def select_intervals(candidates, start, end):
    """Rank only supported intervals. Copied channels do not cast votes."""
    points = {start, end}
    clipped = []
    # Use the compiler's canonical grid so support gaps and selection boundaries
    # refer to the same samples. A dropout may expose another saved reference.
    times = start+np.arange(math.ceil((end-start)*RATE))/RATE
    for c in candidates:
        a, b = max(start, c['start_seconds']), min(end, c['end_seconds'])
        lo, hi = np.searchsorted(times, [a, b])
        if a >= b or hi <= lo:
            continue
        _, support = read_curve(c, times[lo:hi])
        edges = np.flatnonzero(np.diff(np.r_[False, support >= .95, False]))
        for first, stop in edges.reshape(-1, 2):
            low = a if first == 0 else float(times[lo+first])
            high = b if stop == hi-lo else float(times[lo+stop])
            clipped.append(dict(c, start_seconds=low, end_seconds=high))
            points.update((low, high))
    chosen = []
    points = sorted(points)
    for a, b in zip(points, points[1:]):
        eligible = [c for c in clipped if c['start_seconds'] <= a and c['end_seconds'] >= b]
        winner = min(eligible, key=lambda c: (c['priority'], -c.get('reference_support', 0),
            -c['rank'], c['source_key'], c['id'])) if eligible else None
        key = winner['id'] if winner else None
        if chosen and chosen[-1]['selected'] == key:
            chosen[-1]['end_seconds'] = b
        else:
            chosen.append(dict(start_seconds=a, end_seconds=b, selected=key))
    return chosen


def rising(values):
    return .5-.5*np.cos(np.pi*np.clip(values, 0, 1))


def read_curve(candidate, times):
    spec = candidate['curve']
    if spec['rate'] != RATE or spec['frames'] < 2:
        raise ValueError('Unsupported saved tone tracking grid')
    if spec['format'] == 'stream':
        if spec.get('dtype') != '<f8' or Path(spec['path']).stat().st_size != spec['frames']*16:
            raise ValueError('Invalid saved tone stream shape')
        data = np.memmap(spec['path'], dtype='<f8', mode='r', shape=(spec['frames'], 2))
        speed, support = data[:, 0], data[:, 1]
    else:
        with np.load(spec['path'], allow_pickle=False) as archive:
            speed = archive[spec['key']].copy()
            support = archive[spec['key']+'__support'].copy()
        if speed.shape != (spec['frames'],) or support.shape != speed.shape:
            raise ValueError('Invalid saved regional curve shape')
    positions = (times-spec['start_seconds'])*RATE
    # Roundoff at a saved grid point must not turn its support into an
    # out-of-range zero (notably the last sample of an offset regional map).
    nearest = np.rint(positions)
    positions = np.where(abs(positions-nearest) < 1e-7, nearest, positions)
    lo = max(0, math.floor(float(positions[0]))-1)
    hi = min(len(speed), math.ceil(float(positions[-1]))+2)
    if hi <= lo:
        raise ValueError('Selected tone interval has no saved curve')
    e, g = speed[lo:hi], support[lo:hi]
    if (not np.isfinite(e).all() or not np.isfinite(g).all() or np.max(abs(e)) > .05
            or np.any((g < 0) | (g > 1)) or np.any(abs(e[g == 0]) > 1e-12)):
        raise ValueError('Invalid saved speed/support samples')
    grid = np.arange(lo, hi)
    return (np.interp(positions, grid, e, left=0, right=0),
            np.interp(positions, grid, g, left=0, right=0))


def compile_map(path, candidates, selections, start, end):
    """Join supported curves, then integrate and anchor once for the tape."""
    count = math.ceil((end-start)*RATE)+1
    times = start+np.arange(count, dtype=float)/RATE
    times[-1] = end
    speed, support = np.zeros(count), np.zeros(count)
    lookup = {c['id']: c for c in candidates}
    for selection in selections:
        key = selection['selected']
        if key is None:
            continue
        a, b = selection['start_seconds'], selection['end_seconds']
        lo, hi = np.searchsorted(times, [a, b])
        if hi <= lo:
            continue
        local = times[lo:hi]
        e, g = read_curve(lookup[key], local)
        # Fade only at actual selection/support edges, never processing blocks.
        distance = distance_transform_edt(np.r_[False, g >= .95, False])[1:-1]/RATE
        edge = rising(np.minimum(local-a, b-local)/min(1., (b-a)/2))
        gate = g*rising(distance/.5)*edge
        speed[lo:hi], support[lo:hi] = e*gate, gate
    # Endpoint fading and DC removal are global, independent of output channels.
    fade = rising(np.minimum(times-start, end-times)/2)
    speed *= fade
    support *= fade
    area = float(np.trapezoid(support, times))
    dc = float(np.trapezoid(speed, times)/area) if area else 0.
    speed -= dc*support
    if np.max(abs(speed)) > .05:
        raise ValueError('Shared tape duration anchoring exceeds the 5% speed bound')
    delay = cumulative_trapezoid(speed, times, initial=0)
    delay[-1] = 0.
    nominal = times+delay
    if not np.isfinite(nominal).all() or np.any(np.diff(nominal) <= 0):
        raise ValueError('Shared tape map is not finite and strictly monotonic')
    # Store one array; every render loads this artifact, including excerpts.
    # Columns must be contiguous: inverse lookup runs thousands of times during
    # a full-channel render and must not copy a tape-length strided column.
    data = np.lib.format.open_memmap(path, mode='w+', dtype='<f8', shape=(count, 4), fortran_order=True)
    data[:, 0], data[:, 1], data[:, 2], data[:, 3] = times, nominal, speed, support
    data.flush()
    del data
    eligible = sum(s['end_seconds']-s['start_seconds'] for s in selections if s['selected'])
    supported = float(np.trapezoid((support > 1e-12).astype(float), times))
    return dict(rate=RATE, rows=count, columns=['tape_seconds', 'nominal_seconds', 'speed', 'support'],
        start_seconds=start, end_seconds=end, dc_speed_removed=dc,
        maximum_speed_correction=float(np.max(abs(speed))),
        maximum_displacement_seconds=float(np.max(abs(delay))),
        eligible_seconds=eligible, supported_seconds=supported,
        bypass_seconds=end-start-supported, coverage=supported/(end-start),
        finite=True, monotonic=True, duration_anchored=True,
        integration='One tape-level integration and support-weighted DC anchor',
        bypass='Zero local speed correction; constant displacement preserves continuity')


def build_tape_plan(continuous_path, assessment_path, output, *, regional_paths=(), start=None, end=None):
    """Reuse completed evidence; no carrier discovery, fitting, or audio rendering."""
    output = Path(output).resolve()
    protected = [index_path(continuous_path).parent,
        *[index_path(path).parent for path in regional_paths]]
    if any(output == p or output in p.parents or p in output.parents for p in protected):
        raise ValueError('Shared tape output must not overlap saved evidence')
    source, candidates, rejected, bindings = continuous_candidates(continuous_path, assessment_path)
    inv = source['inventory']
    if any(output == Path(r['path']).parent or Path(r['path']).parent in output.parents
           or output in Path(r['path']).parent.parents for r in inv['channels']):
        raise ValueError('Shared tape output must not overlap originals')
    for path in regional_paths:
        regional, extra, binding = regional_candidates(path)
        if regional['inventory']['tape'] != inv['tape'] or identity(regional['inventory']) != identity(inv):
            raise ValueError('Saved evidence disagrees on original identity or canonical offsets')
        candidates.extend(extra)
        bindings.append(binding)
    source_start = source.get('parameters', {}).get('start', source['regions'][0]['start_seconds'])
    source_end = max(r['end_seconds'] for r in source['regions'])
    start = source_start if start is None else start
    end = source_end if end is None else end
    if not np.isfinite([start, end]).all() or not source_start <= start < end <= source_end:
        raise ValueError('Shared scope must lie within completed evidence coverage')
    if output.exists():
        if not (output/'plan.json').is_file():
            raise ValueError('Incomplete shared destination; choose a new directory')
        _, saved, data = load_tape_plan(output)
        del data
        if (saved['sources'] != bindings or saved['implementation_sha256'] != sha256(__file__)
                or saved['start_seconds'] != start or saved['end_seconds'] != end):
            raise ValueError('Existing shared plan has different evidence, scope, or implementation; choose a new directory')
        return saved
    # Verify every imported curve once before compiling. Save original provenance.
    for spec in {c['curve']['path']: c['curve'] for c in candidates}.values():
        checked_stream(Path(spec['path']).parent, Path(spec['path']).name, spec['sha256'])
    selections = select_intervals(candidates, start, end)
    output.mkdir(parents=True)
    statistics = compile_map(output/'tape_map.npy', candidates, selections, start, end)
    atomic_json(output/'selection.json', selections)
    atomic_json(output/'evidence.json', dict(candidates=candidates, rejected=rejected,
        interpretation='Saved channel decisions supply witnesses, never destination eligibility'))
    plan = dict(schema=3, kind='shared_tape_map', methodology=METHODOLOGY,
        complete=True, source_validation='verified', tape_level=True,
        selection_depends_on_output_channel=False, inventory=inv,
        full_tape=bool(source.get('full_tape') and start == 0 and end == inv['duration_seconds']),
        start_seconds=start, end_seconds=end, cleanup_version='v2',
        map=dict(file='tape_map.npy', sha256=sha256(output/'tape_map.npy'), **statistics),
        selection=dict(file='selection.json', sha256=sha256(output/'selection.json')),
        evidence=dict(file='evidence.json', sha256=sha256(output/'evidence.json')),
        sources=bindings, implementation_sha256=sha256(__file__),
        listening_approved=False, production_accepted=False,
        limitations=['Saved numerical tone evidence does not independently authenticate tape motion.',
                    'Missing reserved events remain visible; no new discovery or fitting was run.'])
    atomic_json(output/'plan.json', plan)
    return plan


def load_tape_plan(path):
    path = index_path(path)
    plan = json.loads(path.read_text(encoding='utf-8'))
    if (plan.get('schema') != 3 or plan.get('methodology') != METHODOLOGY
            or plan.get('kind') != 'shared_tape_map' or plan.get('complete') is not True
            or plan.get('tape_level') is not True or plan.get('source_validation') != 'verified'
            or plan.get('cleanup_version') != 'v2'
            or plan.get('selection_depends_on_output_channel') is not False):
        raise ValueError('Stage 05 requires a completed shared tape map; import old evidence with stage 05a')
    for row in plan['inventory']['channels']:
        stat = Path(row['path']).stat()
        if (stat.st_size, stat.st_mtime_ns) != (row['bytes'], row['mtime_ns']):
            raise ValueError('Original source identity changed since shared planning')
    for name in ('map', 'selection', 'evidence'):
        spec = plan[name]
        checked_stream(path.parent, spec['file'], spec['sha256'])
    data = np.load(path.parent/plan['map']['file'], mmap_mode='r', allow_pickle=False)
    if (data.dtype != np.dtype('<f8') or data.shape != (plan['map']['rows'], 4)
            or not data.flags.f_contiguous):
        raise ValueError('Shared tape map shape or dtype changed')
    for begin in range(0, len(data), 200000):
        part = data[max(0, begin-1):begin+200000]
        if (not np.isfinite(part).all() or np.any(np.diff(part[:, 0]) <= 0)
                or np.any(np.diff(part[:, 1]) <= 0) or np.max(abs(part[:, 2])) > .05
                or np.any((part[:, 3] < 0) | (part[:, 3] > 1))
                or np.any(abs(part[part[:, 3] == 0, 2]) > 1e-12)):
            raise ValueError('Shared map failed timing/support integrity checks')
    if (data[0, 0] != plan['start_seconds'] or data[-1, 0] != plan['end_seconds']
            or data[0, 1] != data[0, 0] or data[-1, 1] != data[-1, 0]):
        raise ValueError('Shared map canonical scope or endpoint anchoring changed')
    return path, plan, data
