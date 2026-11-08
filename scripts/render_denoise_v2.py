"""Render a matched original/v2 listening pair without touching stage-03 caches."""
import argparse
import hashlib
import json
from pathlib import Path
import uuid

import numpy as np
import scipy
import soundfile as sf

from denoise_v2 import ALGORITHM, SETTINGS, configuration, corrected_chunks, line_evidence, rms_db


def sha256(path):
    with path.open('rb') as stream:
        digest = hashlib.sha256()
        for block in iter(lambda: stream.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


def saved_seeds(path, start, end):
    if path is None:
        return ()
    rows = []
    with path.open(encoding='utf-8') as stream:
        for line in stream:
            row = json.loads(line)
            a, b = row['start_frame'], row['end_frame']
            if b <= start or a >= end:
                continue
            passes = row['tones']['passes']
            lines = passes[0]['lines'] if passes else []
            hints = [v['hz'] for v in lines if v['occupancy'] > .8 and v['hz'] >= 45
                     and abs(v['hz']-60*round(v['hz']/60)) > 1.5]
            rows.append((a, b, hints))
    return rows


def gather_seeds(rows, start, end):
    found = []
    for a, b, hints in rows:
        if b <= start or a >= end:
            continue
        for hz in hints:
            if all(abs(hz-other) >= 2 for other in found):
                found.append(hz)
    return found


def measure(raw, result, sr):
    freq, before, _, _, _ = line_evidence(raw, sr)
    _, after, _, _, _ = line_evidence(result, sr)
    grid = (abs(freq-60*np.round(freq/60)) <= 3) & (freq >= 40) & (freq < 3950)
    speech = (freq >= 350) & (freq < 2500)
    off = (~grid) & (freq >= 300) & (freq < 3000)
    activity = before[speech].sum(axis=0)
    quiet = activity <= np.percentile(activity, 25)
    return np.array([p[band][:, quiet].sum() for p in (before, after)
                     for band in (grid, off)])


def metrics_dict(values):
    return dict(quiet_grid_original_db=rms_db(values[0]),
                quiet_grid_change_db=rms_db(values[2])-rms_db(values[0]),
                quiet_offgrid_change_db=rms_db(values[3])-rms_db(values[1]))


def render(source, output, start, duration, gain=1.0, offset_frames=0, seeds_path=None):
    source, output = source.resolve(), output.resolve()
    if any(part.lower() in {'denoised', 'denoised_v1', 'denoised_v2', '.denoise_v2_rollout'} for part in source.parts):
        raise ValueError(f'Use original audio for v2 subtraction: {source}')
    if output.exists() or any(part.lower() in {'denoised', 'denoised_v1', '.denoise_v2_rollout', 'orig_concatenated'}
                              for part in output.parts):
        raise FileExistsError(f'Use a new v2 destination outside source and v1 trees: {output}')
    if not np.isfinite(gain) or gain <= 0:
        raise ValueError('Gain must be finite and positive')
    if not np.isfinite(start) or start < 0 or not np.isfinite(duration) or duration <= 0:
        raise ValueError('Expected a nonnegative start and positive duration')
    implementation = Path(__file__).with_name('denoise_v2.py')
    refinement = Path(__file__).with_name('denoise_v2_refinement.py')
    inputs = [implementation, refinement, Path(__file__)]
    if seeds_path is not None:
        seeds_path = seeds_path.resolve()
        inputs.append(seeds_path)
    initial_hashes = {path: sha256(path) for path in inputs}
    stage = output.parent / ('.'+output.name+'.'+uuid.uuid4().hex+'.work')
    stage.mkdir(parents=True)
    raw_path, v2_path = (stage/name for name in ('01_original.wav', '02_denoised_v2.wav'))
    scratch = stage/'corrected.float64.wav'
    peaks = [0., 0.]
    nodes = {}
    totals = np.zeros(4)
    lengths_measured = 0
    joins = []
    previous = None
    source_stat = source.stat()
    input_hash = hashlib.sha256()
    with sf.SoundFile(source) as original:
        sr = original.samplerate
        if original.channels != 1 or sr != SETTINGS.sample_rate:
            raise ValueError('Expected original mono 8 kHz audio')
        first = round(start*sr)+offset_frames
        end = first+round(duration*sr)
        source_frames = original.frames

        def read(frame, count):
            original.seek(frame)
            return original.read(count, dtype='float64')

        context = (2*SETTINGS.step_seconds + SETTINGS.filter_guard_seconds)*sr
        seed_rows = saved_seeds(seeds_path, first-context, end+context)
        with sf.SoundFile(scratch, mode='x', samplerate=sr, channels=1,
                          subtype='DOUBLE', format='RF64') as working:
            for frame, raw, corrected, left, right in corrected_chunks(
                    read, source_frames, first, end,
                    lambda a, b: gather_seeds(seed_rows, a, b)):
                if not np.isfinite(corrected).all():
                    raise ValueError('Non-finite correction')
                nodes[left['anchor_frame']] = left
                nodes[right['anchor_frame']] = right
                peaks = [max(peak, float(np.max(abs(x)))) for peak, x in zip(peaks, (raw, corrected))]
                input_hash.update(raw.astype('<f8', copy=False))
                if len(raw) >= 2*sr:
                    totals += measure(raw, corrected, sr)
                    lengths_measured += len(raw)
                delta = raw-corrected
                if previous is not None:
                    joins.append(dict(frame=frame, correction_step=float(abs(delta[0]-previous))))
                previous = delta[-1]
                working.write(corrected)
                print(f'{output.name}: {(frame+len(raw)-first)/sr:g}/{duration:g}s', flush=True)

        common_gain = min(gain, .999/max(max(peaks), 1e-30))
        outputs = {}
        original.seek(first)
        with sf.SoundFile(scratch) as corrected:
            for path, stream in ((raw_path, original), (v2_path, corrected)):
                with sf.SoundFile(path, mode='x', samplerate=sr, channels=1,
                                  subtype='PCM_24', format='WAV') as result:
                    remaining = end-first
                    while remaining:
                        block = stream.read(min(remaining, 65536), dtype='float64')
                        if not len(block):
                            raise ValueError('Unexpected source EOF')
                        result.write(block*common_gain)
                        remaining -= len(block)
                info = sf.info(path)
                if (info.samplerate, info.frames, info.channels, info.subtype) != (sr, end-first, 1, 'PCM_24'):
                    raise ValueError('Invalid listening file')
                outputs[path.name] = dict(sha256=sha256(path), frames=info.frames,
                                          sample_rate=sr, subtype=info.subtype)
    if source.stat().st_mtime_ns != source_stat.st_mtime_ns or source.stat().st_size != source_stat.st_size:
        raise RuntimeError('Original changed during rendering')
    if any(sha256(path) != digest for path, digest in initial_hashes.items()):
        raise RuntimeError('Implementation or cleanup hints changed during rendering')
    scratch.unlink()
    manifest = dict(algorithm=ALGORITHM, settings=configuration(),
                    implementation_sha256=initial_hashes[implementation],
                    runner_sha256=initial_hashes[Path(__file__)],
                    refinement_sha256=initial_hashes[refinement],
                    versions=dict(numpy=np.__version__, scipy=scipy.__version__, soundfile=sf.__version__),
                    source=dict(path=str(source), frames=source_frames, bytes=source_stat.st_size,
                                mtime_ns=source_stat.st_mtime_ns,
                                excerpt_float64_sha256=input_hash.hexdigest()),
                    canonical_seconds=[start, start+(end-first)/sr], source_offset_frames=offset_frames,
                    source_frames=[first, end], common_gain=common_gain, requested_gain=gain,
                    peaks_at_common_gain=[peak*common_gain for peak in peaks],
                    cleanup_log=str(seeds_path) if seeds_path else None,
                    cleanup_log_sha256=initial_hashes.get(seeds_path),
                    no_broadband_or_time_processing=True, files=outputs,
                    metrics=metrics_dict(totals) if lengths_measured else None,
                    metrics_frames=lengths_measured, model_nodes=list(nodes.values()), joins=joins)
    (stage/'manifest.json').write_text(json.dumps(manifest, indent=2, allow_nan=False)+'\n', encoding='utf-8')
    # Windows rename refuses an existing destination. Keep a failed stage for
    # diagnosis and never replace an existing listening pair.
    if output.exists():
        raise FileExistsError(output)
    stage.rename(output)
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('output', type=Path, help='New v2 directory')
    parser.add_argument('start', type=float, help='Canonical start, seconds')
    parser.add_argument('duration', type=float, help='Duration, seconds')
    parser.add_argument('--gain', type=float, default=1.0, help='Common fixed gain for both files')
    parser.add_argument('--offset-frames', type=int, default=0, help='Canonical-to-source frame offset')
    parser.add_argument('--cleanup-log', type=Path, help='Optional saved v1 frequency hints, read-only')
    args = parser.parse_args()
    render(args.source, args.output, args.start, args.duration, args.gain,
           args.offset_frames, args.cleanup_log)


if __name__ == '__main__':
    main()

