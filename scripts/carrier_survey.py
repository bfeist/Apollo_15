"""Streaming original-audio carrier screening, independent of IRIG decoding.

Two-second windows at one-second hops search the entire source. A candidate
is a generic tone until independently decoded. Cache files retain original
sample coordinates, settings, source identity and bounded-search negatives.
"""
import argparse
import hashlib
import json
from pathlib import Path
import time

import numpy as np
from scipy import signal
import soundfile as sf

from flutter_plan_store import planning_lock
from flutter_policy import DEFAULT_SURVEY_ROOT, digest
from io_metrics import read_bytes as os_read_bytes, difference as io_difference

SETTINGS = dict(version=2, acquisition_hz=[700., 1300.], window_seconds=2.,
                hop_seconds=1., minimum_detectable_island_seconds=2.,
                minimum_contrast_db=12., minimum_rms=1e-6,
                maximum_clipped_fraction=.001, context_seconds=4.)


def write_json(path, value):
    path = Path(path)
    temporary = path.with_suffix('.json.partial')
    temporary.write_text(json.dumps(value, indent=2, allow_nan=False), encoding='utf-8')
    temporary.replace(path)


def source_record(path):
    path = Path(path).resolve()
    info, stat = sf.info(path), path.stat()
    if info.channels != 1 or info.samplerate < 4000:
        raise ValueError(f'Survey needs mono original audio at >=4 kHz: {path}')
    return dict(path=str(path), bytes=stat.st_size, mtime_ns=stat.st_mtime_ns,
                frames=info.frames, sample_rate=info.samplerate)


def screen(x, sr):
    if not np.isfinite(x).all():
        raise ValueError('Non-finite original audio')
    rms = float(np.sqrt(np.mean(x*x)))
    clipped = float(np.mean(abs(x) >= .999))
    f, power = signal.welch(x, sr, nperseg=min(len(x), sr//2), noverlap=sr//4)
    ids = np.flatnonzero((f >= SETTINGS['acquisition_hz'][0]) & (f <= SETTINGS['acquisition_hz'][1]))
    k = ids[np.argmax(power[ids])]
    side = (abs(f-f[k]) > 50) & (abs(f-f[k]) < 180)
    contrast = float(10*np.log10((power[k]+1e-30)/(np.median(power[side])+1e-30)))
    # Broad FM or AM may have multiple peaks; record ambiguity instead of
    # interpreting the spectral maximum as speed or historical clock time.
    peaks, _ = signal.find_peaks(power[ids], distance=20)
    competitors = [float(f[ids[i]]) for i in peaks if power[ids[i]] > power[k]*.5 and abs(f[ids[i]]-f[k]) > 60]
    candidate = rms >= SETTINGS['minimum_rms'] and contrast >= SETTINGS['minimum_contrast_db']
    return dict(carrier_hz=float(f[k]), contrast_db=contrast, rms=rms,
                clipped_fraction=clipped, competing_peaks_hz=competitors,
                status='carrier_candidate' if candidate else 'screened_no_candidate')


def scan_file(path, cache, max_windows=None):
    """Checkpoint per block; source/settings changes reject reuse, never relabel it."""
    source, cache = source_record(path), Path(cache)
    identity = digest(__file__)
    sr, n = source['sample_rate'], source['frames']
    window, hop = round(SETTINGS['window_seconds']*sr), round(SETTINGS['hop_seconds']*sr)
    if max_windows is not None and max_windows < 1:
        raise ValueError('max_windows must be positive')
    if cache.exists():
        result = json.loads(cache.read_text(encoding='utf-8'))
        if (result['source'] != source or result['settings'] != SETTINGS or result['implementation_sha256'] != identity):
            raise ValueError('Carrier cache source/settings/implementation changed; choose a new survey destination')
        if result['complete']:
            for block in result['feature_blocks']:
                feature = (cache.parent/block['file']).resolve()
                if cache.parent.resolve() not in feature.parents or digest(feature) != block['sha256']:
                    raise ValueError('Carrier feature cache changed')
            return result
    else:
        result = dict(source=source, settings=SETTINGS, implementation_sha256=identity,
                      complete=False, next_sample=0, window_count=0, feature_blocks=[], islands=[],
                      carrier_status='not_scanned', clock_status='not_scanned',
                      performance=dict(decoded_read_bytes=0, os_read_bytes=0, wall_seconds=0., cpu_seconds=0.))
    cache.parent.mkdir(parents=True, exist_ok=True)
    wall, cpu, read_bytes, processed = time.perf_counter(), time.process_time(), 0, 0
    io_start = os_read_bytes()
    # Read 60 s blocks plus the overlap. RAM use is independent of tape length.
    with sf.SoundFile(path) as stream:
        while result['next_sample'] < n:
            block_start = result['next_sample']
            windows = []
            stream.seek(block_start)
            audio = stream.read(min(n-block_start, 60*sr+window), dtype='float64')
            read_bytes += audio.nbytes
            while result['next_sample'] < min(n, block_start+60*sr):
                a = result['next_sample']
                b = min(n, a+window)
                x = audio[a-block_start:b-block_start]
                if len(x) < sr//2:
                    feature = dict(status='screened_no_candidate', reason='Tail shorter than minimum detectable island')
                else:
                    feature = screen(x, sr)
                windows.append(dict(start_sample=a, end_sample=b, **feature))
                result['next_sample'] = min(n, a+hop)
                processed += 1
                if max_windows is not None and processed >= max_windows:
                    break
            if source_record(path) != source:
                raise ValueError('Original changed during carrier survey')
            result['complete'] = result['next_sample'] >= n
            # Merge overlapping detections only; a negative hop breaks support.
            islands = result['islands']
            previous_candidate = result.get('last_window_candidate', False)
            for row in windows:
                if (row['status'] != 'carrier_candidate' or row.get('clipped_fraction', 1.) > SETTINGS['maximum_clipped_fraction']
                        or row.get('competing_peaks_hz')):
                    previous_candidate = False
                    continue
                if (previous_candidate and islands and row['start_sample'] <= islands[-1]['end_sample']
                        and abs(row['carrier_hz']-islands[-1]['carrier_hz']) < 80):
                    islands[-1]['end_sample'] = row['end_sample']
                else:
                    islands.append(dict(start_sample=row['start_sample'], end_sample=row['end_sample'],
                                        carrier_hz=row['carrier_hz'], status='carrier_candidate',
                                        contrast_db=row['contrast_db'],
                                        identity='generic_reference', clock_status='not_scanned'))
                previous_candidate = True
            result['islands'] = islands
            result['last_window_candidate'] = previous_candidate
            features = cache.with_name(f'{cache.stem}_features_{block_start:012d}.json')
            write_json(features, windows)
            result['feature_blocks'].append(dict(file=features.name, sha256=digest(features)))
            result['window_count'] += len(windows)
            result['carrier_status'] = 'carrier_candidate' if islands else ('screened_no_candidate' if result['complete'] else 'not_scanned')
            result['searched_samples'] = [0, min(n, result['next_sample']+window-hop)]
            result['negative_interpretation'] = 'No candidate above declared sensitivity within searched bandwidth/duration; not proof of absence'
            performance = result['performance'].copy()
            result['performance'] = dict(decoded_read_bytes=performance['decoded_read_bytes']+read_bytes,
                os_read_bytes=performance.get('os_read_bytes', 0)+(io_difference(io_start) or 0),
                io_measurement='Process OS read-transfer bytes, including cache hits; not physical disk traffic',
                wall_seconds=performance['wall_seconds']+time.perf_counter()-wall,
                cpu_seconds=performance['cpu_seconds']+time.process_time()-cpu)
            write_json(cache, result)
            wall, cpu, read_bytes = time.perf_counter(), time.process_time(), 0
            io_start = os_read_bytes()
            if max_windows is not None and processed >= max_windows:
                break
    return result


def survey_tape(source, output, max_windows=None):
    source, output = Path(source).resolve(), Path(output).resolve()
    if source == output or source in output.parents or output in source.parents:
        raise ValueError('Survey destination must be separate from originals')
    paths = sorted((p for p in source.rglob('*') if p.suffix.lower() == '.wav'),
                   key=lambda p: (not ('IRIG' in p.name.upper() or '_CH1_' in p.name.upper() or '_CH01_' in p.name.upper()), p.name))
    if not paths:
        raise ValueError('No original WAVs found')
    output.mkdir(parents=True, exist_ok=True)
    with planning_lock(output):
        if (output/'survey.json').exists():
            previous = json.loads((output/'survey.json').read_text(encoding='utf-8'))
            for record in previous['files']:
                if record['complete']:
                    cache = (output/record['cache']).resolve()
                    if output not in cache.parents or digest(cache) != record['sha256']:
                        raise ValueError('Completed carrier cache changed')
        index = dict(schema=1, tape=source.name, source=str(source), settings=SETTINGS, complete=False, files=[])
        for path in paths:
            key = hashlib.sha256(str(path.relative_to(source)).encode()).hexdigest()[:16]
            cache = output/(key+'.json')
            result = scan_file(path, cache, max_windows)
            index['files'].append(dict(path=str(path), cache=cache.name, sha256=digest(cache),
                                       complete=result['complete'], status=result['carrier_status']))
            write_json(output/'survey.json', index)
            print(f'{path.name}: {result["carrier_status"]}; {result["window_count"]} windows', flush=True)
        index['complete'] = all(r['complete'] for r in index['files'])
        cache_rows = [json.loads((output/r['cache']).read_text(encoding='utf-8')) for r in index['files']]
        measured = sum(r['next_sample']/r['source']['sample_rate'] for r in cache_rows)
        remaining = sum((r['source']['frames']-r['next_sample'])/r['source']['sample_rate'] for r in cache_rows)
        wall = sum(r['performance']['wall_seconds'] for r in cache_rows)
        index['performance'] = dict(scanned_source_seconds=measured, remaining_source_seconds=remaining,
            wall_seconds=wall, cpu_seconds=sum(r['performance']['cpu_seconds'] for r in cache_rows),
            os_read_bytes=sum(r['performance'].get('os_read_bytes', 0) for r in cache_rows),
            projected_remaining_seconds=remaining*wall/measured if measured else None,
            projection_basis='Observed screening throughput; excludes subsequent detailed tracking and decoding')
        write_json(output/'survey.json', index)
    return index


def load_survey(path, require_complete=True):
    path = Path(path)
    if path.is_dir():
        path /= 'survey.json'
    index = json.loads(path.read_text(encoding='utf-8'))
    if require_complete and not index['complete']:
        raise ValueError('Carrier survey is incomplete')
    rows = []
    for record in index['files']:
        cache = (path.parent/record['cache']).resolve()
        if path.parent.resolve() not in cache.parents or digest(cache) != record['sha256']:
            raise ValueError('Carrier survey artifact changed')
        row = json.loads(cache.read_text(encoding='utf-8'))
        if row['source'] != source_record(record['path']) or row['settings'] != SETTINGS or row['implementation_sha256'] != digest(__file__):
            raise ValueError('Stale carrier survey')
        for block in row['feature_blocks']:
            feature_path = (path.parent/block['file']).resolve()
            if path.parent.resolve() not in feature_path.parents or digest(feature_path) != block['sha256']:
                raise ValueError('Carrier feature cache changed')
        rows.append(row)
    return index, rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, required=True, help='Canonical originals tape, or root with --all-tapes')
    parser.add_argument('--output', type=Path, default=DEFAULT_SURVEY_ROOT)
    parser.add_argument('--all-tapes', action='store_true')
    parser.add_argument('--max-windows', type=int, help='Per-file screening budget for cost benchmarking; resume by omitting')
    args = parser.parse_args()
    try:
        folders = sorted(args.input.glob('A15_T*')) if args.all_tapes else [args.input]
        if not folders:
            raise ValueError('No canonical tapes')
        for folder in folders:
            survey_tape(folder, args.output/folder.name, args.max_windows)
    except (ValueError, OSError, KeyError) as exc:
        parser.error(str(exc))


if __name__ == '__main__':
    main()
