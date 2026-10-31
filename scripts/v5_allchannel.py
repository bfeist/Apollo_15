"""V5 unconditional contextual evidence survey and conservative planner.

Survey records are observations, not proof of independent tape motion. Candidate
frequencies are frozen before target-specific calibration and validation.
"""
import json
import hashlib
import math
from pathlib import Path

import numpy as np
import soundfile as sf

from flutter_planning import inventory, read_channel, write_json, STAMP, seconds
from continuous_acquisition import ROLE_CYCLE
from flutter_tracking import discover, trajectory, RATE
from carrier_capture import capture_candidates
import continuous_planning

METHODOLOGY = 'continuous-allchannel-v5'


def survey_tape(source, output, *, start=0., duration=None, monitor_seconds=60.,
                context_seconds=4., candidates_per_channel=8, max_windows=None, denoised_root=None):
    """Read every channel for each core window and record each overlapping second.

    Failures are per-channel, not hidden by successful reads from other channels.
    Truncated boundary contexts remain explicit, including clips too short to track.
    """
    try:
        inv = inventory(source)
    except (OSError, ValueError, RuntimeError) as exc:
        output = Path(output)
        output.mkdir(parents=True, exist_ok=False)
        write_json(output/'survey_manifest.json', dict(methodology=METHODOLOGY, complete=False,
            all_recorded_sources_covered=False, status='inventory_error', reason=str(exc),
            analysis_gap='Entire requested interval; source inventory failed'))
        raise ValueError('V5 source inventory failed; see survey_manifest.json: '+str(exc)) from exc
    end = min(inv['duration_seconds'], start + duration) if duration is not None else inv['duration_seconds']
    if not (math.isfinite(start) and math.isfinite(end) and math.isfinite(monitor_seconds)
            and math.isfinite(context_seconds) and (duration is None or math.isfinite(duration))
            and 0 <= start < end and monitor_seconds >= 20 and context_seconds >= 4
            and isinstance(candidates_per_channel, int) and candidates_per_channel > 0
            and (max_windows is None or isinstance(max_windows, int) and max_windows > 0)):
        raise ValueError('Invalid survey interval or context')
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    windows = []
    unassigned = []
    for index, item in enumerate(inv.get('excluded_files', [])):
        source_id = 'unassigned_'+str(index+1)
        try:
            path = Path(item['path']); stamp = STAMP.search(path.name); info = sf.info(path)
            if not stamp or info.channels != 1 or info.samplerate % 1000 or not 6000 <= info.samplerate <= 48000:
                raise ValueError('Unassigned source timestamp or audio format unsupported')
            offset = seconds(stamp[1])-inv['origin_filename_seconds']
            if abs(info.duration-(seconds(stamp[2])-seconds(stamp[1]))) > 1:
                raise ValueError('Unassigned source duration disagrees with timestamp')
            unassigned.append(dict(channel=source_id, path=str(path), bytes=item['bytes'],
                mtime_ns=item['mtime_ns'], sample_rate=info.samplerate, frames=info.frames,
                start_seconds=offset, end_seconds=offset+info.duration,
                role='unassigned_survey_only'))
        except (OSError, ValueError, RuntimeError) as exc:
            unassigned.append(dict(channel=source_id, path=item['path'],
                start_seconds=start, end_seconds=end, error=str(exc)))
    source_end = max([inv['duration_seconds']]+[r['end_seconds'] for r in unassigned if not r.get('error')])
    end = min(source_end, start+duration) if duration is not None else source_end
    coverage_path = output / 'survey.jsonl'
    cursor = start
    with coverage_path.open('w', encoding='utf-8') as stream:
        while cursor < end - 1e-8 and (max_windows is None or len(windows) < max_windows):
            high = min(end, cursor + monitor_seconds)
            window = dict(start_seconds=cursor, end_seconds=high, channels={})
            for row in inv['channels']+unassigned:
                ch = row['channel']
                a, b = max(row['start_seconds'], cursor-context_seconds), min(row['end_seconds'], high+context_seconds)
                if b > a and b-a < 10:
                    a = max(row['start_seconds'], b-10)
                    b = min(row['end_seconds'], max(b, a+10))
                result = dict(context_start=a, context_end=b, candidates=[], status='unavailable')
                if b > a:
                    try:
                        if row.get('error'):
                            raise ValueError(row['error'])
                        x, sr, digest = read_channel(row, a, b)
                        result.update(status='analyzed', source_sha256=digest)
                        if b-a < 10:
                            result.update(status='insufficient_context', reason='Less than 10 seconds of source context')
                        else:
                            raw_candidates = discover(x, sr, candidates_per_channel)
                            result['discovery_sources'] = {'original': 'analyzed'}
                            discovered = [dict(c, provenance=['original']) for c in raw_candidates]
                            if denoised_root is not None and isinstance(ch, int):
                                try:
                                    folder = Path(denoised_root)/f'CH{ch:02d}'
                                    raw = (folder/'denoised.json').read_bytes()
                                    metadata = json.loads(raw)
                                    signature = metadata['signature']
                                    identity = {k:row[k] for k in ('path','bytes','mtime_ns','frames','sample_rate','channel')}
                                    if (metadata['status'] != 'complete' or signature['source'] != identity
                                        or signature['offset_frames'] != 0 or signature['frames'] != row['frames']):
                                        raise ValueError('Denoised source identity or alignment mismatch')
                                    path = folder/'denoised.wav'
                                    info, stat = sf.info(path), path.stat()
                                    if info.frames != row['frames'] or info.samplerate != sr or info.channels != 1:
                                        raise ValueError('Denoised audio format mismatch')
                                    with sf.SoundFile(path) as f:
                                        f.seek(round((a-row['start_seconds'])*sr))
                                        clean = f.read(len(x), dtype='float64')
                                    after = path.stat()
                                    if len(clean) != len(x) or not np.isfinite(clean).all() or (stat.st_size,stat.st_mtime_ns) != (after.st_size,after.st_mtime_ns):
                                        raise ValueError('Denoised source changed or unreadable')
                                    for candidate in discover(clean, sr, candidates_per_channel):
                                        existing = next((c for c in discovered if abs(c['carrier_hz']-candidate['carrier_hz']) < .5), None)
                                        if existing is not None:
                                            existing['provenance'].append('denoised')
                                        else:
                                            discovered.append(dict(candidate, provenance=['denoised']))
                                    result['discovery_sources']['denoised'] = dict(status='analyzed',
                                        manifest_sha256=hashlib.sha256(raw).hexdigest(), bytes=stat.st_size, mtime_ns=stat.st_mtime_ns,
                                        decoded_excerpt_sha256=hashlib.sha256(clean.tobytes()).hexdigest(),
                                        full_source_decoded_hash_reverified=False)
                                except (OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
                                    result['discovery_sources']['denoised'] = dict(status='analysis_gap', reason=str(exc))
                            for candidate in discovered:
                                c = dict(candidate, tracking_status='unsupported', support_seconds=[])
                                if abs(c['carrier_hz']/60-round(c['carrier_hz']/60))*60 <= .5:
                                    c.update(tracking_status='excluded_digitization_mains',
                                        reason='60 Hz harmonic contamination from digitization; already removed in denoising')
                                    result['candidates'].append(c)
                                    continue
                                if abs(c['carrier_hz']/60-round(c['carrier_hz']/60))*60 < 3:
                                    c['provenance_ambiguous'] = 'Near a digitization-mains harmonic; independent target controls required'
                                for capture in capture_candidates(c['carrier_hz'], sr, (2., 30.)):
                                    try:
                                        trace = trajectory(x, sr, c['carrier_hz'], capture)
                                        support = trace[5]
                                        if not c['support_seconds'] or np.mean(support >= .95) > c['support_fraction']:
                                            c.update(tracking_status='measured', capture_hz=capture,
                                                support_fraction=float(np.mean(support >= .95)),
                                                support_seconds=[int(i) for i in range(math.floor(cursor), math.ceil(high))
                                                    if max(i, cursor, a) < min(i+1, high, b)
                                                    and np.all(support[max(0, math.floor((max(i,cursor,a)-a)*RATE)):
                                                        min(len(support), math.ceil((min(i+1,high,b)-a)*RATE))] >= .95)])
                                    except (ValueError, FloatingPointError) as exc:
                                        c['tracking_error'] = str(exc)
                                result['candidates'].append(c)
                    except (OSError, ValueError, RuntimeError) as exc:
                        result.update(status='read_error', reason=str(exc))
                window['channels'][str(ch)] = result
                # Each cell records exact intersected coverage and analysis status.
                for sec in range(math.floor(cursor), math.ceil(high)):
                    lo, hi = max(cursor, sec), min(high, sec+1)
                    av_lo, av_hi = max(lo, row['start_seconds']), min(hi, row['end_seconds'])
                    record = dict(channel=ch, second=sec, start_seconds=lo, end_seconds=hi,
                        available_start=min(hi, max(lo, av_lo)), available_end=min(hi, max(lo, av_hi)),
                        status=result['status'] if av_hi > av_lo else 'unavailable',
                        context_start=a, context_end=b,
                        supported_candidates=[c['carrier_hz'] for c in result['candidates'] if sec in c['support_seconds']])
                    if result.get('reason'):
                        record['reason'] = result['reason']
                    stream.write(json.dumps(record, allow_nan=False)+'\n')
            # Missing channel numbers are metadata, not assumptions about adjacency.
            for ch in inv['missing_channels']:
                for sec in range(math.floor(cursor), math.ceil(high)):
                    stream.write(json.dumps(dict(channel=ch, second=sec, start_seconds=max(cursor,sec),
                        end_seconds=min(high,sec+1), status='missing_channel'))+'\n')
            windows.append(window)
            cursor = high
    # Explicitly identify the unprocessed tail when a bounded run stops early.
    if cursor < end - 1e-8:
        with coverage_path.open('a', encoding='utf-8') as stream:
            for ch in [r['channel'] for r in inv['channels']+unassigned] + inv['missing_channels']:
                for sec in range(math.floor(cursor), math.ceil(end)):
                    stream.write(json.dumps(dict(channel=ch, second=sec, start_seconds=max(cursor, sec),
                        end_seconds=min(end, sec+1), status='analysis_gap'))+'\n')
    manifest = dict(methodology=METHODOLOGY, inventory=inv, start_seconds=start,
        requested_end_seconds=end, surveyed_end_seconds=cursor, complete=cursor >= end-1e-8,
        all_recorded_sources_covered=(cursor >= end-1e-8 and
            all(c['status'] == 'analyzed' for w in windows for c in w['channels'].values()
                if c['context_end'] > c['context_start'])),
        context_seconds=context_seconds, monitor_seconds=monitor_seconds, candidates_per_channel=candidates_per_channel,
        denoised_root=str(Path(denoised_root).resolve()) if denoised_root is not None else None,
        event_role_cycle=list(ROLE_CYCLE),
        evidence_protocol='Role cycle assigned before predictive candidate scoring. Survey is unlabeled predictor feature generation; discovery excludes the 2150-2850 Hz target check-tone band. Reserved target response outcomes are not used in candidate scoring.',
        unassigned_sources=unassigned,
        source_end_seconds=source_end,
        sources_before_timeline_origin=[dict(path=r['path'], start_seconds=r['start_seconds'], end_seconds=min(0.,r['end_seconds']),
            status='outside_requested_timeline') for r in unassigned if r['start_seconds'] < 0],
        interpretation='Surveyed candidates are hypotheses, not independent or validated corrections; 60 Hz harmonic frequencies within 0.5 Hz are excluded as digitization contamination; nearby candidates remain ambiguous',
        windows=windows, coverage_file=coverage_path.name)
    write_json(output/'survey_manifest.json', manifest)
    return manifest


def surveyed_candidates(manifest, channel, low, high):
    """Use only completed windows and never rank a frequency by future support."""
    selected = {}
    for window in manifest['windows']:
        if window['end_seconds'] <= low or window['end_seconds'] > high:
            continue
        for candidate in window['channels'].get(str(channel), {}).get('candidates', []):
            if candidate['tracking_status'] != 'measured':
                continue
            key = round(candidate['carrier_hz'], 1)
            if key not in selected:
                selected[key] = candidate
    return list(selected.values())


def plan_tape(source, output, *, start=0., duration=None, monitor_seconds=60.,
              context_seconds=4., candidates_per_channel=8, reference_limit=8,
              max_windows=None, chunk_seconds=7., resume=False, reuse=False, denoised_root=None):
    if reuse:
        raise ValueError('Reuse requires explicit resume and a matching v5 checkpoint')
    output = Path(output).resolve()
    source = Path(source).resolve()
    if source == output or source in output.parents or output in source.parents:
        raise ValueError('Input and output must be non-overlapping')
    if resume:
        if not (output/'plan'/'plan.json').exists():
            raise ValueError('No v5 planning checkpoint; interrupted surveys are not resumable')
        manifest = json.loads((output/'survey_manifest.json').read_text(encoding='utf-8'))
        source_end = max([inventory(source)['duration_seconds']]+[
            r['end_seconds'] for r in manifest.get('unassigned_sources', []) if not r.get('error')])
        expected_end = min(source_end, start+duration) if duration is not None else source_end
        if (manifest['methodology'] != METHODOLOGY or manifest['inventory'] != inventory(source)
            or manifest['start_seconds'] != start or manifest['requested_end_seconds'] != expected_end
            or manifest['monitor_seconds'] != monitor_seconds or manifest['context_seconds'] != context_seconds
            or manifest.get('candidates_per_channel') != candidates_per_channel
            or manifest.get('denoised_root') != (str(Path(denoised_root).resolve()) if denoised_root is not None else None)):
            raise ValueError('V5 survey source or settings changed')
        if denoised_root is not None:
            for window in manifest['windows']:
                for channel, result in window['channels'].items():
                    clean = result.get('discovery_sources', {}).get('denoised')
                    if not isinstance(clean, dict) or clean.get('status') != 'analyzed':
                        continue
                    folder = Path(denoised_root)/f'CH{int(channel):02d}'
                    stat = (folder/'denoised.wav').stat()
                    if ((stat.st_size, stat.st_mtime_ns) != (clean['bytes'], clean['mtime_ns']) or
                        hashlib.sha256((folder/'denoised.json').read_bytes()).hexdigest() != clean['manifest_sha256']):
                        raise ValueError('Denoised survey source changed since checkpoint')
    else:
        manifest = survey_tape(source, output, start=start, duration=duration,
            monitor_seconds=monitor_seconds, context_seconds=context_seconds,
            candidates_per_channel=candidates_per_channel, max_windows=None, denoised_root=denoised_root)
    def provider(channel, low, high):
        return surveyed_candidates(manifest, channel, low, high)
    plan = continuous_planning.plan_tape(source, output/'plan', start=start, duration=duration,
        monitor_seconds=monitor_seconds, context_seconds=context_seconds,
        candidates_per_channel=candidates_per_channel, reference_limit=reference_limit,
        max_windows=max_windows, chunk_seconds=chunk_seconds, candidate_provider=provider,
        methodology_override=METHODOLOGY, reuse=False, resume=resume, allow_transfer_group=False, tolerate_read_errors=True,
        survey_signature=dict(manifest_sha256=hashlib.sha256((output/'survey_manifest.json').read_bytes()).hexdigest(),
                              coverage_sha256=hashlib.sha256((output/'survey.jsonl').read_bytes()).hexdigest()))
    return plan
