"""Stage-03 v1 cleanup cache helpers.

The former per-channel map evaluation was removed; the active deflutter
renderer is shared_render with one shared tape map.
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

from audio_processing import clean_tones, denoise, DEFAULT_BROADBAND_CAP_DB, TONAL_CLEANUP_METHOD
from denoise_archive import (cleanup_version, denoised_audio_path, historical_path, legacy_write,
                             read_json, root_marker, sha256, validate_binding)
from flutter_plan_store import planning_lock, tape_directory

RATE = 200


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


def rising(x):
    return .5-.5*np.cos(np.pi*np.clip(x, 0, 1))


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
