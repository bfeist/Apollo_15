"""Historical map evaluation and stage-03 cache helpers.

The active deflutter renderer is shared_render; this module does not publish
channel reviews or listening pages.
"""
from collections import deque
from concurrent.futures import ProcessPoolExecutor
from contextlib import closing, nullcontext
from multiprocessing import get_context
import hashlib
import inspect
import json
import os
import math
from pathlib import Path
import time

import numpy as np
import soundfile as sf
from scipy.integrate import cumulative_trapezoid

from audio_processing import clean_tones, denoise, DEFAULT_BROADBAND_CAP_DB, TONAL_CLEANUP_METHOD
from denoise_archive import (cleanup_version, denoised_audio_path, historical_path, legacy_write,
                             read_json, root_marker, sha256, validate_binding)
from flutter import resample_at
from flutter_plan_store import checked_artifact, load_saved_plan, planning_lock, tape_directory

RATE = 200

# Imported lazily in the dispatchers to keep the regional renderer available
# independently of the continuous methodology's implementation modules.


def write_json(path, value):
    temporary = path.with_suffix(path.suffix + '.partial')
    temporary.write_text(json.dumps(value, indent=2, allow_nan=False), encoding='utf-8')
    temporary.replace(path)


def check_source(row):
    stat = Path(row['path']).stat()
    if stat.st_size != row['bytes'] or stat.st_mtime_ns != row['mtime_ns']:
        raise ValueError('Original source changed since planning')


def separate(destination, protected):
    for path in protected:
        path = Path(path).resolve()
        if destination == path or destination in path.parents or path in destination.parents:
            raise ValueError('Output must not overlap originals or saved plans')


def prepare(plan_path, channel, start=0., duration=None):
    plan_path = Path(plan_path).resolve()
    if plan_path.is_dir():
        plan_path /= 'plan.json'
    plan = load_saved_plan(plan_path, expand_regions=False)
    row = next((r for r in plan['inventory']['channels'] if r['channel'] == channel), None)
    if row is None or row['role'] == 'timing_only':
        raise ValueError('Choose an available non-IRIG channel')
    if duration is None:
        if not plan.get('full_tape'):
            raise ValueError('A partial plan needs an explicit --start and --duration')
        duration = row['end_seconds'] - start
    if not np.isfinite([start, duration]).all() or duration < 15:
        raise ValueError('Review range must be finite and at least 15 seconds')
    sr = row['sample_rate']
    offset = round((start-row['start_seconds'])*sr)
    frames = round(duration*sr)
    start = row['start_seconds']+offset/sr
    end = start+frames/sr
    if offset < 0 or offset+frames > row['frames']:
        raise ValueError('Review range exceeds the target channel coverage')
    regions = [r for r in plan['regions'] if r['end_seconds'] > start and r['start_seconds'] < end]
    cursor = start
    for r in regions:
        if r['status'] != 'analyzed' or r['start_seconds'] > cursor+1e-8:
            raise ValueError('Saved plan has an unplanned gap in the review range')
        cursor = max(cursor, r['end_seconds'])
    if not regions or cursor < end-1e-8:
        raise ValueError('Saved plan does not cover the review range')
    info = sf.info(row['path'])
    if info.channels != 1 or info.frames != row['frames'] or info.samplerate != sr:
        raise ValueError('Original audio header differs from inventory')
    return plan_path, plan, row, regions, start, offset, frames


def region_curve(folder, summary, channel, experimental):
    _, raw = checked_artifact(folder, summary['detail_file'], summary['detail_sha256'])
    r = json.loads(raw)
    target = next((t for t in r['targets'] if t['channel'] == channel), None)
    if target is None:
        raise ValueError(f'{r["id"]}: missing target decision')
    action = target['action']
    if action not in ('render_and_review', 'group_render_and_review', 'validate_transfer', 'leave_uncorrected'):
        raise ValueError('Unknown target decision')
    enabled = action in {'render_and_review', 'group_render_and_review'} or (action == 'validate_transfer' and experimental)
    result = dict(region=r, target=target, enabled=enabled, speed=None, support=None)
    if enabled:
        if not r.get('maps') or not target.get('selected'):
            raise ValueError('Enabled region has no saved map')
        path, _ = checked_artifact(folder, r['maps']['file'], r['maps']['sha256'])
        with np.load(path, allow_pickle=False) as archive:
            speed = archive[target['selected']].copy()
            support = archive[target['selected']+'__support'].copy()
        n = round((r['analysis_end']-r['analysis_start'])*RATE)
        if (r['maps']['rate'] != RATE or speed.shape != (n,) or support.shape != (n,)
                or n < 2 or not np.isfinite(speed).all() or not np.isfinite(support).all()
                or np.max(abs(speed)) > .05 or np.min(support) < 0 or np.max(support) > 1
                or np.any(np.abs(speed[support == 0]) > 1e-12)):
            raise ValueError('Invalid saved speed/support map')
        result.update(speed=speed, support=support)
    return result


def sample_curve(curve, times):
    if not curve['enabled']:
        return np.zeros(len(times)), np.zeros(len(times))
    r = curve['region']
    tt = r['analysis_start']+np.arange(len(curve['speed']))/RATE
    return (np.interp(times, tt, curve['speed'], left=0, right=0),
            np.interp(times, tt, curve['support'], left=0, right=0))


def rising(x):
    return .5-.5*np.cos(np.pi*np.clip(x, 0, 1))


def continuous_map(folder, regions, channel, start, frames, sr, experimental=False, progress=print):
    if regions and regions[0].get('kind') == 'continuous_reference':
        from continuous_maps import saved_continuous_map
        if experimental:
            raise ValueError('Continuous plans do not contain experimental transfer proposals')
        return saved_continuous_map(folder, regions[0], channel, start, frames, sr)
    # End at the last sample, including a fractional final tracking interval.
    last = (frames-1)/sr
    tt = np.arange(math.floor(last*RATE)+1, dtype=float)/RATE
    if last > tt[-1]+1e-12:
        tt = np.r_[tt, last]
    speed, envelope = np.zeros(len(tt)), np.zeros(len(tt))
    markers, evidence = [], []
    previous = None
    for index, summary in enumerate(regions):
        current = region_curve(folder, summary, channel, experimental)
        r, target = current['region'], current['target']
        a, b = max(start, r['start_seconds']), min(start+frames/sr, r['end_seconds'])
        lo, hi = np.searchsorted(tt, [a-start, b-start])
        speed[lo:hi], envelope[lo:hi] = sample_curve(current, start+tt[lo:hi])
        status = ('numeric_pass' if target['action'] == 'render_and_review' else
                  'group_numeric_pass' if target['action'] == 'group_render_and_review' else 'experimental') if current['enabled'] else 'bypassed'
        markers.append(dict(start_seconds=a-start, end_seconds=b-start, kind=status,
                            region=r['id'], selected=target.get('selected'), reason=target['reason']))
        evidence.append(dict(region=r['id'], detail_sha256=summary['detail_sha256'],
                             maps_sha256=r.get('maps', {}).get('sha256'), decision=target['action'], applied=current['enabled']))
        if current['enabled']:
            for gap in target.get('reference_spans', []):
                ga, gb = max(a, gap['start_seconds']), min(b, gap['end_seconds'])
                if ga < gb and gap['action'] != 'use_reference':
                    markers.append(dict(start_seconds=ga-start, end_seconds=gb-start, kind='support_gap',
                                        region=r['id'], reason=gap['action']))
        if previous is not None:
            left = previous['region']
            boundary = r['start_seconds']
            # Never allow neighboring transition windows to overlap.
            width = min(1., (left['end_seconds']-left['start_seconds'])/2,
                        (r['end_seconds']-r['start_seconds'])/2)
            # A tiny bypass tail need not shorten the outgoing fade.
            left_width = min(1., (left['end_seconds']-left['start_seconds'])/2)
            right_width = min(1., (r['end_seconds']-r['start_seconds'])/2)
            oa = max(left['analysis_start']+2, r['analysis_start']+2)
            ob = min(left['analysis_end']-2, r['analysis_end']-2)
            support_fraction, disagreement, blend = 0., None, False
            if previous['enabled'] and current['enabled'] and ob > oa:
                overlap = np.arange(oa, ob, 1/RATE)
                le, lg = sample_curve(previous, overlap)
                re, rg = sample_curve(current, overlap)
                supported = (lg >= .95) & (rg >= .95)
                support_fraction = float(np.mean(supported))
                if supported.any():
                    disagreement = float(np.sqrt(np.mean((le[supported]-re[supported])**2)))
                    blend = support_fraction >= .9 and disagreement <= .005
            if blend:
                lo, hi = np.searchsorted(tt, [boundary-width-start, boundary+width-start])
                times = start+tt[lo:hi]
                weight = rising((times-boundary+width)/(2*width))
                le, lg = sample_curve(previous, times)
                re, rg = sample_curve(current, times)
                speed[lo:hi] = le*(1-weight)+re*weight
                envelope[lo:hi] = lg*(1-weight)+rg*weight
                join_action = 'blend'
            else:
                for curve, low, high, incoming in ((previous, boundary-left_width, boundary, False),
                                                   (current, boundary, boundary+right_width, True)):
                    lo, hi = np.searchsorted(tt, [low-start, high-start])
                    times = start+tt[lo:hi]
                    e, g = sample_curve(curve, times)
                    weight = rising((times-low)/(high-low))
                    if not incoming:
                        weight = 1-weight
                    speed[lo:hi], envelope[lo:hi] = e*weight, g*weight
                join_action = 'fade'
            if previous['enabled'] or current['enabled']:
                markers.append(dict(start_seconds=max(0., boundary-left_width-start),
                                    end_seconds=min(last, boundary+right_width-start), kind=join_action,
                                    region=left['id']+' / '+r['id'], reason='Adjacent saved speed curves',
                                    overlap_supported_fraction=support_fraction, speed_rms_difference=disagreement))
        previous = current
        if (index+1) % 100 == 0 or index+1 == len(regions):
            progress(f'Maps: {index+1}/{len(regions)} regions')
    fade = rising(np.minimum(tt, last-tt)/2.)
    speed *= fade
    envelope *= fade
    # Remove only the integrated DC drift, weighted to enabled/supported spans.
    # Bypassed spans keep zero local speed correction, not a reset of displacement.
    area = float(np.trapezoid(envelope, tt))
    dc = float(np.trapezoid(speed, tt)/area) if area else 0.
    speed -= dc*envelope
    if np.max(abs(speed)) > .05:
        raise ValueError('Duration anchoring exceeds the 5% speed bound')
    delay = cumulative_trapezoid(speed, tt, initial=0)
    delay[-1] = 0.
    nominal = tt+delay
    if not np.isfinite(nominal).all() or np.any(np.diff(nominal) <= 0):
        raise ValueError('Continuous timing map must be finite and strictly monotonic')
    stats = dict(rate=RATE, frames=frames, sample_rate=sr, last_sample_seconds=last,
                 dc_speed_removed=dc, maximum_displacement_seconds=float(np.max(abs(delay))),
                 maximum_speed_correction=float(np.max(abs(speed))),
                 speed_limit_saturation_fraction=float(np.mean(abs(speed) >= .049)),
                 reference_switches=sum(a.get('selected') != b.get('selected') for a, b in zip(
                     [m for m in markers if m['kind'] in {'numeric_pass','group_numeric_pass','experimental','bypassed'}],
                     [m for m in markers if m['kind'] in {'numeric_pass','group_numeric_pass','experimental','bypassed'}][1:])),
                 fade_count=sum(m['kind'] == 'fade' for m in markers),
                 blend_count=sum(m['kind'] == 'blend' for m in markers),
                 nonzero_speed_fraction=float(np.mean(abs(speed)>1e-12)),
                 integration='Single trapezoidal integration after speed blending/fading; support-weighted DC anchoring',
                 bypass='Zero local speed correction; a constant timing offset can remain for continuity')
    return tt, nominal, speed, stats, sorted(markers, key=lambda m:m['start_seconds']), evidence


def joined_map_checks(folder, regions, row, channel, start, tt, nominal, speed):
    """Measure original check tones through the exact final inverse map.

Boundary/dropout events were excluded from calibration and regional holdouts.
No alternative is fitted after these results; failure stays a failure.
"""
    if regions and regions[0].get('kind') == 'continuous_reference':
        from continuous_maps import continuous_holdout_checks
        return continuous_holdout_checks(folder, regions[0], row, channel, start, tt, nominal, speed)
    from flutter_tracking import evaluate_events
    from flutter_policy import LIMITS
    observations, seen = [], set()
    sr = row['sample_rate']
    displacement = float(np.max(abs(nominal-tt)))
    for summary in regions:
        _, raw = checked_artifact(folder, summary['detail_file'], summary['detail_sha256'])
        r = json.loads(raw)
        target = next(t for t in r['targets'] if t['channel'] == channel)
        if target['action'] not in {'render_and_review', 'group_render_and_review'}:
            continue
        for event in target.get('reserved_events', []):
            a = r['analysis_start']+event['start']
            b = r['analysis_start']+event['end']
            key = round(a*sr)
            if key in seen or a < start+.5 or b > start+tt[-1]-.5:
                continue
            seen.add(key)
            low = max(start, a-.5-displacement)
            high = min(start+tt[-1], b+.5+displacement)
            offset = round((low-row['start_seconds'])*sr)
            count = round((high-low)*sr)
            low = row['start_seconds']+offset/sr
            with sf.SoundFile(row['path']) as stream:
                stream.seek(offset)
                x = stream.read(count, dtype='float64')
            output_times = low-start+np.arange(len(x))/sr
            input_times = np.interp(output_times, nominal, tt)
            pos = (input_times-(low-start))*sr
            if np.any(np.diff(pos) <= 0):
                continue
            metrics = evaluate_events(x, sr, None, [dict(event, start=a-low, end=b-low)], sample_positions=pos)
            margins = metrics[0]['margins'] if metrics else []
            passed = len(margins) == 3 and all(m['after_rms_hz'] <= LIMITS['max_residual_hz']
                and m['after_rms_cents'] <= LIMITS['max_residual_cents']
                and m['after_rms_hz'] <= max(m['before_rms_hz']*1.05, m['before_rms_hz']+.1) for m in margins)
            observations.append(dict(region=r['id'], original_start_seconds=a,
                                     status='passed' if passed else 'failed', metrics=metrics))
    status = ('passed' if len(observations) >= 2 else 'insufficient_reserved_events')
    if any(o['status'] == 'failed' for o in observations):
        status = 'failed'
    if not np.any(abs(speed) > 1e-12):
        status = 'passed'
    return dict(status=status, reserved_events=observations, no_motion=not bool(np.any(abs(speed) > 1e-12)),
                map='Exact final continuous joined, faded and duration-anchored inverse map',
                failed_choice_refitted=False, listening_approved=False)


def wav_format(frames):
    return 'RF64' if frames*4+4096 >= 2**32 else 'WAV'


def cleanup_block(row, start_frame, frames, hop, begin):
    """Read and clean one contextual block; workers never write shared output."""
    check_source(row)
    sr = row['sample_rate']
    end = min(frames, begin+2*hop)
    absolute_begin, absolute_end = start_frame+begin, start_frame+end
    low, high = max(0, absolute_begin-4*sr), min(row['frames'], absolute_end+4*sr)
    # Give short tails the same real context as the serial implementation.
    low = max(0, min(low, high-20*sr))
    high = min(row['frames'], max(high, low+20*sr))
    with sf.SoundFile(row['path']) as source:
        source.seek(low)
        x = source.read(high-low, dtype='float64')
    if len(x) != high-low or not np.isfinite(x).all():
        raise ValueError('Invalid source audio block')
    y, tones = clean_tones(x, sr)
    y, noise_window = denoise(y, sr, DEFAULT_BROADBAND_CAP_DB)
    local = absolute_begin-low
    take = min(hop, frames-begin)
    raw = x[local:local+take].tobytes()
    y = y[local:local+end-begin].copy()
    if len(y) != end-begin or not np.isfinite(y).all():
        raise ValueError('Cleanup produced invalid samples')
    check_source(row)
    record = dict(start_frame=begin, end_frame=end, context_start_frame=low,
                  context_end_frame=high, noise_window_seconds=noise_window, tones=tones)
    return y, raw, record


def cleanup_results(row, start_frame, frames, hop, workers):
    """Yield in timeline order with at most one queued block per worker."""
    starts = iter(range(0, frames, hop))
    if workers == 1:
        for begin in starts:
            yield cleanup_block(row, start_frame, frames, hop, begin)
        return
    # Spawned children inherit these before importing NumPy/SciPy.
    names = ('OPENBLAS_NUM_THREADS', 'OMP_NUM_THREADS', 'MKL_NUM_THREADS')
    previous = {name: os.environ.get(name) for name in names}
    pool = None
    pending = deque()
    try:
        for name in names:
            os.environ[name] = '1'
        pool = ProcessPoolExecutor(max_workers=workers, mp_context=get_context('spawn'))
        for _ in range(workers):
            begin = next(starts, None)
            if begin is not None:
                pending.append(pool.submit(cleanup_block, row, start_frame, frames, hop, begin))
        while pending:
            result = pending.popleft().result()
            yield result
            begin = next(starts, None)
            if begin is not None:
                pending.append(pool.submit(cleanup_block, row, start_frame, frames, hop, begin))
    finally:
        for future in pending:
            future.cancel()
        if pool is not None:
            pool.shutdown(wait=True, cancel_futures=True)
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def cleanup_channel(row, start_frame, frames, output, log_path, block_seconds=60., progress=print, workers=1):
    """Contextual processing with complementary half-cosine overlap/add."""
    sr = row['sample_rate']
    if not np.isfinite(block_seconds) or not 20 <= block_seconds <= 120:
        raise ValueError('Cleanup block must be 20-120 seconds')
    if not isinstance(workers, int) or workers < 0:
        raise ValueError('Cleanup workers must be a nonnegative integer')
    if workers == 0:
        workers = min(16, max(1, (os.cpu_count() or 2)-1))
    hop = round(block_seconds*sr/2)
    pending, pending_weight = np.zeros(0), np.zeros(0)
    digest, peak = hashlib.sha256(), 0.
    blocks = math.ceil(frames/hop)
    workers = min(workers, blocks)
    started = time.monotonic()
    progress(f'Cleanup: {blocks} blocks, {workers} worker(s)')
    results = cleanup_results(row, start_frame, frames, hop, workers)
    with closing(results), sf.SoundFile(output, 'w', samplerate=sr, channels=1,
            subtype='FLOAT', format=wav_format(frames)) as dest, log_path.open('w', encoding='utf-8') as log:
        for i, (y, raw, record) in enumerate(results):
            check_source(row)
            begin = record['start_frame']
            take = min(hop, frames-begin)
            digest.update(raw)
            weight = np.ones(len(y))
            if begin:
                weight[:min(hop,len(y))] = rising(np.arange(min(hop,len(y)))/hop)
            if len(y)>hop:
                weight[hop:] = 1-rising(np.arange(len(y)-hop)/hop)
            mixed = y*weight
            mixed[:len(pending)] += pending
            weight[:len(pending_weight)] += pending_weight
            if np.min(weight[:take]) <= 0:
                raise ValueError('Cleanup overlap left an uncovered sample')
            samples = mixed[:take]/weight[:take]
            peak = max(peak,float(np.max(abs(samples))))
            dest.write(samples)
            pending, pending_weight = mixed[take:].copy(), weight[take:].copy()
            log.write(json.dumps(record)+'\n')
            log.flush()
            elapsed = time.monotonic()-started
            remaining = elapsed/(i+1)*(blocks-i-1)
            progress(f'Cleanup: {i+1}/{blocks} blocks ({(begin+take)/sr/60:.1f} min audio; '
                     f'{elapsed:.1f}s elapsed; ETA {remaining/60:.1f} min)')
    check_source(row)
    return dict(method=TONAL_CLEANUP_METHOD, broadband_cap_db=DEFAULT_BROADBAND_CAP_DB,
                block_seconds=block_seconds, overlap_fraction=.5, context_seconds=4,
                input_excerpt_float64_sha256=digest.hexdigest(), peak=peak)


def render_warp(cleaned, output, tt, nominal, block_seconds=5., progress=print):
    """Inverse-map a single cleaned stream; source halos hide resampler edges."""
    if not np.isfinite(block_seconds) or not .1 <= block_seconds <= 30:
        raise ValueError('Render block must be 0.1-30 seconds')
    peak, maximum_step, minimum_step = 0., 0., float('inf')
    previous_pos = None
    with sf.SoundFile(cleaned) as source:
        sr, frames = source.samplerate, source.frames
        if abs(tt[-1]-(frames-1)/sr)>1e-9:
            raise ValueError('Timing map does not match cleaned audio')
        block = round(block_seconds*sr)
        with sf.SoundFile(output, 'w', samplerate=sr, channels=1, subtype='FLOAT', format=wav_format(frames)) as dest:
            for begin in range(0, frames, block):
                end = min(frames,begin+block)
                times = np.arange(begin,end)/sr
                lo = max(0,int(np.searchsorted(nominal,times[0]))-1)
                hi = min(len(tt),int(np.searchsorted(nominal,times[-1]))+2)
                pos = np.interp(times,nominal[lo:hi],tt[lo:hi])*sr
                steps = np.diff(pos if previous_pos is None else np.r_[previous_pos,pos])
                if len(steps):
                    minimum_step = min(minimum_step,float(np.min(steps)))
                    maximum_step = max(maximum_step,float(np.max(steps)))
                    if np.min(steps)<=0:
                        raise ValueError('Nonmonotonic inverse audio map')
                previous_pos = pos[-1]
                left = max(0,int(np.floor(pos[0]))-256)
                right = min(frames,int(np.ceil(pos[-1]))+257)
                source.seek(left)
                x = source.read(right-left,dtype='float64')
                if len(x)!=right-left or not np.isfinite(x).all():
                    raise ValueError('Invalid cleaned audio')
                if np.max(abs(pos-np.arange(begin,end)))<1e-7:
                    y = x[begin-left:end-left]  # Exact identity when the map is identity.
                else:
                    y = resample_at(x,pos-left)
                if len(y)!=end-begin or not np.isfinite(y).all():
                    raise ValueError('Invalid corrected audio')
                dest.write(y)
                peak = max(peak,float(np.max(abs(y))))
                if begin//block % 12 == 0 or end==frames:
                    progress(f'Deflutter: {end/frames:.1%} ({end/sr/60:.1f} min)')
    return dict(peak=peak,minimum_input_sample_step=minimum_step,maximum_input_sample_step=maximum_step,
                block_seconds=block_seconds,resampler='8x Kaiser FIR and cubic interpolation; 256-sample source halo')


def verify_audio(path, frames, sr):
    digest, peak = hashlib.sha256(), 0.
    with sf.SoundFile(path) as audio:
        if audio.frames != frames or audio.samplerate != sr or audio.channels != 1:
            raise ValueError('Written audio header verification failed')
        for x in audio.blocks(blocksize=sr*10,dtype='float32'):
            if not np.isfinite(x).all():
                raise ValueError('Written audio has non-finite samples')
            peak = max(peak,float(np.max(abs(x))))
            digest.update(x.tobytes())
    return dict(frames=frames,sample_rate=sr,peak=peak,decoded_float32_sha256=digest.hexdigest())


def denoised_directory(root, tape, channel, start=0., duration=None):
    folder = tape_directory(tape, root)/f'CH{channel:02d}'
    if start != 0 or duration is not None:
        folder /= f'start_{start:g}_duration_{duration:g}' if duration is not None else f'start_{start:g}_to_end'
    return folder


def denoised_signature(row, offset, frames, block_seconds=60.):
    code = Path(__file__).with_name('audio_processing.py').read_bytes()
    code += ''.join(inspect.getsource(f) for f in (cleanup_block, cleanup_results, cleanup_channel)).encode()
    return dict(source={k:row[k] for k in ('path','bytes','mtime_ns','frames','sample_rate','channel')},
                offset_frames=offset,frames=frames,block_seconds=block_seconds,
                cleanup_sha256=hashlib.sha256(code).hexdigest(),broadband_cap_db=DEFAULT_BROADBAND_CAP_DB)


def completed_denoised(row, folder, offset, frames, block_seconds=60., version=None):
    """Read-only pipeline check; avoid decoding a completed channel on every rerun."""
    try:
        folder = historical_path(folder) if version == 'v1' else Path(folder).resolve()
        saved = json.loads((folder/'denoised.json').read_text(encoding='utf-8'))
        if not isinstance(saved, dict):
            return False
        validate_binding(folder, saved, row, offset, frames, version)
        if cleanup_version(saved) == 'v1' and saved.get('signature') != denoised_signature(row, offset, frames, block_seconds):
            return False
        separate(folder.resolve(), [Path(row['path']).parent])
        check_source(row)
        audio = saved['audio']
        if not isinstance(audio, dict) or audio['frames'] != frames or audio['sample_rate'] != row['sample_rate']:
            return False
        return hashlib.sha256((folder/'cleanup.jsonl').read_bytes()).hexdigest() == saved['cleanup_log_sha256']
    except (ValueError, KeyError, TypeError, OSError, RuntimeError):
        return False


def ensure_denoised(row, folder, offset, frames, block_seconds=60., progress=print, require_existing=False, workers=1,
                    version=None):
    """Persist one aligned intermediate; reuse only verified identical cleanup."""
    folder = historical_path(folder) if require_existing and version == 'v1' else Path(folder).absolute()
    resolved = folder.resolve()
    manifest = resolved/'denoised.json'
    existing = read_json(manifest) if manifest.is_file() else None
    actual = cleanup_version(existing) if existing else None
    if version == 'v2' or actual == 'v2' or root_marker(resolved):
        if version == 'v1' or not completed_denoised(row, resolved, offset, frames, block_seconds, 'v2'):
            raise ValueError('V2 denoise cache is missing or incompatible; run the frozen v2 stage 03')
        path = denoised_audio_path(resolved, existing)
        if sha256(path) != existing['audio']['sha256']:
            raise ValueError('V2 denoise audio changed; refusing reuse')
        return dict(existing, reused=True, file=str(path))
    with (nullcontext() if require_existing else legacy_write(folder)):
        return _ensure_v1(row, resolved, offset, frames, block_seconds, progress, require_existing, workers)


def _ensure_v1(row, folder, offset, frames, block_seconds, progress, require_existing, workers):
    separate(folder,[Path(row['path']).parent])
    check_source(row)
    if offset < 0 or frames < 15*row['sample_rate'] or offset+frames > row['frames']:
        raise ValueError('Invalid channel/range for denoised intermediate')
    path = folder/'denoised.wav'
    manifest = folder/'denoised.json'
    if require_existing and not manifest.is_file():
        raise ValueError('Prepared denoised intermediate is missing; run stage 03 first')
    signature = denoised_signature(row, offset, frames, block_seconds)
    with (nullcontext() if require_existing else planning_lock(folder)):
        if manifest.is_file():
            saved = json.loads(manifest.read_text(encoding='utf-8'))
            if cleanup_version(saved) != 'v1':
                raise ValueError('Legacy writer cannot replace v2 audio')
            same_signature = saved.get('signature') == signature
            if not same_signature:
                old = dict(saved.get('signature') or {})
                old['cleanup_sha256'] = signature['cleanup_sha256']
                if old != signature or saved.get('status') not in {'running', 'failed', 'complete'} or require_existing:
                    raise ValueError('Denoised intermediate uses different source/settings; choose another temporary directory')
                progress('Cleanup implementation changed; reprocessing channel from the beginning')
            if same_signature and saved.get('status') == 'complete':
                verified = verify_audio(path,frames,row['sample_rate'])
                if verified != saved['audio']:
                    raise ValueError('Denoised intermediate changed; refusing reuse')
                if hashlib.sha256((folder/'cleanup.jsonl').read_bytes()).hexdigest() != saved['cleanup_log_sha256']:
                    raise ValueError('Denoised cleanup evidence changed')
                check_source(row)
                progress(f'Reusing verified denoised intermediate: {path}')
                return dict(saved,reused=True,file=str(path))
            if require_existing:
                raise ValueError('Prepared denoised intermediate is incomplete; rerun stage 03')
        elif folder.exists() and any(folder.iterdir()):
            raise ValueError('Denoised directory must be empty or contain its own manifest')
        folder.mkdir(parents=True,exist_ok=True)
        record = dict(status='running',cleanup_version='v1',signature=signature,file=str(path),reused=False)
        write_json(manifest,record)
        try:
            temporary = folder/'denoised.partial'
            record['cleanup'] = cleanup_channel(row,offset,frames,temporary,folder/'cleanup.jsonl',block_seconds,progress,workers)
            record['audio'] = verify_audio(temporary,frames,row['sample_rate'])
            record['cleanup_log_sha256'] = hashlib.sha256((folder/'cleanup.jsonl').read_bytes()).hexdigest()
            check_source(row)
            temporary.replace(path)
            record['status'] = 'complete'
            write_json(manifest,record)
        except BaseException as exc:
            record.update(status='failed',error=str(exc))
            write_json(manifest,record)
            raise
        return record
