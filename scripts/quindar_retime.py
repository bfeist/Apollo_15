"""Quindar-anchored slow-speed curve and streamed post-deflutter retiming."""
import hashlib
import csv
import html
import json
import time
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy import signal
from scipy.interpolate import PchipInterpolator

from quindar_speed import fit_events, scan
from flutter import resample_at
from flutter_plan_store import planning_lock
from normalization import normalize_pending_file, validate_settings, verify_peak

DEFAULT_RETIMED_ROOT = Path('D:/Apollo_15_30-Track/defluttered_retimed_v2')


def smooth_at(times, ratios, queries, bandwidth):
    """Robust local linear regression; sparse neighbourhoods expand to 12 tones.

    Bandwidth is a Gaussian standard deviation in seconds, not anchor spacing.
    Every accepted event retains its own anchor location. Local residuals, not
    raw single-beep centres, determine downweighting for residual flutter.
    """
    times, ratios, queries = map(np.asarray, (times, ratios, queries))
    predictions, supports = [], []
    for q in queries:
        distance = abs(times-q)
        width = max(float(bandwidth), float(np.partition(distance, min(11, len(times)-1))[min(11, len(times)-1)])/2)
        ids = distance <= 3*width
        u = (times[ids]-q)/width
        y = ratios[ids]
        design = np.column_stack([np.ones(len(u)), u])
        base = np.exp(-.5*u*u)
        w = base.copy()
        for _ in range(12):
            coef = np.linalg.lstsq(design*np.sqrt(w[:, None]), y*np.sqrt(w), rcond=None)[0]
            residual = y-design@coef
            scale = max(.0005, 1.4826*np.median(abs(residual-np.median(residual))))
            w = base*np.minimum(1., 1.5*scale/np.maximum(abs(residual), 1e-15))
        predictions.append(float(coef[0]))
        supports.append(dict(kernel_sigma_seconds=width, neighbours=int(ids.sum()),
                             effective_neighbours=float(w.sum()**2/(w@w))))
    return np.array(predictions), supports


def curve_model(events, duration, bandwidth=None):
    """Select smoothing with held-out 10-minute blocks, then anchor every beep."""
    fitted = fit_events(events)
    selected = sorted(fitted['events'], key=lambda e: e['start_seconds'])
    times = np.array([(e['start_seconds']+e['end_seconds'])/2 for e in selected])
    ratios = np.array([e['recorded_speed_ratio'] for e in selected])
    nominal = np.array([e['nominal_hz'] for e in selected])
    if not np.isfinite(duration) or duration <= times[-1] or times[0] <= 0 or np.any(np.diff(times) <= 0):
        raise ValueError('Invalid duration or duplicate/out-of-range Quindar times')
    candidates = [float(bandwidth)] if bandwidth is not None else [900., 1800., 3600., 7200.]
    if any(not np.isfinite(h) or h < 300 for h in candidates):
        raise ValueError('Smoothing bandwidth must be at least 300 seconds')
    blocks = (times//600).astype(int)
    validation = []
    for width in candidates:
        errors, folds = [], []
        for parity in (0, 1):
            train = blocks % 2 == parity
            test = ~train
            # Fit training anchors only; reproduce the deployed interpolation and
            # endpoint hold when predicting test events (no held-out anchors).
            pred, _ = smooth_at(times[train], ratios[train], times[train], width)
            predictor = PchipInterpolator(times[train], pred)
            guessed = predictor(np.clip(times[test], times[train].min(), times[train].max()))
            error = (ratios[test]-guessed)*nominal[test]
            errors.extend(error.tolist())
            folds.append(float(np.sqrt(np.mean(error**2))))
        validation.append(dict(kernel_sigma_seconds=width, rmse_hz=float(np.sqrt(np.mean(np.square(errors)))),
                               fold_rmse_hz=folds))
    # Prefer the smoother model among essentially tied validation scores.
    best = min(x['rmse_hz'] for x in validation)
    winner = max((v for v in validation if v['rmse_hz'] <= best*1.02), key=lambda v: v['kernel_sigma_seconds'])
    values, supports = smooth_at(times, ratios, times, winner['kernel_sigma_seconds'])
    if not np.isfinite(values).all() or np.min(values) < .94 or np.max(values) > 1.06:
        raise ValueError('Quindar speed curve exceeds supported +/-6% bounds')
    anchors = []
    for e, t, value, support in zip(selected, times, values, supports):
        anchors.append(dict(input_seconds=float(t), kind=e['kind'], nominal_hz=e['nominal_hz'],
                            measured_hz=e['frequency_hz'], measured_speed_ratio=e['recorded_speed_ratio'],
                            smoothed_speed_ratio=float(value), playback_multiplier=float(1/value), **support))
    knots = np.r_[0., times, duration]
    speed = np.r_[values[0], values, values[-1]]
    curve = PchipInterpolator(knots, speed)
    integral = curve.antiderivative()
    for e in anchors:
        e['output_seconds'] = float(integral(e['input_seconds'])-integral(0))
    return dict(schema=1, method='robust_local_linear_quindar_anchors_pchip',
                kernel_sigma_seconds=winner['kernel_sigma_seconds'], bandwidth_selection='smoothest within 2% of best held-out-block RMSE',
                validation=validation, chosen_validation=winner,
                linear_comparison=fitted['validation'], constant_speed_ratio=fitted['constant_speed_ratio'],
                linear_coefficients=fitted['linear_coefficients'],
                anchors=anchors, input_seconds=knots.tolist(), recorded_speed_ratio=speed.tolist(),
                input_duration_seconds=duration, output_duration_seconds=float(integral(duration)-integral(0)),
                support=fitted['support'], edge_policy='Hold nearest smoothed anchor speed before first and after last tone',
                gap_policy='PCHIP interpolation between smoothed anchors; gaps remain modeled, not measured',
                interpretation='Speed anchors are smoothed measurements, not exact fits to individual flutter-contaminated beeps; output time is the integral of recorded-speed ratio')


def timing_map(model, step=.1):
    times = np.asarray(model['input_seconds'])
    values = np.asarray(model['recorded_speed_ratio'])
    if (len(times) < 2 or times.shape != values.shape or not np.isfinite(times).all()
            or not np.isfinite(values).all() or times[0] != 0 or np.any(np.diff(times) <= 0)
            or min(values) < .94 or max(values) > 1.06 or not np.isfinite(step) or step <= 0):
        raise ValueError('Invalid monotone timing curve')
    curve = PchipInterpolator(times, values)
    integral = curve.antiderivative()
    grid = np.unique(np.r_[np.arange(0., times[-1], step), times])
    output = integral(grid)-integral(0)
    if np.any(np.diff(output) <= 0):
        raise ValueError('Nonmonotone output timing map')
    return grid, output, curve(grid)


def audio_signature(path):
    info = sf.info(path)
    stat = path.stat()
    return dict(path=str(path.resolve()), bytes=stat.st_size, mtime_ns=stat.st_mtime_ns,
                frames=info.frames, sample_rate=info.samplerate, channels=info.channels)


def verify_audio(path):
    digest = hashlib.sha256()
    peak, frames = 0., 0
    with sf.SoundFile(path) as f:
        sr = f.samplerate
        for x in f.blocks(blocksize=sr*30, dtype='float32'):
            if not np.isfinite(x).all():
                raise ValueError('Non-finite audio')
            peak = max(peak, float(np.max(abs(x))))
            frames += len(x)
            digest.update(x.tobytes())
    return dict(frames=frames, sample_rate=sr, peak=peak, decoded_float32_sha256=digest.hexdigest())


def write_json(path, value):
    temporary = path.with_suffix(path.suffix+'.partial')
    temporary.write_text(json.dumps(value, indent=2, allow_nan=False), encoding='utf-8')
    temporary.replace(path)


def render_stream(source, output, input_times, output_times, speed, *, block_seconds=5., progress=print):
    """Every output block uses one global inverse map; no local re-anchoring.

    512-sample input halos cover the antialias FIR and oversampling support.
    The antialias filter is fixed for the whole file, including at block joins.
    """
    if not np.isfinite(block_seconds) or block_seconds <= 0:
        raise ValueError('Block size must be positive')
    if (len(input_times) != len(output_times) or len(speed) != len(input_times)
            or np.any(np.diff(input_times) <= 0) or np.any(np.diff(output_times) <= 0)
            or not np.isfinite(output_times).all()):
        raise ValueError('Invalid inverse map')
    cutoff = min(1., float(min(speed)))*.97
    kernel = signal.firwin(255, cutoff, window=('kaiser', 8.6))
    started = last_progress = time.monotonic()
    minimum_step, maximum_step, previous_last = float('inf'), 0., None
    with sf.SoundFile(source) as f:
        sr, n = f.samplerate, len(f)
        if f.channels != 1 or abs(input_times[-1]-n/sr) > .5/sr:
            raise ValueError('Map and mono source duration disagree')
        count = round(output_times[-1]*sr)
        block = max(1, round(block_seconds*sr))
        fmt = 'RF64' if count*4 > 4_000_000_000 else 'WAV'
        with sf.SoundFile(output, 'w', samplerate=sr, channels=1, subtype='FLOAT', format=fmt) as out:
            for start in range(0, count, block):
                times = np.arange(start, min(count, start+block))/sr
                positions = np.interp(times, output_times, input_times)*sr
                positions = np.clip(positions, 0., n-1.)
                if previous_last is not None:
                    minimum_step = min(minimum_step, float(positions[0]-previous_last))
                    maximum_step = max(maximum_step, float(positions[0]-previous_last))
                if len(positions) > 1:
                    minimum_step = min(minimum_step, float(np.min(np.diff(positions))))
                    maximum_step = max(maximum_step, float(np.max(np.diff(positions))))
                previous_last = positions[-1]
                left = max(0, int(np.floor(positions[0]))-512)
                right = min(n, int(np.ceil(positions[-1]))+513)
                f.seek(left)
                x = f.read(right-left)
                if not np.isfinite(x).all():
                    raise ValueError('Non-finite source audio')
                filtered = signal.fftconvolve(x, kernel, mode='same')
                y = resample_at(filtered, positions-left)
                out.write(y)
                now = time.monotonic()
                if now-last_progress >= 20 or start+block >= count:
                    progress(f'Retime {100*min(start+block,count)/count:.1f}% ({now-started:.1f}s)')
                    last_progress = now
    return dict(frames=count, sample_rate=sr, minimum_input_sample_step=minimum_step,
                maximum_input_sample_step=maximum_step, block_seconds=block_seconds,
                antialias_cutoff_fraction_of_nyquist=cutoff,
                resampler='255-tap Kaiser antialias; 8x FIR/cubic; 512-sample halo; global inverse map')


def retime(source, destination, candidates_path=None, bandwidth=None, target_dbfs=-3., max_boost_db=12., progress=print, *, shared_plan=None):
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if source.is_dir():
        if (source/'deflutter.wav').is_file():
            source = source/'deflutter.wav'
        else:
            wavs = sorted(source.glob('*.wav'))
            if len(wavs) != 1:
                raise ValueError('Select one defluttered WAV; automatic independent multi-channel retiming is not supported')
            source = wavs[0]
    if not source.is_file() or source.parent == destination or source.parent in destination.parents or destination in source.parents:
        raise ValueError('Input and output must be separate non-overlapping locations')
    if shared_plan is None and 'irig' in source.name.lower():
        raise ValueError('Quindar calibration requires a voice channel, not IRIG')
    validate_settings(target_dbfs, max_boost_db)
    signature = audio_signature(source)
    if signature['channels'] != 1 or signature['frames'] == 0:
        raise ValueError('Retiming requires nonempty mono defluttered audio')
    settings = dict(kernel_sigma_seconds=bandwidth, target_dbfs=target_dbfs, max_boost_db=max_boost_db,
                    map_step_seconds=.1, method='robust_local_linear_quindar_anchors_pchip')
    shared = None
    if shared_plan is not None:
        if candidates_path is not None or bandwidth is not None:
            raise ValueError('A frozen tape plan cannot be combined with per-channel candidates or smoothing overrides')
        from quindar_tape import target_map
        shared = target_map(shared_plan, source)
        settings.update(method='frozen_shared_tape_quindar_curve', shared_plan=shared[4])
    with planning_lock(destination):
        if destination.exists() and any(destination.iterdir()):
            manifest = destination/'retime.json'
            if not manifest.is_file():
                raise ValueError('Output must be empty or a matching completed retiming run')
            old = json.loads(manifest.read_text())
            if old.get('status') != 'complete' or old['source'] != signature or old['settings'] != settings:
                raise ValueError('Existing retiming output is incomplete or input/settings changed; use a new output folder')
            if candidates_path is not None and hashlib.sha256(Path(candidates_path).read_bytes()).hexdigest() != old['candidates_file_sha256']:
                raise ValueError('Candidate inventory differs from completed retiming run')
            for filename, digest in old['artifacts'].items():
                if hashlib.sha256((destination/filename).read_bytes()).hexdigest() != digest:
                    raise ValueError(f'Retiming artifact changed: {filename}')
            if verify_audio(destination/'retimed.wav') != old['audio'] or verify_audio(source)['decoded_float32_sha256'] != old['input_audio']['decoded_float32_sha256']:
                raise ValueError('Retimed audio or source content changed')
            progress('Reusing verified retimed output')
            return old
        duration = signature['frames']/signature['sample_rate']
        inventory = None
        if candidates_path is not None:
            inventory = json.loads(Path(candidates_path).read_text())
            if (Path(inventory['source']).resolve() != source or inventory['bytes'] != signature['bytes']
                    or inventory['mtime_ns'] != signature['mtime_ns'] or inventory['frames'] != signature['frames']
                    or inventory['sample_rate'] != signature['sample_rate'] or inventory['scan_start'] != 0
                    or abs(inventory['scan_end']-duration) > .5/signature['sample_rate']):
                raise ValueError('Candidate inventory must cover this exact complete defluttered input')
        destination.mkdir(parents=True, exist_ok=True)
        record = dict(schema=1, status='running', stage='05c', source=signature, settings=settings,
                      input_stage='defluttered', source_start_anchor_preserved=True)
        write_json(destination/'retime.json', record)
        try:
            if shared is not None:
                model, tt, nominal, speed, shared_metadata = shared
                record.update(shared_plan=shared_metadata, curve_time_basis='global tape clock',
                              timing_map_time_basis='local file clock; add shared_plan.output_start_seconds for tape placement')
            elif inventory is None:
                progress('Scanning the complete defluttered input for Quindar tones')
                inventory = dict(source=str(source), bytes=signature['bytes'], mtime_ns=signature['mtime_ns'],
                                 frames=signature['frames'], sample_rate=signature['sample_rate'], duration_seconds=duration,
                                 scan_start=0., scan_end=duration, events=scan(source))
            if shared is None:
                if candidates_path is not None:
                    (destination/'candidates.json').write_bytes(Path(candidates_path).read_bytes())
                else:
                    write_json(destination/'candidates.json', inventory)
                record['candidates_file_sha256'] = hashlib.sha256((destination/'candidates.json').read_bytes()).hexdigest()
                model = curve_model(inventory['events'], duration, bandwidth)
                tt, nominal, speed = timing_map(model)
            write_json(destination/'curve.json', model)
            np.savez_compressed(destination/'timing_map.npz', input_seconds=tt, output_seconds=nominal, recorded_speed_ratio=speed)
            with (destination/'anchors.csv').open('w', newline='', encoding='utf-8') as f:
                writer = csv.DictWriter(f, fieldnames=list(model['anchors'][0]))
                writer.writeheader()
                writer.writerows(model['anchors'])
            record.update(input_audio=verify_audio(source), anchor_count=len(model['anchors']),
                          curve_duration_seconds=model['output_duration_seconds'],
                          curve_validation=model['chosen_validation'], coverage=model['support'],
                          playback_multiplier_range=[float(1/max(speed)), float(1/min(speed))],
                          implementation_sha256=hashlib.sha256(Path(__file__).read_bytes()+Path(__file__).with_name('quindar_speed.py').read_bytes()).hexdigest())
            progress(f'{len(model["anchors"])} Quindar anchors; {model["kernel_sigma_seconds"]/60:g}-minute smoothing; output {nominal[-1]:.3f}s')
            temporary = destination/'retimed.partial.wav'
            record['render'] = render_stream(source, temporary, tt, nominal, speed, progress=progress)
            normalization = normalize_pending_file(temporary, target_dbfs, max_boost_db, progress)
            audio = verify_audio(temporary)
            verify_peak(audio['peak'], normalization)
            if audio['frames'] != round(nominal[-1]*signature['sample_rate']):
                raise ValueError('Rendered frame count disagrees with timing map')
            if audio_signature(source) != signature:
                raise ValueError('Input changed during retiming')
            record.update(audio=audio, normalization=normalization,
                          output_duration_seconds=audio['frames']/audio['sample_rate'],
                          seconds_removed=duration-audio['frames']/audio['sample_rate'])
            # Markers are input-file-relative, as emitted by stage 05b.
            marker_file = source.parent/'markers.csv'
            if marker_file.is_file():
                with marker_file.open(newline='', encoding='utf-8') as f:
                    reader = csv.DictReader(f)
                    fields = reader.fieldnames
                    rows = list(reader)
                if not {'start_seconds', 'end_seconds'} <= set(fields or []):
                    raise ValueError('Source markers need start_seconds and end_seconds')
                for row in rows:
                    for key in ('start_seconds', 'end_seconds'):
                        value = float(row[key])
                        if not 0 <= value <= duration:
                            raise ValueError('Source marker outside input timeline')
                        row['input_'+key] = value
                        row[key] = float(np.interp(value, tt, nominal))
                with (destination/'markers.csv').open('w', newline='', encoding='utf-8') as f:
                    writer = csv.DictWriter(f, fieldnames=fields+['input_start_seconds', 'input_end_seconds'])
                    writer.writeheader()
                    writer.writerows(rows)
            temporary.replace(destination/'retimed.wav')
            record['artifacts'] = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in destination.iterdir()
                                   if p.name in {'curve.json', 'timing_map.npz', 'anchors.csv', 'markers.csv', 'candidates.json'}}
            record['status'] = 'complete'
            write_json(destination/'retime.json', record)
            page = f'''<!doctype html><html lang="en"><meta charset="utf-8"><title>05c Quindar retiming</title>
<style>body{{font:18px/1.5 system-ui;max-width:900px;margin:40px auto;padding:20px}}a{{color:#165d98}}</style>
<h1>05c · Quindar-anchored retiming</h1><p>Input: {html.escape(str(source))}</p>
<p>{record['anchor_count']} smoothed Quindar anchors. Playback speed multiplier: {min(record['playback_multiplier_range']):.6f}–{max(record['playback_multiplier_range']):.6f}.
Duration: {duration:.3f} → {record['output_duration_seconds']:.3f} seconds. Removed {record['seconds_removed']:.3f} seconds.</p>
<p><a href="retimed.wav">Open retimed audio</a> · <a href="curve.json">Speed curve</a> · <a href="anchors.csv">Anchors</a> · <a href="timing_map.npz">Timing map</a> · <a href="retime.json">Provenance and verification</a></p>
<p>Retiming follows a smooth curve through locally averaged tone measurements. It preserves the input start, changes duration, and holds the nearest anchor speed beyond tone coverage. Nominal Quindar accuracy remains a systematic uncertainty; sparse spans are modeled. The input is unchanged. Use an audio editor for large WAV/RF64 files.</p></html>'''
            (destination/'index.html').write_text(page, encoding='utf-8')
            progress(f'Completed: {destination/"retimed.wav"}')
            return record
        except Exception as error:
            record.update(status='failed', error=str(error))
            write_json(destination/'retime.json', record)
            raise
