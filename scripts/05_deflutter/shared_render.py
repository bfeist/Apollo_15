"""Stream v2 denoised audio through the saved tape map, preserving channel offsets."""
import hashlib
import json
import math
from pathlib import Path
import time

import numpy as np
import soundfile as sf

from denoise_archive import atomic_json, sha256, validate_binding
from flutter import resample_at
from shared_tape import bound, load_tape_plan, verify_bindings, METHODOLOGY


def prepared_source(row, folder):
    folder = Path(folder).resolve()
    saved = json.loads((folder/'denoised.json').read_text(encoding='utf-8'))
    path = validate_binding(folder, saved, row, 0, row['frames'], version='v2')
    if saved['action'] != 'denoise_v2':
        raise ValueError('Shared rendering requires completed new denoised audio')
    if sha256(folder/'cleanup.jsonl') != saved['cleanup_log_sha256']:
        raise ValueError('Denoise cleanup evidence changed')
    return path.resolve(), saved


def render_shared_channel(plan_path, channel, denoised, output, *, start=None, duration=None,
                          comparison=False, dry_run=False, block_seconds=5., progress=print):
    started = time.monotonic()
    plan_path, plan, data = load_tape_plan(plan_path)
    row = next((r for r in plan['inventory']['channels'] if r['channel'] == channel), None)
    if row is None:
        raise ValueError('Requested channel is absent from the tape')
    source, saved = prepared_source(row, denoised)
    start = row['start_seconds'] if start is None else start
    if not math.isfinite(start) or duration is not None and not math.isfinite(duration):
        raise ValueError('Review interval must be finite')
    offset = round((start-row['start_seconds'])*row['sample_rate'])
    start = row['start_seconds']+offset/row['sample_rate']
    frames = row['frames']-offset if duration is None else round(duration*row['sample_rate'])
    sr = row['sample_rate']
    last = start+(frames-1)/sr
    if (frames < 2 or offset < 0 or offset+frames > row['frames']
            or start < plan['start_seconds'] or last > plan['end_seconds']):
        raise ValueError('Render interval exceeds the channel or shared map scope')
    if not np.isfinite(block_seconds) or not .1 <= block_seconds <= 30:
        raise ValueError('Render block must be 0.1-30 seconds')
    if duration is None and not plan['full_tape']:
        raise ValueError('A bounded shared plan needs an explicit review duration')
    output = Path(output).resolve()
    protected = [plan_path.parent, Path(denoised).resolve(), *[Path(r['path']).parent for r in plan['inventory']['channels']]]
    if any(output == p or output in p.parents or p in output.parents for p in protected):
        raise ValueError('Output must not overlap denoised audio, originals, or tape maps')
    if output.exists():
        raise ValueError('Render destination must be new; previous audio is immutable')
    # Prove the common inverse map stays within this channel, without clamping
    # its endpoints, fitting a local map, or changing the shared anchor.
    query = np.array([start, last])
    mapped = np.interp(query, data[:, 1], data[:, 0])
    if mapped.min() < row['start_seconds']-1e-9 or mapped.max() > row['end_seconds']-1/sr+1e-9:
        raise ValueError('Shared map reaches unavailable channel samples; no channel-specific reanchoring is allowed')
    summary = dict(schema=3, status='preflight', methodology=METHODOLOGY,
        tape=plan['inventory']['tape'], channel=channel, source=row,
        start_seconds=start, duration_seconds=frames/sr, frames=frames, sample_rate=sr,
        source_offset_frames=offset, shared_map=str(plan_path.parent/plan['map']['file']),
        map_sha256=plan['map']['sha256'], plan_sha256=sha256(plan_path),
        tape_level=True, per_channel_anchoring=False, cleanup_version='v2',
        denoised_intermediate=dict(file=str(source), reused=True, algorithm=saved['algorithm']),
        input_bindings=[bound(Path(denoised)/'denoised.json'), bound(Path(denoised)/'cleanup.jsonl')],
        source_audio_sha256=saved['audio']['sha256'], gain=1.,
        gain_policy='Retain the completed v2 channel gain in both comparison files',
        pipeline=[saved['algorithm'], 'shared_tape_inverse_resampling'],
        comparison=comparison, experimental_transfer=False, listening_approved=False,
        production_accepted=False, implementation_sha256=sha256(__file__))
    if dry_run:
        return summary
    progress('Verifying completed v2 audio hash; no denoising or tone fitting')
    if sha256(source) != saved['audio']['sha256']:
        raise ValueError('Completed denoised audio hash changed')
    output.mkdir(parents=True)
    summary['status'] = 'running'
    atomic_json(output/'review.json', summary)
    name = 'B.wav' if comparison else 'deflutter.wav'
    pending = output/(name+'.partial')
    baseline = output/'A.wav.partial'
    wav_format = 'RF64' if frames*4+4096 >= 2**32 else 'WAV'
    before = source.stat()
    digests = {name: hashlib.sha256()}
    peaks = {name: 0.}
    if comparison:
        digests['A.wav'], peaks['A.wav'] = hashlib.sha256(), 0.
    try:
        from contextlib import ExitStack
        with ExitStack() as stack:
            src = stack.enter_context(sf.SoundFile(source))
            dest = stack.enter_context(sf.SoundFile(pending, 'w', samplerate=sr, channels=1,
                subtype='FLOAT', format=wav_format))
            original = stack.enter_context(sf.SoundFile(baseline, 'w', samplerate=sr, channels=1,
                subtype='FLOAT', format=wav_format)) if comparison else None
            block = round(block_seconds*sr)
            for begin in range(0, frames, block):
                stop = min(frames, begin+block)
                times = row['start_seconds']+(offset+np.arange(begin, stop))/sr
                lo = max(0, int(np.searchsorted(data[:, 1], times[0]))-1)
                hi = min(len(data), int(np.searchsorted(data[:, 1], times[-1]))+2)
                positions = (np.interp(times, data[lo:hi, 1], data[lo:hi, 0])-row['start_seconds'])*sr
                left = max(0, math.floor(float(positions[0]))-256)
                right = min(row['frames'], math.ceil(float(positions[-1]))+257)
                src.seek(left)
                samples = src.read(right-left, dtype='float64')
                if len(samples) != right-left or not np.isfinite(samples).all():
                    raise ValueError('Denoised source became unreadable or nonfinite')
                expected = offset+np.arange(begin, stop)
                if np.max(abs(positions-expected)) < 1e-7:
                    corrected = samples[offset+begin-left:offset+stop-left]
                else:
                    corrected = resample_at(samples, positions-left)
                corrected = corrected.astype('float32')
                if len(corrected) != stop-begin or not np.isfinite(corrected).all():
                    raise ValueError('Invalid shared-map output samples')
                dest.write(corrected)
                digests[name].update(corrected.tobytes())
                peaks[name] = max(peaks[name], float(np.max(abs(corrected))))
                if original:
                    src.seek(offset+begin)
                    plain = src.read(stop-begin, dtype='float32')
                    if len(plain) != stop-begin or not np.isfinite(plain).all():
                        raise ValueError('Invalid comparison source samples')
                    original.write(plain)
                    digests['A.wav'].update(plain.tobytes())
                    peaks['A.wav'] = max(peaks['A.wav'], float(np.max(abs(plain))))
                if begin//block % 120 == 0 or stop == frames:
                    progress(f'CH{channel:02d}: {stop/frames:.1%}')
        after = source.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise ValueError('Completed denoised audio changed during rendering')
        verify_bindings(summary['input_bindings'])
        if sha256(plan_path.parent/plan['map']['file']) != plan['map']['sha256']:
            raise ValueError('Shared tape map changed during rendering')
        for file, temporary in [(name, pending), *([('A.wav', baseline)] if comparison else [])]:
            info = sf.info(temporary)
            if (info.frames, info.samplerate, info.channels, info.subtype) != (frames, sr, 1, 'FLOAT'):
                raise ValueError('Output WAV header failed verification')
            verified = hashlib.sha256()
            with sf.SoundFile(temporary) as stream:
                for samples in stream.blocks(blocksize=sr*30, dtype='float32'):
                    if not np.isfinite(samples).all():
                        raise ValueError('Written WAV has nonfinite samples')
                    verified.update(samples.tobytes())
            if verified.hexdigest() != digests[file].hexdigest():
                raise ValueError('Written WAV differs from rendered samples')
        pending.replace(output/name)
        if comparison:
            baseline.replace(output/'A.wav')
        audio = {file: dict(frames=frames, sample_rate=sr, peak=peaks[file],
                 decoded_float32_sha256=h.hexdigest(), sha256=sha256(output/file)) for file, h in digests.items()}
        summary.update(status='complete', elapsed_seconds=time.monotonic()-started,
            audio=dict(deflutter=audio[name], **({'cleaned': audio['A.wav']} if comparison else {})),
            files=audio)
        atomic_json(output/'review.json', summary)
    except BaseException as error:
        summary.update(status='failed', error=str(error))
        atomic_json(output/'review.json', summary)
        raise
    return summary
