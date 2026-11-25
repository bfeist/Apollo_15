"""Run the frozen Apollo 15 v2 denoise rollout, with guarded cutover and resume."""
import os

THREAD_VARS = ('OPENBLAS_NUM_THREADS', 'MKL_NUM_THREADS', 'OMP_NUM_THREADS',
               'BLIS_NUM_THREADS', 'NUMEXPR_NUM_THREADS', 'VECLIB_MAXIMUM_THREADS')

if __name__ == '__main__':
    for variable in THREAD_VARS:
        os.environ[variable] = '1'

import argparse
from collections import Counter, defaultdict, deque
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
import hashlib
import json
from pathlib import Path
import re
import shutil
import sys
import time

import numpy as np
import scipy
import soundfile as sf

from denoise_archive import (ALL_NUMBERED, ARCHIVE, CANONICAL, CHANNEL_WAV, CONTROL, MARKER, ORIGINALS,
                             TRANSACTION, archive_lock, atomic_json, cleanup_version, denoised_audio_name,
                             denoised_audio_path, exact_path, read_json,
                             require_closed_files, sha256)
from denoise_v2 import ALGORITHM, SETTINGS, configuration
from denoise_v2_gain import configuration as gain_configuration
from denoise_v2_production import (DENOISE, TIMING, IdentityError, action, job_key, publish,
                                   normalize_audio, pending_in_place, validate_hint, validate_source, verify_completed,
                                   wav_header, write_channel)
from denoise_v2_quality import MAX_REEVALUATIONS, compare_review, freeze_thresholds, review_windows

REPO = Path(__file__).resolve().parents[1]
INVENTORY = REPO/'reports/denoise_v2_rollout_20261108/inventory.json'
REVIEW = REPO/'reports/denoise_v2_late_hum_20261108'
CANARY_TAPE = 'A15_T510083'
OUTPUT_NAMING = dict(audio_naming=CHANNEL_WAV)
RESERVE = 100 << 30
FROZEN_FILES = ('denoise_v2.py', 'denoise_v2_refinement.py', 'render_denoise_v2.py',
                'denoise_v2_production.py', 'denoise_v2_quality.py', 'denoise_archive.py',
                'denoise_v2_rollout.py', 'denoise_v2_gain.py', 'normalization.py')


def freeze_run(inventory_path, review_path):
    implementation = {name: sha256(Path(__file__).with_name(name)) for name in FROZEN_FILES}
    review = read_json(review_path)
    versions = dict(python=sys.version, numpy=np.__version__, scipy=scipy.__version__,
                    soundfile=sf.__version__, libsndfile=sf.__libsndfile_version__)
    if (review['algorithm'] != ALGORITHM
            or review['implementation_sha256'] != implementation['denoise_v2.py']
            or review['refinement_sha256'] != implementation['denoise_v2_refinement.py']
            or review['settings'] != configuration()
            or any(versions[k] != review['versions'][k] for k in ('numpy', 'scipy', 'soundfile'))):
        raise IdentityError('Installed DSP or numerical libraries differ from the saved v2.16 review')
    record = dict(algorithm=ALGORITHM, implementation_sha256=implementation,
                  inventory_sha256=sha256(inventory_path), review_sha256=sha256(review_path),
                  settings=configuration(), normalization=gain_configuration(), versions=versions,
                  channel_policy=ALL_NUMBERED, **OUTPUT_NAMING)
    return dict(record, run_id='v2-'+hashlib.sha256(
        json.dumps(record, sort_keys=True).encode()).hexdigest()[:24])


def archive_snapshot(root):
    result = {}
    for path in sorted(root.rglob('*')):
        exact_path(path)
        if not path.is_file():
            continue
        stat = path.stat()
        record = dict(bytes=stat.st_size, mtime_ns=stat.st_mtime_ns)
        if path.suffix.lower() in {'.json', '.jsonl'}:
            record['sha256'] = sha256(path)
        if path.name == 'denoised.json':
            manifest = read_json(path)
            if cleanup_version(manifest) != 'v1':
                raise IdentityError(f'Expected legacy denoise tree: {path}')
            record['recorded_audio'] = manifest.get('audio')
        result[path.relative_to(root).as_posix()] = record
    return result


def marker(freeze):
    return dict(schema=1, cleanup_version='v2', run_id=freeze['run_id'], frozen=freeze)


def rotate(freeze, canonical=CANONICAL, archive=ARCHIVE, control=CONTROL):
    """Recover or commit a rotation while the caller holds the archive lock."""
    canonical, archive, control = map(exact_path, (canonical, archive, control))
    if canonical.parent != archive.parent or (canonical.name, archive.name) != ('denoised', 'denoised_v1'):
        raise IdentityError('Unexpected archive rotation paths')
    path, snapshot = control/TRANSACTION, control/('legacy_'+freeze['run_id']+'.json')
    binding = dict(version='v1', run_id=freeze['run_id'], **{'from': str(canonical), 'to': str(archive)})
    if path.exists():
        transaction = read_json(path)
        if transaction['status'] == 'rolled_back' or transaction['status'] == 'rolling_back':
            raise IdentityError('Previous cutover was rolled back or rollback is pending')
        if any(transaction.get(k) != v for k,v in binding.items()):
            raise IdentityError('Migration belongs to another run')
        if sha256(snapshot) != transaction['snapshot_sha256']:
            raise IdentityError('Legacy migration inventory changed')
        if transaction['status'] == 'complete':
            if not archive.is_dir() or read_json(canonical/MARKER) != marker(freeze):
                raise IdentityError('Completed migration roots changed')
            return
    else:
        if archive.exists() or not canonical.is_dir() or (canonical/MARKER).exists():
            raise IdentityError('Expected unversioned denoised root and an absent denoised_v1')
        files = archive_snapshot(canonical)
        if not any(name.endswith('/denoised.json') for name in files):
            raise IdentityError('Legacy tree has no denoised manifests')
        atomic_json(snapshot, files)
        transaction = dict(binding, status='prepared', snapshot_sha256=sha256(snapshot))
        atomic_json(path, transaction)
    files = read_json(snapshot)
    if not archive.exists():
        if not canonical.is_dir() or (canonical/MARKER).exists() or archive_snapshot(canonical) != files:
            raise IdentityError('Legacy root changed since migration was prepared')
        require_closed_files(canonical, files)
        canonical.rename(archive)
    if archive_snapshot(archive) != files:
        raise IdentityError('Archived v1 tree differs from the pre-rotation inventory')
    canonical.mkdir(exist_ok=True)
    if (canonical/MARKER).exists():
        if read_json(canonical/MARKER) != marker(freeze):
            raise IdentityError('Canonical v2 marker differs from migration')
    else:
        if any(p.name != MARKER+'.partial' for p in canonical.iterdir()):
            raise IdentityError('Refusing to mark a populated canonical directory')
        atomic_json(canonical/MARKER, marker(freeze))
    atomic_json(path, dict(transaction, status='complete'))


def rollback(control=CONTROL, canonical=CANONICAL, archive=ARCHIVE):
    """Preserve the v2 tree under a unique name before restoring original v1 names."""
    import uuid

    control, canonical, archive = map(exact_path, (control, canonical, archive))
    path = control/TRANSACTION
    transaction = read_json(path)
    if transaction['from'] != str(canonical) or transaction['to'] != str(archive):
        raise IdentityError('Rollback roots differ from the migration record')
    if transaction['status'] == 'rolled_back':
        return
    if transaction['status'] != 'rolling_back':
        if transaction['status'] != 'complete' or read_json(canonical/MARKER)['run_id'] != transaction['run_id']:
            raise IdentityError('Finish the recorded cutover before rolling it back')
        quarantine = canonical.with_name('denoised_quarantine_'+uuid.uuid4().hex)
        transaction.update(status='rolling_back', quarantine=str(quarantine))
        atomic_json(path, transaction)
    quarantine = exact_path(transaction['quarantine'])
    if quarantine.parent != canonical.parent or not quarantine.name.startswith('denoised_quarantine_'):
        raise IdentityError('Invalid rollback quarantine path')
    snapshot = control/('legacy_'+transaction['run_id']+'.json')
    if sha256(snapshot) != transaction['snapshot_sha256']:
        raise IdentityError('Legacy migration inventory changed')
    if not quarantine.exists():
        if archive_snapshot(archive) != read_json(snapshot):
            raise IdentityError('Legacy archive changed before rollback')
        require_closed_files(canonical, [p.relative_to(canonical) for p in canonical.rglob('*') if p.is_file()])
        canonical.rename(quarantine)
    if read_json(quarantine/MARKER)['run_id'] != transaction['run_id']:
        raise IdentityError('Rollback quarantine belongs to another run')
    if archive.exists():
        if canonical.exists():
            raise IdentityError('Refusing to restore v1 over an existing tree')
        archive.rename(canonical)
    if archive_snapshot(canonical) != read_json(snapshot):
        raise IdentityError('Restored v1 tree differs from the original inventory')
    atomic_json(path, dict(transaction, status='rolled_back'))


def load_inventory(path):
    value = read_json(path)
    policy = value['path_policy']
    if (value['schema'] != 1 or value['failures']
            or tuple(Path(policy[k]) for k in ('originals','current_v1','archive_v1','new_v2'))
            != (ORIGINALS, CANONICAL, ARCHIVE, CANONICAL)):
        raise IdentityError('Unexpected frozen inventory schema, failures, or path policy')
    jobs = value['jobs']
    keys = {job_key(j) for j in jobs}
    counts = Counter(action(j) for j in jobs)
    if len(keys) != len(jobs) or counts != {DENOISE: 1130, TIMING: 39}:
        raise IdentityError('Unexpected inventory actions or duplicate jobs')
    for job in jobs:
        key = job_key(job)
        if (not 1 <= job['channel'] <= 30 or key.parent.parent != Path('.')
                or Path(job['output_directory']) != CANONICAL/key
                or Path(job['archive_directory']) != ARCHIVE/key):
            raise IdentityError(f'Invalid frozen job destination: {key}')
    # The saved inventory binds files and original filename labels. Its former
    # copy recommendation does not establish where the timing signal is recorded.
    jobs = [dict(job, action=DENOISE, expected_audio_bytes=job['source']['frames']*4+4096)
            if action(job) == TIMING else job for job in jobs]
    value.update(jobs=jobs, counts=dict(value['counts'], denoise=len(jobs), timing_copy=0,
                                       formerly_timing_labeled=counts[TIMING], compatible_hint_logs=len(jobs)),
                 expected_new_audio_bytes=sum(job['expected_audio_bytes'] for job in jobs),
                 denoise_channel_hours=sum(job['source']['duration_seconds'] for job in jobs)/3600)
    return value


def disk_check(root, work, jobs, workers=1):
    remaining = 0
    metadata = 0
    scratch_sizes = []
    for job in jobs:
        key = job_key(job)
        if (root/key).exists():
            continue
        partial = work/key/denoised_audio_name(job['channel'], OUTPUT_NAMING)
        remaining += max(0, job['expected_audio_bytes']-(partial.stat().st_size if partial.exists() else 0))
        if action(job) == DENOISE:
            metadata += job['source']['frames']//(SETTINGS.sample_rate*SETTINGS.step_seconds)*128*1024
            scratch_sizes.append(job['expected_audio_bytes'])
    scratch = sum(sorted(scratch_sizes, reverse=True)[:workers])
    free = shutil.disk_usage(CONTROL.parent).free
    required = remaining+metadata+scratch+RESERVE
    if free < required:
        raise IdentityError(f'Need {required/(1<<30):,.1f} GiB free; available {free/(1<<30):,.1f} GiB')
    return dict(remaining_audio_bytes=remaining, estimated_metadata_bytes=metadata,
                normalization_staging_bytes=scratch, free_bytes=free, reserve_bytes=RESERVE)


def memory_guard(workers):
    if os.name != 'nt':
        return
    import ctypes
    available = (ctypes.c_ulonglong*8)()
    available[0] = ctypes.sizeof(available)
    if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(available)):
        # Available physical memory in MEMORYSTATUSEX.
        if available[2] < (8+1.5*workers)*(1<<30):
            raise IdentityError('Insufficient available memory for this worker count and 8 GiB reserve')


def invoke(job, freeze, root, work, hint_root, thresholds, resume, inventory, review, stop=None, reuse_unity=None,
           previous_run_id=None):
    def check():
        if (sha256(inventory) != freeze['inventory_sha256'] or sha256(review) != freeze['review_sha256']
                or any(sha256(Path(__file__).with_name(name)) != digest
                       for name, digest in freeze['implementation_sha256'].items())):
            raise IdentityError('Code, settings, inventory, or environment changed during the rollout')

    return write_channel(job, freeze, root, work, hint_root, thresholds, resume=resume,
                         progress=lambda message: print(f'{job_key(job)} {message}', flush=True),
                         verify_freeze=check, stop=stop, reuse_unity=reuse_unity, previous_run_id=previous_run_id)


def canary_jobs(jobs):
    selected = [job for job in jobs if (job['tape'], job['channel']) == (CANARY_TAPE, 20)]
    if len(selected) != 1:
        raise IdentityError('Frozen inventory must contain T510083 CH20')
    return selected


def preflight(inventory, hints):
    for job in inventory['jobs']:
        validate_source(job)
        validate_hint(job, hints)
    return dict(counts=inventory['counts'], denoise_channel_hours=inventory['denoise_channel_hours'],
                missing={t['tape']:t['missing_channels'] for t in inventory['tapes'] if t['missing_channels']},
                excluded=[f for t in inventory['tapes'] for f in t['excluded_files']])


def collect_canary(jobs, run, freeze, review_path):
    root = run/'canary'
    checks = {}
    for job in canary_jobs(jobs):
        folder = root/job_key(job)
        saved = verify_completed(folder, job, freeze, full=True)
        review = read_json(review_path)
        if (job['hints']['sha256'] != review['cleanup_log_sha256']
                or any(job['source'][key] != review['source'][key]
                       for key in ('path', 'bytes', 'mtime_ns', 'frames'))):
            raise IdentityError('Canary source or frequency hints differ from the saved comparison')
        reference = review_path.with_name('03_denoised_v2.wav')
        checks['review_crop'] = compare_review(denoised_audio_path(folder, saved), review, reference,
                                              job['source']['sample_rate'], saved['gain'])
        checks['normalization'] = saved['normalization']
        checks['thresholds'] = freeze_thresholds(folder/'cleanup.jsonl', *review['source_frames'])
        checks['observations'] = review_windows(folder/'cleanup.jsonl', job['source']['frames'],
                                               job['source']['sample_rate'], review['source_frames'][0])
        checks['manifest_sha256'] = sha256(folder/'denoised.json')
        checks['denoise_log'] = str(folder/'cleanup.jsonl')
    return dict(run_id=freeze['run_id'], checks=checks, max_reevaluations=MAX_REEVALUATIONS,
                status='pending_listening_review')


def compatible_unity_canary(control, run_id, freeze):
    if not re.fullmatch(r'v2-[0-9a-f]{24}', run_id):
        raise IdentityError('Expected a frozen unity canary run ID')
    prior_run = exact_path(control/run_id)
    prior = read_json(prior_run/'frozen.json')
    if (prior['run_id'] != run_id or 'normalization' in prior
            or any(prior[key] != freeze[key] for key in
                   ('algorithm', 'inventory_sha256', 'review_sha256', 'settings', 'versions'))
            or any(prior['implementation_sha256'][key] != freeze['implementation_sha256'][key]
                   for key in ('denoise_v2.py', 'denoise_v2_refinement.py', 'render_denoise_v2.py'))):
        raise IdentityError('The prior unity canary used different sources, hum subtraction, or numerical settings')
    root = exact_path(prior_run/'canary')
    if read_json(root/MARKER) != marker(prior):
        raise IdentityError('Prior unity canary root marker differs from its frozen run')
    return root, prior


def run_canary(args, jobs, freeze, run, hints, review):
    prior = compatible_unity_canary(CONTROL, args.reuse_unity_canary, freeze) if args.reuse_unity_canary else None
    root, work = run/'canary', run/'canary_work'
    root.mkdir(exist_ok=True)
    if not (root/MARKER).exists():
        if any(p.name != MARKER+'.partial' for p in root.iterdir()):
            raise IdentityError('Unowned canary root')
        atomic_json(root/MARKER, marker(freeze))
    disk_check(root, work, canary_jobs(jobs))
    for job in canary_jobs(jobs):
        reuse = None
        if prior and action(job) == DENOISE:
            folder = exact_path(prior[0]/job_key(job))
            if not (folder/'denoised.json').is_file():
                raise IdentityError('The unity denoise canary is still incomplete. Finish it before adding normalization.')
            reuse = folder, prior[1]
        invoke(job, freeze, root, work, hints, None, True, args.inventory, review, reuse_unity=reuse)
    report = collect_canary(jobs, run, freeze, review)
    atomic_json(run/'canary.json', report)
    print(json.dumps(report, indent=2))


def adopt_timing_copy_run(jobs, freeze, run, prior_id, review):
    """Carry the approved, byte-identical CH20 canary into the all-channel run."""
    if not re.fullmatch(r'v2-[0-9a-f]{24}', prior_id):
        raise IdentityError('Expected a previous frozen run ID')
    prior_run = exact_path(CONTROL/prior_id)
    prior = read_json(prior_run/'frozen.json')
    if (prior['run_id'] != prior_id or 'channel_policy' in prior
            or any(prior[key] != freeze[key] for key in
                   ('algorithm', 'inventory_sha256', 'review_sha256', 'settings', 'normalization', 'versions'))
            or any(prior['implementation_sha256'][name] != freeze['implementation_sha256'][name]
                   for name in ('denoise_v2.py', 'denoise_v2_refinement.py', 'render_denoise_v2.py',
                                'denoise_v2_gain.py', 'normalization.py', 'denoise_v2_quality.py'))):
        raise IdentityError('The earlier run differs in source inventory, denoise DSP, or normalization')
    transaction = read_json(CONTROL/TRANSACTION)
    if (transaction['status'] != 'complete' or transaction['run_id'] != prior_id
            or read_json(CANONICAL/MARKER) != marker(prior)):
        raise IdentityError('The earlier run does not own the current production root')
    approval_path = prior_run/'approval.json'
    approval = read_json(approval_path)
    if approval['run_id'] != prior_id or approval['status'] != 'accepted':
        raise IdentityError('The earlier canary needs recorded listening acceptance')

    job = canary_jobs(jobs)[0]
    source = exact_path(CANONICAL/job_key(job))
    if sha256(source/'denoised.json') != approval['checks']['manifest_sha256']:
        raise IdentityError('The earlier canary manifest does not match its approval')
    record = verify_completed(source, job, prior, full=True)
    target = exact_path(run/'canary'/job_key(job))
    target.parent.mkdir(parents=True, exist_ok=True)
    if not (run/'canary'/MARKER).exists():
        atomic_json(run/'canary'/MARKER, marker(freeze))
    if not target.exists():
        stage = exact_path(run/'canary_work'/job_key(job))
        stage.mkdir(parents=True, exist_ok=True)
        name = denoised_audio_name(job['channel'], freeze)
        wav = exact_path(stage/name)
        shutil.copy2(exact_path(denoised_audio_path(source, record)), wav)
        shutil.copyfile(exact_path(source/'cleanup.jsonl'), exact_path(stage/'cleanup.jsonl'))
        audio = dict(record['audio'], filename=name, mtime_ns=wav.stat().st_mtime_ns)
        adopted = dict(record, run_id=freeze['run_id'], frozen=freeze, audio=audio,
                       adopted_from_run=prior_id)
        atomic_json(stage/'denoised.json', adopted)
        verify_completed(stage, job, freeze, full=True)
        publish(stage, target)
    if verify_completed(target, job, freeze, full=True)['audio']['sha256'] != record['audio']['sha256']:
        raise IdentityError('The adopted canary audio differs from the approved WAV')
    report = collect_canary(jobs, run, freeze, review)
    atomic_json(run/'canary.json', report)
    atomic_json(run/'approval.json', dict(report, status='accepted',
        approved_at_unix=approval['approved_at_unix'], adopted_from_run=prior_id,
        prior_approval_sha256=sha256(approval_path), approved_audio_sha256=record['audio']['sha256']))
    atomic_json(run/'supersede.json', dict(prior_run_id=prior_id))
    print(f'Preserved CH20 listening approval for the unchanged WAV in {freeze["run_id"]}', flush=True)


def adopt_filename_only_benchmark(freeze, run, benchmark_id, prior_id):
    if not re.fullmatch(r'v2-[0-9a-f]{24}', benchmark_id):
        raise IdentityError('Expected a prior all-channel benchmark run ID')
    folder = exact_path(CONTROL/benchmark_id)
    prior = read_json(folder/'frozen.json')
    if (prior['run_id'] != benchmark_id or 'audio_naming' in prior
            or any(prior[key] != freeze[key] for key in ('algorithm', 'inventory_sha256', 'review_sha256',
                   'settings', 'normalization', 'versions', 'channel_policy'))
            or any(prior['implementation_sha256'][name] != freeze['implementation_sha256'][name]
                   for name in ('denoise_v2.py', 'denoise_v2_refinement.py', 'render_denoise_v2.py',
                                'denoise_v2_gain.py', 'normalization.py', 'denoise_v2_quality.py'))
            or read_json(folder/'supersede.json')['prior_run_id'] != prior_id):
        raise IdentityError('Prior benchmark does not match this all-channel run and DSP')
    reports = sorted(folder.glob('benchmark_*t_*w.json'))
    if not reports:
        raise IdentityError('No prior benchmark reports found')
    for path in reports:
        benchmark = read_json(path)
        if benchmark['run_id'] != benchmark_id:
            raise IdentityError('Benchmark report belongs to another run')
        atomic_json(run/path.name, dict(benchmark, run_id=freeze['run_id'],
                    adopted_from_run=benchmark_id, prior_benchmark_sha256=sha256(path)))
    print(f'Carried forward the all-channel benchmark from {benchmark_id}', flush=True)


def supersede_timing_copy_root(jobs, freeze, run):
    """Quarantine the stopped output tree and hand the recorded v1 move to the new run."""
    prior_id = read_json(run/'supersede.json')['prior_run_id']
    if not re.fullmatch(r'v2-[0-9a-f]{24}', prior_id):
        raise IdentityError('Unexpected prior run ID in the cutover record')
    transaction_path = CONTROL/TRANSACTION
    transaction = read_json(transaction_path)
    prior_run = exact_path(CONTROL/prior_id)
    prior = read_json(prior_run/'frozen.json')
    if (transaction['run_id'] != prior_id or transaction['status'] != 'complete'
            or (transaction['from'], transaction['to']) != (str(CANONICAL), str(ARCHIVE))):
        raise IdentityError('The stopped timing-copy run does not own the recorded cutover')
    snapshot = CONTROL/('legacy_'+prior_id+'.json')
    digest = transaction['snapshot_sha256']
    if sha256(snapshot) != digest or archive_snapshot(exact_path(ARCHIVE)) != read_json(snapshot):
        raise IdentityError('The v1 archive differs from the recorded migration inventory')
    quarantine = exact_path(CANONICAL.with_name('denoised_quarantine_'+prior_id))
    if CANONICAL.exists():
        if quarantine.exists() or read_json(CANONICAL/MARKER) != marker(prior):
            raise IdentityError('The prior v2 tree or quarantine does not match the transition')
        old_jobs = {job_key(job): job for job in jobs}
        canary = job_key(canary_jobs(jobs)[0])
        for path in CANONICAL.glob('*/CH*/denoised.json'):
            key = path.parent.relative_to(CANONICAL)
            job = old_jobs.get(key)
            if job is None or (key != canary and job['source']['role'] != 'timing_only'):
                raise IdentityError(f'The prior run has additional completed denoise work: {key}')
            old_job = job if key == canary else dict(job, action=TIMING)
            verify_completed(path.parent, old_job, prior)
        require_closed_files(CANONICAL, [p.relative_to(CANONICAL) for p in CANONICAL.rglob('*') if p.is_file()])
        exact_path(CANONICAL).rename(quarantine)
    if read_json(quarantine/MARKER) != marker(prior):
        raise IdentityError('The quarantined v2 tree does not match the stopped run')
    copied_snapshot = CONTROL/('legacy_'+freeze['run_id']+'.json')
    shutil.copyfile(snapshot, copied_snapshot)
    if sha256(copied_snapshot) != digest:
        raise IdentityError('The new run did not inherit the verified v1 archive inventory')
    atomic_json(prior_run/'migration_before_supersede.json', transaction)
    atomic_json(transaction_path, dict(transaction, status='prepared', run_id=freeze['run_id'],
                                     prior_run_id=prior_id, prior_v2_quarantine=str(quarantine)))
    print(f'Preserved the stopped timing-copy run in {quarantine}', flush=True)


def run_scheduler(args, inventory, freeze, run, thresholds, selected):
    root, work = CANONICAL, run/'work'
    jobs = inventory['jobs']
    report_path = run/'status.json'
    statuses = read_json(report_path)['jobs'] if report_path.exists() else {}
    active = {}
    queue = defaultdict(deque)
    previous_run_id = getattr(args, 'previous_run_id', None)
    for job in selected:
        name = job_key(job).as_posix()
        final, stage = root/job_key(job), work/job_key(job)
        if final.exists() and not pending_in_place(final, stage, previous_run_id):
            try:
                validate_source(job)
                validate_hint(job, ARCHIVE)
                saved = verify_completed(root/job_key(job), job, freeze)
            except (OSError, ValueError, KeyError, RuntimeError) as exc:
                statuses[name] = dict(status='failed_verification', action=action(job), error=str(exc))
                atomic_json(report_path, dict(run_id=freeze['run_id'], stop_reason=str(exc), jobs=statuses))
                raise
            statuses[name] = dict(status='complete', reused=True, action=action(job), quality=saved['quality'])
        else:
            queue[job['tape']].append(job)
            statuses[name] = dict(status='pending', action=action(job))
    disk = disk_check(root, work, jobs, args.workers)
    memory_guard(args.workers)
    stopped, systemic = None, 0
    opened = []
    elapsed = time.monotonic()
    stop = run/'stop_requested'
    stop.unlink(missing_ok=True)

    def summary():
        complete = sum(v['status'] == 'complete' for v in statuses.values())
        worker_seconds = sum(v.get('worker_seconds', 0) for v in statuses.values())
        denoise_hours = sum(v.get('denoise_hours', 0) for v in statuses.values())
        atomic_json(report_path, dict(run_id=freeze['run_id'], counts=inventory['counts'],
            tape_limit=args.tapes, worker_limit=args.workers, complete=complete,
            stop_reason=stopped, status_counts=dict(Counter(v['status'] for v in statuses.values())),
            actions={kind:dict(Counter(v['status'] for v in statuses.values() if v['action'] == kind))
                     for kind in (DENOISE, TIMING)}, jobs=statuses, disk=disk,
            missing={t['tape']:t['missing_channels'] for t in inventory['tapes'] if t['missing_channels']},
            excluded=[f for t in inventory['tapes'] for f in t['excluded_files']],
            elapsed_seconds=time.monotonic()-elapsed,
            denoise_channel_hours=denoise_hours,
            channel_hours_per_worker_hour=denoise_hours/(worker_seconds/3600) if worker_seconds else None))

    review = REVIEW/'CH20_30min/manifest.json'
    summary()
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        try:
            while queue or active:
                opened = [t for t in opened if queue.get(t) or any(j['tape'] == t for j in active.values())]
                for tape in queue:
                    if len(opened) == args.tapes:
                        break
                    if tape not in opened:
                        opened.append(tape)
                while queue and not stopped and len(active) < args.workers:
                    available = [t for t in opened if queue.get(t)]
                    if not available:
                        break
                    tape = min(available, key=lambda t: sum(j['tape'] == t for j in active.values()))
                    try:
                        if freeze_run(args.inventory, review) != freeze:
                            raise IdentityError('Frozen implementation or inventory changed')
                        if read_json(root/MARKER) != marker(freeze):
                            raise IdentityError('Production root marker changed')
                        if shutil.disk_usage(root).free < RESERVE:
                            raise IdentityError('Disk reserve exhausted')
                    except (OSError, ValueError) as exc:
                        stopped = str(exc)
                        break
                    job = queue[tape].popleft()
                    if not queue[tape]:
                        del queue[tape]
                    name = job_key(job).as_posix()
                    statuses[name]['status'] = 'running'
                    print(f'Starting {name} ({statuses[name]["action"]})', flush=True)
                    active[pool.submit(invoke, job, freeze, root, work, ARCHIVE, thresholds,
                                       args.mode == 'resume', args.inventory, review, stop,
                                       previous_run_id=previous_run_id)] = job
                summary()
                if not active:
                    break
                done, _ = wait(active, return_when=FIRST_COMPLETED)
                for future in done:
                    job = active.pop(future)
                    name = job_key(job).as_posix()
                    row = statuses[name]
                    try:
                        result = future.result()
                        row.update(status='complete', quality=result['quality'])
                        if action(job) == DENOISE:
                            row.update(worker_seconds=result['worker_seconds'],
                                       denoise_hours=job['source']['duration_seconds']/3600)
                        systemic = 0
                    except Exception as exc:
                        systemic += 1
                        row.update(status='failed', error=f'{type(exc).__name__}: {exc}')
                        if isinstance(exc, (IdentityError, OSError)) or systemic >= 3:
                            stopped = row['error']
                    print(f'{name} {row["status"]} {row.get("error", "")}', flush=True)
        except KeyboardInterrupt:
            stopped = 'Interrupted; use resume'
            stop.touch()
            for future in active:
                future.cancel()
            raise
        finally:
            for row in statuses.values():
                if row['status'] == 'pending':
                    row['status'] = 'not_run'
                elif row['status'] == 'running':
                    row['status'] = 'interrupted'
            summary()
    return int(bool(stopped or any(statuses[job_key(j).as_posix()]['status'] != 'complete' for j in selected)))


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--version', choices=['v2'], required=True)
    p.add_argument('--mode', choices=['dry-run','canary','benchmark','approve-canary','adopt-run','full','resume','reconcile','rollback'],
                   default='dry-run')
    p.add_argument('--inventory', type=Path, default=INVENTORY)
    p.add_argument('--tapes', type=int, default=3, help='Maximum active tapes on the array')
    p.add_argument('--workers', type=int, default=3, help='Maximum channel processes across active tapes')
    p.add_argument('--benchmark-seconds', type=int, default=512)
    p.add_argument('--reuse-unity-canary', metavar='RUN_ID',
                   help='Normalize a compatible completed unity canary without repeating hum subtraction')
    p.add_argument('--prior-timing-copy-run', metavar='RUN_ID',
                   help='Adopt the approved CH20 WAV from a stopped timing-copy run')
    p.add_argument('--prior-benchmark-run', metavar='RUN_ID',
                   help='Reuse a matching all-channel benchmark from before the WAV filename change')
    p.add_argument('--readers-stopped', action='store_true',
                   help='Confirm that external audio readers and writers have closed the denoise tree')
    p.add_argument('--tape', help=argparse.SUPPRESS)
    p.add_argument('--channel', type=int, help=argparse.SUPPRESS)
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    if not 1 <= args.workers <= 61 or args.tapes < 1 or args.benchmark_seconds < 64:
        raise ValueError('Positive tape and worker limits and at least 64 benchmark seconds are required')
    if bool(args.tape) != bool(args.channel) or args.tape and args.mode != 'resume':
        raise ValueError('A single-channel selection requires resume and both tape and channel')
    if args.reuse_unity_canary and args.mode != 'canary':
        raise ValueError('--reuse-unity-canary requires --mode canary')
    if bool(args.prior_timing_copy_run) != (args.mode == 'adopt-run'):
        raise ValueError('--mode adopt-run requires --prior-timing-copy-run')
    if args.prior_benchmark_run and args.mode != 'adopt-run':
        raise ValueError('--prior-benchmark-run requires --mode adopt-run')
    for variable in THREAD_VARS:
        os.environ[variable] = '1'
    review = REVIEW/'CH20_30min/manifest.json'
    if args.mode == 'rollback':
        if not args.readers_stopped:
            raise ValueError('Close readers of the denoise tree and supply --readers-stopped')
        with archive_lock():
            rollback()
        return 0
    inventory = load_inventory(args.inventory)
    jobs = inventory['jobs']
    freeze = freeze_run(args.inventory, review)
    run = CONTROL/freeze['run_id']
    hints = ARCHIVE if ARCHIVE.is_dir() else CANONICAL
    if args.mode == 'dry-run':
        result = preflight(inventory, hints)
        result.update(run_id=freeze['run_id'], disk=disk_check(CANONICAL if hints == ARCHIVE else run/'new',
                                                               run/'work', jobs, args.workers))
        print(json.dumps(result, indent=2))
        return 0
    with archive_lock():
        hints = ARCHIVE if ARCHIVE.is_dir() else CANONICAL
        run.mkdir(parents=True, exist_ok=True)
        atomic_json(run/'frozen.json', freeze)
        if args.mode == 'canary':
            run_canary(args, jobs, freeze, run, hints, review)
            return 0
        if args.mode == 'benchmark':
            benchmark(args, jobs, freeze, run, hints)
            return 0
        if args.mode == 'adopt-run':
            adopt_timing_copy_run(jobs, freeze, run, args.prior_timing_copy_run, review)
            if args.prior_benchmark_run:
                adopt_filename_only_benchmark(freeze, run, args.prior_benchmark_run, args.prior_timing_copy_run)
            return 0
        if args.mode == 'approve-canary':
            report = collect_canary(jobs, run, freeze, review)
            atomic_json(run/'approval.json', dict(report, status='accepted', approved_at_unix=time.time()))
            return 0
        if args.mode == 'reconcile':
            return reconcile(inventory, run, freeze)
        if not (run/'approval.json').is_file():
            raise IdentityError('Run and review the canary, then use --mode approve-canary before production')
        approved = read_json(run/'approval.json')
        if approved['run_id'] != freeze['run_id'] or approved['status'] != 'accepted':
            raise IdentityError('An accepted canary for the frozen run is required')
        benchmarks = [read_json(path) for path in run.glob('benchmark_*t_*w.json')]
        if not any(report['run_id'] == freeze['run_id'] and args.workers <= report['results'][-1]['workers']
                   and min(args.tapes, args.workers) <= report['results'][-1]['tapes'] for report in benchmarks):
            raise IdentityError('Benchmark the proposed tape and worker limits before increasing production concurrency')
        transaction = CONTROL/TRANSACTION
        previous = read_json(transaction) if transaction.exists() else {}
        different_run = bool(previous and previous['run_id'] != freeze['run_id'])
        if different_run and not (run/'supersede.json').is_file():
            raise IdentityError('The canonical root belongs to another run')
        if different_run or previous.get('status') != 'complete':
            if not args.readers_stopped or args.tape:
                raise ValueError('For cutover, close all legacy writers and audio readers and supply --readers-stopped')
            print('Checking frozen sources, frequency hints, and available disk space', flush=True)
            preflight(inventory, hints)
            disk_check(run/'new', run/'work', jobs, args.workers)
        for job in canary_jobs(jobs):
            final, staged = CANONICAL/job_key(job), run/'canary'/job_key(job)
            if staged.exists():
                saved = verify_completed(staged, job, freeze, full=True)
                if action(job) == DENOISE and sha256(staged/'denoised.json') != approved['checks']['manifest_sha256']:
                    raise IdentityError('Approved canary manifest changed')
            elif not final.exists() or not (CANONICAL/MARKER).exists():
                raise IdentityError('Approved canary output is missing')
        if different_run:
            print('Verifying and preserving the stopped run before creating the new output root', flush=True)
            supersede_timing_copy_root(jobs, freeze, run)
        rotate(freeze)
        for job in canary_jobs(jobs):
            final, staged = CANONICAL/job_key(job), run/'canary'/job_key(job)
            if not final.exists():
                publish(staged, final)
        selected = [j for j in jobs if not args.tape or (j['tape'], j['channel']) == (args.tape, args.channel)]
        if not selected:
            raise ValueError('Requested channel is absent from the frozen inventory')
        return run_scheduler(args, inventory, freeze, run, approved['checks']['thresholds'], selected)


def cli():
    try:
        return main()
    except (OSError, ValueError, KeyError, RuntimeError) as exc:
        print(f'Rollout stopped: {exc}', file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


def benchmark_channel(job, scratch, hints, seconds):
    """Time the model and endpoint-aware float output against the archive volume."""
    import tempfile
    from denoise_archive import sync
    from denoise_v2 import corrected_chunks
    from denoise_v2_quality import residual_windows
    from render_denoise_v2 import gather_seeds, saved_seeds

    source, info = validate_source(job)
    hint = validate_hint(job, hints)
    rate = info.samplerate
    start = 13200*rate if job['tape'] == CANARY_TAPE and job['channel'] == 20 else info.frames//2
    start = (start//(rate*64))*(rate*64)
    end = min(info.frames, start+seconds*rate)
    seeds = saved_seeds(hint, 0, info.frames)
    begin = time.monotonic()
    with tempfile.TemporaryDirectory(dir=scratch) as folder:
        unity = Path(folder)/'unity.wav'
        frames = end-start
        header = wav_header(frames, rate)
        fmt = 'RF64' if header[:4] == b'RF64' else 'WAV'
        digest, decoded = hashlib.sha256(header), hashlib.sha256()
        with (unity.open('wb') as stream, tempfile.TemporaryFile(dir=scratch) as journal,
              sf.SoundFile(source) as original):
            def read(frame, count):
                original.seek(frame)
                return original.read(count, dtype='float64')

            stream.write(header)
            for frame, raw, y, left, right in corrected_chunks(
                    read, info.frames, start, end, lambda a, b: gather_seeds(seeds, a, b)):
                block = y.astype('<f4')
                evidence = residual_windows(raw, block, rate, left, right, None, frame)
                journal.write(json.dumps(evidence).encode('utf-8'))
                stream.write(block.tobytes())
                digest.update(block)
                decoded.update(block)
                sync(stream)
                sync(journal)
        if sha256(unity) != digest.hexdigest():
            raise IdentityError('Benchmark scratch verification failed')
        normalize_audio(unity, Path(folder)/denoised_audio_name(job['channel'], OUTPUT_NAMING),
                        dict(frames=frames, sample_rate=rate, format=fmt, sha256=digest.hexdigest(),
                             decoded_float32_sha256=decoded.hexdigest()), gain_configuration())
    return dict(channel=job_key(job).as_posix(), source_seconds=(end-start)/rate,
                wall_seconds=time.monotonic()-begin, peak_rss_bytes=peak_memory())


def peak_memory():
    if os.name != 'nt':
        import resource
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*(1 if sys.platform == 'darwin' else 1024)
    import ctypes
    from ctypes import wintypes
    class Counters(ctypes.Structure):
        _fields_ = [('cb', wintypes.DWORD), ('faults', wintypes.DWORD)] + [
            (name, ctypes.c_size_t) for name in ('peak', 'current', 'quota_peak_paged', 'quota_paged',
            'quota_peak_nonpaged', 'quota_nonpaged', 'pagefile', 'peak_pagefile')]
    counters = Counters()
    counters.cb = ctypes.sizeof(counters)
    ctypes.windll.kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    if ctypes.windll.psapi.GetProcessMemoryInfo(ctypes.c_void_p(ctypes.windll.kernel32.GetCurrentProcess()),
                                               ctypes.byref(counters), counters.cb):
        return counters.peak
    return None


def benchmark(args, jobs, freeze, run, hints):
    memory_guard(args.workers)
    scratch = run/'benchmark_scratch'
    scratch.mkdir(exist_ok=True)
    disk_check(scratch, scratch, [])
    primary = next(job for job in jobs if job['tape'] == CANARY_TAPE and job['channel'] == 20)
    by_tape = defaultdict(list)
    for job in jobs:
        if action(job) == DENOISE and job != primary:
            by_tape[job['tape']].append(job)
    tapes = [CANARY_TAPE]+[t for t in by_tape if t != CANARY_TAPE][:args.tapes-1]
    chosen = [primary]
    timing_tape = tapes[min(1, len(tapes)-1)]
    timing_tested = False
    while len(chosen) < args.workers:
        for tape in [*tapes[1:], tapes[0]]:
            if by_tape[tape]:
                index = len(by_tape[tape])//2
                if tape == timing_tape and not timing_tested:
                    index = next((i for i, job in enumerate(by_tape[tape])
                                  if job['source']['role'] == 'timing_only'), index)
                    timing_tested = True
                chosen.append(by_tape[tape].pop(index))
            if len(chosen) == args.workers:
                break
        else:
            if not any(by_tape[tape] for tape in tapes):
                raise ValueError('Too many workers for the selected benchmark tapes')
    results = []
    for label, group in (('single', [primary]), ('concurrent', chosen)):
        begin = time.monotonic()
        with ProcessPoolExecutor(max_workers=len(group)) as pool:
            futures = [pool.submit(benchmark_channel, job, scratch, hints, args.benchmark_seconds) for job in group]
            rows = [future.result() for future in futures]
        wall = time.monotonic()-begin
        audio = sum(row['source_seconds'] for row in rows)
        results.append(dict(mode=label, workers=len(group), tapes=len({j['tape'] for j in group}),
                            wall_seconds=wall, channels=rows, channel_hours_per_wall_hour=audio/wall,
                            channel_hours_per_worker_hour=audio/sum(row['wall_seconds'] for row in rows),
                            estimated_archive_days=sum(j['source']['duration_seconds'] for j in jobs)/3600/(audio/wall)/24))
    report = dict(run_id=freeze['run_id'], sample_seconds=args.benchmark_seconds,
                  results=results, note='Excerpt-based scaling estimate; costs vary by channel and tape.')
    atomic_json(run/f'benchmark_{args.tapes}t_{args.workers}w.json', report)
    print(json.dumps(report, indent=2))


def reconcile(inventory, run, freeze):
    if read_json(CANONICAL/MARKER) != marker(freeze):
        raise IdentityError('Production root does not match the run')
    jobs = {}
    for job in inventory['jobs']:
        folder = CANONICAL/job_key(job)
        work = run/'work'/job_key(job)
        record = dict(action=action(job))
        try:
            if folder.exists():
                validate_source(job)
                validate_hint(job, ARCHIVE)
                saved = verify_completed(folder, job, freeze, full=True)
                record.update(status='complete', audio=saved['audio'], quality=saved['quality'],
                              manifest_sha256=sha256(folder/'denoised.json'))
                if action(job) == TIMING and saved['audio']['sha256'] != sha256(job['source']['path']):
                    raise IdentityError('Timing copy differs from original')
            elif (work/'denoised.json').exists():
                saved = read_json(work/'denoised.json')
                record.update(status='restartable', staging_status=saved['status'], error=saved.get('error'))
            else:
                record['status'] = 'not_run'
        except (OSError, ValueError, KeyError, RuntimeError) as exc:
            record.update(status='failed_verification', error=str(exc))
        jobs[job_key(job).as_posix()] = record
    expected = set(jobs)
    unexpected = sorted(folder.relative_to(CANONICAL).as_posix()
                        for folder in CANONICAL.glob('*/CH*') if folder.is_dir()
                        and folder.relative_to(CANONICAL).as_posix() not in expected)
    counts = Counter(row['status'] for row in jobs.values())
    report = dict(frozen=freeze, expected_counts=inventory['counts'], counts=dict(counts), jobs=jobs,
                  unexpected_channels=unexpected, acoustic_status='review_required',
                  missing={t['tape']:t['missing_channels'] for t in inventory['tapes'] if t['missing_channels']},
                  excluded=[f for t in inventory['tapes'] for f in t['excluded_files']])
    atomic_json(run/'reconciliation.json', report)
    print(json.dumps(dict(counts), indent=2))
    return int(counts['complete'] != len(jobs) or bool(unexpected))


if __name__ == '__main__':
    raise SystemExit(cli())
