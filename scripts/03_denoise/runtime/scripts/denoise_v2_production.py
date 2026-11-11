"""Streaming whole-channel v2 writer. The reviewed DSP is imported unchanged."""
import hashlib
import json
from pathlib import Path
import re
import shutil
import struct
import time

import numpy as np
import soundfile as sf

from denoise_archive import (ARCHIVE, CANONICAL, CHANNEL_WAV, CONTROL, MARKER, ORIGINALS, SOURCE_KEYS,
                             atomic_json, denoised_audio_name, denoised_audio_path, exact_path,
                             read_json, sha256, sync, validate_binding)
from denoise_v2 import ALGORITHM, SETTINGS, corrected_chunks
from denoise_v2_gain import Settings as GainSettings, measure as measure_gain
from denoise_v2_quality import residual_windows
from flutter_plan_store import planning_lock
from normalization import gain_for_peak
from render_denoise_v2 import gather_seeds, saved_seeds

STEP = SETTINGS.step_seconds*SETTINGS.sample_rate
BLOCK = 4 << 20
TIMING = 'copy_original_timing'
DENOISE = 'denoise_v2'
UNITY = 'unity.wav'


class IdentityError(ValueError):
    """An input, checkpoint, or completed cache conflicts with the frozen run."""


def action(job):
    return TIMING if job['action'] == 'copy_timing_original' else job['action']


def job_key(job):
    return Path(job['tape'])/f'CH{job["channel"]:02d}'


def validate_source(job, originals=ORIGINALS):
    row = job['source']
    path = exact_path(row['path'])
    stat = path.stat()
    match = re.search(r'_CH(\d+)_', path.name, re.I)
    timing = 'IRIG' in path.name.upper()
    if (path.parent != originals/job['tape'] or not path.name.startswith(job['tape']+'_')
            or not match or int(match[1]) != job['channel'] or row['channel'] != job['channel']
            or row['role'] != ('timing_only' if timing else 'reference_and_target')
            or (action(job) != DENOISE and (action(job) != TIMING or not timing))
            or (stat.st_size, stat.st_mtime_ns) != (row['bytes'], row['mtime_ns'])):
        raise IdentityError(f'Original source identity changed: {path}')
    info = sf.info(path)
    if (info.channels, info.samplerate, info.frames) != (1, SETTINGS.sample_rate, row['frames']):
        raise IdentityError(f'Original source header changed: {path}')
    if row['sample_rate'] != info.samplerate or job['source_frame_interval'] != [0, info.frames]:
        raise IdentityError(f'Invalid frozen source interval: {path}')
    return path, info


def validate_hint(job, hint_root):
    if action(job) == TIMING:
        return None
    hints = job['hints']
    path = exact_path(hint_root/job_key(job)/'cleanup.jsonl')
    if not hints.get('available') or sha256(path) != hints['sha256']:
        raise IdentityError(f'V1 frequency hints changed: {path}')
    return path


def wav_header(frames, rate, force_rf64=False):
    """Fixed final-size header, so resuming never depends on an interrupted WAV close."""
    size = frames*4
    fmt = struct.pack('<4sIHHIIHH', b'fmt ', 16, 3, 1, rate, rate*4, 4, 32)
    fact = struct.pack('<4sII', b'fact', 4, min(frames, 0xffffffff))
    if force_rf64 or size+48 >= 0xffffffff:
        header = struct.pack('<4sI4s4sIQQQI', b'RF64', 0xffffffff, b'WAVE', b'ds64', 28,
                             size+84, size, frames, 0)
        return header+fmt+fact+struct.pack('<4sI', b'data', 0xffffffff)
    return struct.pack('<4sI4s', b'RIFF', size+48, b'WAVE')+fmt+fact+struct.pack('<4sI', b'data', size)


def prefix(stream, size, digest=None, decoded=None):
    digest = hashlib.sha256() if digest is None else digest
    while size:
        block = stream.read(min(size, BLOCK))
        if not block:
            raise IdentityError('Truncated committed output prefix')
        digest.update(block)
        if decoded is not None:
            decoded.update(block)
        size -= len(block)
    return digest


def write_entry(stream, digest, entry):
    block = (json.dumps(entry, separators=(',', ':'), allow_nan=False)+'\n').encode('utf-8')
    stream.write(block)
    digest.update(block)


def output_metrics(path, info, *, expected_hash, expected_decoded=None, header=None):
    """Verify the written bytes and report finite decoded peak without a second full read."""
    value = sf.info(path)
    if (value.frames, value.samplerate, value.channels, value.subtype, value.format) != info:
        raise IdentityError(f'Published audio header mismatch: {path}')
    peak, over = 0., 0
    digest, decoded = hashlib.sha256(), hashlib.sha256()
    if header is not None:
        with path.open('rb') as stream:
            actual = stream.read(len(header))
            if actual != header:
                raise IdentityError('Output WAV header changed')
            digest.update(actual)
            for block in iter(lambda: stream.read(BLOCK), b''):
                x = np.frombuffer(block, dtype='<f4')
                if not np.isfinite(x).all():
                    raise IdentityError('Non-finite output samples')
                peak = max(peak, float(np.max(abs(x))))
                over += int(np.count_nonzero(abs(x) > 1))
                decoded.update(block)
                digest.update(block)
        if path.stat().st_size != len(header)+4*value.frames or decoded.hexdigest() != expected_decoded:
            raise IdentityError('Output WAV sample count or decoded hash changed')
    else:
        # Timing files retain the original encoding, including ancillary chunks.
        # Decode for finite/peak verification, and hash the original file bytes.
        with sf.SoundFile(path) as stream:
            for x in stream.blocks(blocksize=BLOCK//4, dtype='float32'):
                if not np.isfinite(x).all():
                    raise IdentityError('Non-finite timing samples')
                peak = max(peak, float(np.max(abs(x))))
                over += int(np.count_nonzero(abs(x) > 1))
                decoded.update(x.astype('<f4', copy=False))
        with path.open('rb') as stream:
            digest = prefix(stream, path.stat().st_size)
    if digest.hexdigest() != expected_hash:
        raise IdentityError('Output WAV bytes changed')
    stat = path.stat()
    return dict(frames=value.frames, sample_rate=value.samplerate, channels=1, format=value.format,
                subtype=value.subtype, peak=peak, samples_above_full_scale=over, bytes=stat.st_size,
                mtime_ns=stat.st_mtime_ns, sha256=digest.hexdigest(),
                decoded_float32_sha256=decoded.hexdigest())


def new_record(job, freeze, hint):
    row = job['source']
    return dict(schema=2, cleanup_version='v2', algorithm=ALGORITHM, action=action(job),
                run_id=freeze['run_id'], frozen=freeze, source=row,
                signature=dict(source={k:row[k] for k in SOURCE_KEYS}, offset_frames=0,
                               frames=row['frames']), source_frame_interval=job['source_frame_interval'],
                canonical_start_offset_frames=round(row['start_seconds']*row['sample_rate']),
                gain=1.0, hint_provenance=None if hint is None else dict(
                    relative_path=str(job_key(job)/'cleanup.jsonl'),
                    cleanup_version='v1', sha256=job['hints']['sha256']))


def verify_completed(folder, job, freeze, *, full=False):
    record = read_json(folder/'denoised.json')
    expected = new_record(job, freeze, True if action(job) == DENOISE else None)
    if action(job) == DENOISE and 'normalization' in freeze:
        gain = record['normalization']
        settings = freeze['normalization']
        expected_gain = gain_for_peak(gain['input_peak'], settings['target_dbfs'], settings['max_boost_db'])
        if (gain['settings'] != settings or gain['gain'] != expected_gain['gain']
                or gain['gain'] != record['gain']):
            raise IdentityError(f'Incompatible normalization record: {folder}')
        expected['gain'] = gain['gain']
    if any(record.get(k) != value for k, value in expected.items()):
        raise IdentityError(f'Incompatible completed v2 channel: {folder}')
    try:
        path = validate_binding(folder, record, job['source'], 0, job['source']['frames'], 'v2')
    except ValueError as exc:
        raise IdentityError(str(exc)) from exc
    if sha256(folder/'cleanup.jsonl') != record['cleanup_log_sha256']:
        raise IdentityError(f'Cleanup evidence changed: {folder}')
    if full and sha256(path) != record['audio']['sha256']:
        raise IdentityError(f'Completed audio bytes changed: {folder}')
    return record


def render_timing(source, stage, info):
    audio, log = stage/'denoised.wav', stage/'cleanup.jsonl'
    digest, notes = hashlib.sha256(), hashlib.sha256()
    with source.open('rb') as src, audio.open('wb') as dest:
        for block in iter(lambda: src.read(BLOCK), b''):
            digest.update(block)
            dest.write(block)
        sync(dest)
    with log.open('wb') as stream:
        write_entry(stream, notes, dict(kind='bypass', action=TIMING, source_frames=[0, info.frames]))
        sync(stream)
    return (output_metrics(audio, (info.frames, info.samplerate, 1, info.subtype, info.format),
                           expected_hash=digest.hexdigest()), notes.hexdigest(), dict(status='timing_bypass'))


def render_float(source, stage, info, hint, record, thresholds, progress, resume, stop):
    total, rate = info.frames, info.samplerate
    header = wav_header(total, rate)
    name = UNITY if 'normalization' in record['frozen'] else 'denoised.wav'
    audio, log, checkpoint = (stage/n for n in (name, 'cleanup.jsonl', 'checkpoint.json'))
    cursor, last_anchor, log_bytes, flags = 0, -1, 0, 0
    identity = dict(record, quality_thresholds=thresholds)
    if checkpoint.exists():
        if not resume:
            raise IdentityError(f'Use resume for an existing checkpoint: {stage}')
        saved = read_json(checkpoint)
        cursor, last_anchor, log_bytes, flags = (saved[k] for k in ('cursor', 'last_anchor', 'log_bytes', 'flags'))
        if (saved['identity'] != identity or not 0 < cursor <= total
                or cursor != total and cursor % STEP
                or last_anchor != ((cursor+STEP-1)//STEP)*STEP):
            raise IdentityError(f'Incompatible checkpoint: {stage}')
    seeds = saved_seeds(hint, 0, total)
    with (audio.open('r+b' if cursor else 'w+b') as dest,
          log.open('r+b' if cursor else 'w+b') as journal,
          sf.SoundFile(source) as original):
        if cursor:
            actual_header = dest.read(len(header))
            if actual_header != header:
                raise IdentityError('Checkpoint WAV header changed')
            decoded = hashlib.sha256()
            digest = prefix(dest, 4*cursor, hashlib.sha256(header), decoded)
            notes = prefix(journal, log_bytes)
            if digest.hexdigest() != saved['audio_sha256'] or notes.hexdigest() != saved['log_sha256']:
                raise IdentityError(f'Committed audio or cleanup evidence changed: {stage}')
            dest.truncate()
            journal.truncate()
        else:
            dest.write(header)
            digest, decoded, notes = hashlib.sha256(header), hashlib.sha256(), hashlib.sha256()

        def read(frame, count):
            original.seek(frame)
            return original.read(count, dtype='float64')

        chunks = (corrected_chunks(read, total, cursor, total, lambda a, b: gather_seeds(seeds, a, b))
                  if cursor < total else ())
        for frame, raw, result, left, right in chunks:
            if stop is not None and stop.exists():
                raise InterruptedError('Stopped after the last committed checkpoint')
            if frame != cursor or len(result) != min(STEP, total-cursor):
                raise IdentityError('DSP chunk broke the source-frame grid')
            x = result.astype('<f4')
            if not np.isfinite(x).all():
                raise ValueError('Non-finite float32 correction')
            for node in (left, right):
                if node['anchor_frame'] > last_anchor:
                    write_entry(journal, notes, dict(kind='anchor', **node))
                    last_anchor = node['anchor_frame']
            windows = residual_windows(raw, x, rate, left, right, thresholds, frame)
            flags += sum(bool(window['flags']) for window in windows)
            cursor += len(x)
            write_entry(journal, notes, dict(kind='interval', source_frames=[frame, cursor], windows=windows))
            dest.write(x.tobytes())
            digest.update(x)
            decoded.update(x)
            sync(dest)
            sync(journal)
            atomic_json(checkpoint, dict(identity=identity, cursor=cursor, last_anchor=last_anchor,
                log_bytes=journal.tell(), flags=flags, audio_sha256=digest.hexdigest(),
                log_sha256=notes.hexdigest()))
            if cursor == total or cursor % (STEP*8) == 0:
                progress(f'{cursor/rate:,.0f}/{total/rate:,.0f} source seconds')
    fmt = 'RF64' if header[:4] == b'RF64' else 'WAV'
    return (output_metrics(audio, (total, rate, 1, 'FLOAT', fmt), expected_hash=digest.hexdigest(),
                           expected_decoded=decoded.hexdigest(), header=header),
            notes.hexdigest(), dict(status='flagged' if flags else 'measured_pending_review',
                                    flagged_windows=flags, thresholds=thresholds, automatic_retries=0))


def normalize_audio(unity_path, output_path, audio, settings, stop=None):
    """Write a new float WAV and verify its bytes while the unity input stays intact."""
    total, rate = audio['frames'], audio['sample_rate']
    header = wav_header(total, rate, force_rf64=audio['format'] == 'RF64')
    fmt = 'RF64' if header[:4] == b'RF64' else 'WAV'
    digest, decoded = hashlib.sha256(header), hashlib.sha256()
    partial = output_path.with_suffix('.wav.partial')
    before = unity_path.stat()

    def check_stop():
        if stop is not None and stop.exists():
            raise InterruptedError('Stopped before publishing normalized audio')

    with sf.SoundFile(unity_path) as unity:
        if (unity.frames, unity.samplerate, unity.channels, unity.subtype) != (total, rate, 1, 'FLOAT'):
            raise IdentityError(f'Unity WAV header changed: {unity_path}')
        normalization = measure_gain(unity, GainSettings(**settings), check_stop)
        scaled_peak = normalization['unfiltered_input_peak']*normalization['gain']
        if scaled_peak > np.finfo(np.float32).max:
            raise ValueError('Normalized peak is too large for a float32 WAV')
        input_decoded = hashlib.sha256()
        unity.seek(0)
        with partial.open('wb') as output:
            output.write(header)
            for samples in unity.blocks(blocksize=BLOCK//4, dtype='float32'):
                check_stop()
                input_decoded.update(samples.astype('<f4', copy=False))
                scaled = (samples.astype('float64')*normalization['gain']).astype('<f4')
                output.write(scaled.tobytes())
                digest.update(scaled)
                decoded.update(scaled)
            sync(output)
    after = unity_path.stat()
    if ((before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns)
            or input_decoded.hexdigest() != audio['decoded_float32_sha256']):
        raise IdentityError(f'Unity audio changed during normalization: {unity_path}')
    verified = output_metrics(partial, (total, rate, 1, 'FLOAT', fmt), expected_hash=digest.hexdigest(),
                              expected_decoded=decoded.hexdigest(), header=header)
    expected_peak = float(np.float32(scaled_peak))
    if verified['peak'] != expected_peak:
        raise IdentityError('Normalized WAV peak does not match the constant gain')
    partial.replace(output_path)
    normalization.update(unity_audio_sha256=audio['sha256'], output_peak=verified['peak'],
                         samples_above_full_scale=verified['samples_above_full_scale'])
    return verified, normalization


def write_channel(job, freeze, root, work, hint_root, thresholds, *, resume=False,
                  originals=ORIGINALS, progress=print, verify_freeze=lambda: None, stop=None, reuse_unity=None,
                  previous_run_id=None):
    root, work = exact_path(root), exact_path(work)
    if (any(p.name.lower() in {'denoised_v1', 'orig_concatenated'}
            for candidate in (root, work) for p in (candidate, *candidate.parents))
            or root == work or root in work.parents or work in root.parents):
        raise IdentityError('V2 requires separate run-owned staging and output paths')
    if originals == ORIGINALS:
        run = CONTROL/freeze['run_id']
        if (root, work, hint_root) not in {(CANONICAL, run/'work', ARCHIVE),
                (run/'canary', run/'canary_work', ARCHIVE), (run/'canary', run/'canary_work', CANONICAL)}:
            raise IdentityError('V2 cannot write outside its frozen production or canary roots')
    marker = read_json(root/MARKER)
    if (marker['cleanup_version'] != 'v2' or marker['run_id'] != freeze['run_id']
            or marker['frozen'] != freeze or root.name.lower() in {'denoised_v1', 'orig_concatenated'}):
        raise IdentityError(f'Output root does not own this v2 run: {root}')
    stage = exact_path(work/job_key(job))
    final = exact_path(root/job_key(job))
    output_name = denoised_audio_name(job['channel'], freeze)
    with planning_lock(stage):
        for folder in (stage, final):
            for name in (output_name, output_name+'.partial', UNITY, 'denoised.json', 'cleanup.jsonl', 'checkpoint.json',
                         'checkpoint.json.partial', 'denoised.json.partial'):
                exact_path(folder/name)
        verify_freeze()
        source, info = validate_source(job, originals)
        hint = validate_hint(job, hint_root)
        if final.exists() and not pending_in_place(final, stage, previous_run_id):
            return verify_completed(final, job, freeze)
        record = new_record(job, freeze, hint)
        manifest = stage/'denoised.json'
        if stage.exists():
            if not resume:
                raise IdentityError(f'Use resume for unfinished channel: {stage}')
            if manifest.exists():
                old = read_json(manifest)
                if old['status'] == 'complete':
                    result = verify_completed(stage, job, freeze, full=True)
                    publish(stage, final, previous_run_id)
                    return result
                if any(old.get(k) != v for k, v in record.items()):
                    raise IdentityError(f'Incompatible staging manifest: {stage}')
            elif any(stage.iterdir()):
                raise IdentityError(f'Unowned staging files: {stage}')
        stage.mkdir(parents=True, exist_ok=True)
        started = time.monotonic()
        atomic_json(manifest, dict(record, status='running'))
        try:
            if action(job) == TIMING:
                audio, notes, quality = render_timing(source, stage, info)
            else:
                if reuse_unity is not None:
                    unity_folder, prior_freeze = reuse_unity
                    for name in ('denoised.wav', 'denoised.json', 'cleanup.jsonl'):
                        exact_path(unity_folder/name)
                    prior = verify_completed(unity_folder, job, prior_freeze, full=True)
                    if ('normalization' not in freeze or prior['gain'] != 1.0
                            or (stage/'checkpoint.json').exists()):
                        raise IdentityError('Reusing a unity canary requires separate, unrendered staging')
                    audio, notes, quality = prior['audio'], prior['cleanup_log_sha256'], prior['quality']
                    unity_path = denoised_audio_path(unity_folder, prior)
                    shutil.copyfile(unity_folder/'cleanup.jsonl', stage/'cleanup.jsonl')
                else:
                    audio, notes, quality = render_float(source, stage, info, hint, record, thresholds, progress, resume, stop)
                    unity_path = stage/UNITY
                if 'normalization' in freeze:
                    audio, gain = normalize_audio(unity_path, stage/output_name, audio, freeze['normalization'], stop)
                    record.update(gain=gain['gain'], normalization=gain)
                    if reuse_unity is not None:
                        record['reused_unity_run_id'] = prior_freeze['run_id']
                    progress(f"Normalized by {gain['gain_db']:+.2f} dB, excluding {len(gain['ignored_endpoint_impulses'])} endpoint peaks")
            validate_source(job, originals)
            validate_hint(job, hint_root)
            verify_freeze()
            if sha256(stage/'cleanup.jsonl') != notes:
                raise IdentityError('Cleanup log verification failed')
            if read_json(root/MARKER) != marker:
                raise IdentityError('Denoise root marker changed')
            if freeze.get('audio_naming') == CHANNEL_WAV:
                audio['filename'] = output_name
            record.update(status='complete', audio=audio, cleanup_log_sha256=notes, quality=quality,
                          worker_seconds=time.monotonic()-started)
            atomic_json(manifest, record)
        except BaseException as exc:
            failed = new_record(job, freeze, hint)
            atomic_json(manifest, dict(failed, status='failed', error=f'{type(exc).__name__}: {exc}'))
            raise
        publish(stage, final, previous_run_id)
        return record


def pending_in_place(final, stage, previous_run_id):
    if previous_run_id is None or not final.is_dir():
        return False
    manifest = final/'denoised.json'
    if manifest.is_file():
        return read_json(manifest).get('run_id') == previous_run_id
    return (stage/'denoised.json').is_file()


def publish_into_existing(stage, final, previous_run_id):
    """Keep existing WAVs and install the new channel manifest last."""
    if not pending_in_place(final, stage, previous_run_id):
        raise IdentityError(f'Output already exists: {final}')
    new_manifest = stage/'denoised.json'
    record = read_json(new_manifest)
    old_manifest = final/'denoised.json'
    old_log = final/'cleanup.jsonl'
    if old_manifest.exists():
        previous = read_json(old_manifest)
        if (previous['signature']['source'] != record['signature']['source']
                or previous['status'] != 'complete'):
            raise IdentityError(f'Unexpected previous channel binding: {final}')
        old_record = exact_path(final/f'denoised.{previous_run_id}.json')
        if not old_record.exists():
            shutil.copyfile(old_manifest, old_record)
        if previous['cleanup_log_sha256'] != record['cleanup_log_sha256']:
            old_evidence = exact_path(final/f'cleanup.{previous_run_id}.jsonl')
            if not old_evidence.exists():
                if sha256(old_log) != previous['cleanup_log_sha256']:
                    raise IdentityError(f'Previous cleanup log changed: {final}')
                shutil.copyfile(old_log, old_evidence)
    source = denoised_audio_path(stage, record)
    target = exact_path(final/source.name)
    if target.exists():
        if sha256(target) != record['audio']['sha256']:
            raise IdentityError(f'Output WAV already exists with different audio: {target}')
    else:
        target.hardlink_to(source)
    for name in ('cleanup.jsonl', 'denoised.json'):
        partial = exact_path(final/(name+'.partial'))
        shutil.copyfile(stage/name, partial)
        partial.replace(final/name)
    for name in (source.name, UNITY, 'checkpoint.json', 'cleanup.jsonl', 'denoised.json'):
        (stage/name).unlink(missing_ok=True)
    stage.rmdir()


def publish(stage, final, previous_run_id=None):
    if final.exists():
        publish_into_existing(stage, final, previous_run_id)
        return
    final.parent.mkdir(parents=True, exist_ok=True)
    (stage/UNITY).unlink(missing_ok=True)
    stage.rename(final)
    # Checkpoints are private while running. A crash here leaves an extra
    # verified checkpoint alongside the complete channel, never partial audio.
    (final/'checkpoint.json').unlink(missing_ok=True)
