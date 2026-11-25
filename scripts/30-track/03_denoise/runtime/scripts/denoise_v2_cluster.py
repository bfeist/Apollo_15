"""One Windows coordinator, local workers and SSH workers on Genus.

The frozen production modules are deliberately unchanged. Remote execution has
its own identity and SSD checkpoints; Windows validates and adopts its completed
audio under the production channel lock. No remote process writes production.
"""
import os

for _name in ('OPENBLAS_NUM_THREADS', 'MKL_NUM_THREADS', 'OMP_NUM_THREADS',
              'BLIS_NUM_THREADS', 'NUMEXPR_NUM_THREADS', 'VECLIB_MAXIMUM_THREADS'):
    os.environ[_name] = '1'

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from copy import deepcopy
import hashlib
import json
from pathlib import Path, PurePosixPath, PureWindowsPath
import platform
import queue
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time

import numpy as np
import scipy
import soundfile as sf

from denoise_archive import (ARCHIVE, BASE, CANONICAL, CONTROL, MARKER,
                            ORIGINALS, archive_lock, atomic_json, exact_path,
                            read_json, sha256)
from denoise_v2 import corrected_chunks
from denoise_v2_background import lower_priority
from denoise_v2_production import (STEP, IdentityError, job_key, new_record,
                                  output_metrics, publish, validate_hint,
                                  validate_source, verify_completed, wav_header,
                                  write_channel)
from denoise_v2_rollout import (INVENTORY, REVIEW, disk_check, freeze_run,
                               load_inventory, marker, memory_guard)
from flutter_plan_store import planning_lock
from render_denoise_v2 import gather_seeds, saved_seeds

SCRIPT = Path(__file__).resolve()
REPO = SCRIPT.parent.parent
MAX_ERROR = 2**-22
RMS_ERROR = 2**-25
EMIT_LOCK = threading.Lock()


def digest_json(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def versions():
    return dict(python=sys.version, numpy=np.__version__, scipy=scipy.__version__,
                soundfile=sf.__version__, libsndfile=sf.__libsndfile_version__)


def mapped_job(job, data_root):
    """Map only the known original root, retaining every other identity field."""
    value = deepcopy(job)
    source = PureWindowsPath(job['source']['path'])
    relative = source.relative_to(PureWindowsPath('D:/Apollo_15_30-Track/orig_concatenated'))
    if len(relative.parts) != 2 or relative.parts[0] != job['tape']:
        raise IdentityError('Source is outside its canonical tape directory')
    root_type = PurePosixPath if str(data_root).startswith('/') else Path
    value['source']['path'] = str(root_type(data_root)/'orig_concatenated'/root_type(*relative.parts))
    return value


def check_code(config):
    frozen = config['frozen']
    if sha256(SCRIPT) != config['adapter_sha256']:
        raise IdentityError('Cluster launcher changed; stop workers before preparing a new session')
    for name, expected in frozen['implementation_sha256'].items():
        if sha256(SCRIPT.parent/name) != expected:
            raise IdentityError(f'Frozen implementation changed: {name}')
    if sha256(INVENTORY) != frozen['inventory_sha256']:
        raise IdentityError('Frozen inventory changed')
    if sha256(REVIEW/'CH20_30min/manifest.json') != frozen['review_sha256']:
        raise IdentityError('Frozen review changed')
    if digest_json(read_json(session_path(config)/'jobs.json')) != config['jobs_sha256']:
        raise IdentityError('Cluster job inventory changed')
    installed = versions()
    for name in ('numpy', 'scipy', 'soundfile', 'libsndfile'):
        if installed[name] != frozen['versions'][name]:
            raise IdentityError(f'{name} differs from the production environment')


def remote_freeze(config):
    frozen = deepcopy(config['frozen'])
    frozen.pop('run_id')
    frozen['versions'] = versions()
    frozen['remote_execution'] = dict(parent_run_id=config['frozen']['run_id'],
        adapter_sha256=config['adapter_sha256'], data_root=config['remote_data'],
        hostname=platform.node())
    frozen['run_id'] = 'remote-'+digest_json(frozen)[:24]
    return frozen


def scratch_paths(config, frozen):
    scratch = exact_path(Path(config['scratch'])/frozen['run_id'])
    return scratch, scratch/'output', scratch/'work'


def check_scratch(config, frozen):
    scratch, root, work = scratch_paths(config, frozen)
    if not (root/MARKER).is_file() or read_json(root/MARKER) != marker(frozen):
        raise IdentityError('SSD scratch is not prepared for this qualified run')
    return scratch, root, work


def emit(**value):
    with EMIT_LOCK:
        print(json.dumps(dict(at=time.time(), **value), allow_nan=False), flush=True)


def session_path(config):
    base = BASE if os.name == 'nt' else Path(config['remote_data'])
    return base/'.denoise_v2_rollout'/config['frozen']['run_id']/'cluster'


def remote_command(config, *arguments):
    command = [config['remote_uv'], 'run', '--no-project', '--python', config['remote_python'],
               str(PurePosixPath(config['remote_repo'])/'scripts'/SCRIPT.name), *map(str, arguments)]
    # exec makes SSH's process lifetime follow the worker, without an extra shell.
    return ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
            '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=3',
            config['ssh'], 'exec '+shlex.join(command)]


def probe_audio(job, data_root, intervals):
    actual = mapped_job(job, data_root) if os.name != 'nt' else job
    source, info = validate_source(actual, Path(data_root)/'orig_concatenated')
    hint = validate_hint(actual, Path(data_root)/'denoised_v1')
    seeds = saved_seeds(hint, 0, info.frames)
    with sf.SoundFile(source) as stream:
        def read(frame, count):
            stream.seek(frame)
            return stream.read(count, dtype='float64')
        for start, end in intervals:
            arrays, contexts = [], []
            for _, _, result, left, right in corrected_chunks(
                    read, info.frames, start, end, lambda a, b: gather_seeds(seeds, a, b)):
                arrays.append(result.astype('<f4'))
                contexts.append([left['context_float64_sha256'], right['context_float64_sha256']])
            yield np.concatenate(arrays), contexts


def compare_probe(expected, actual):
    if expected.shape != actual.shape or not np.isfinite(actual).all():
        raise IdentityError('Invalid remote qualification samples')
    difference = expected.astype('float64')-actual.astype('float64')
    maximum, rms = float(np.max(abs(difference))), float(np.sqrt(np.mean(difference**2)))
    if maximum > MAX_ERROR or rms > RMS_ERROR:
        raise IdentityError(f'Linux differs from Windows: max={maximum:g}, RMS={rms:g}')
    return dict(maximum_error=maximum, rms_error=rms)


def qualify(config):
    check_code(config)
    folder = session_path(config)
    probe = read_json(folder/'probe.json')
    results = []
    for index, (audio, contexts) in enumerate(probe_audio(
            probe['job'], config['remote_data'], probe['intervals'])):
        reference = folder/f'windows-{index}.npy'
        if sha256(reference) != probe['files'][index]['sha256']:
            raise IdentityError('Windows qualification reference changed')
        if contexts != probe['files'][index]['contexts']:
            raise IdentityError('Decoded original context differs across mounts')
        result = compare_probe(np.load(reference, allow_pickle=False), audio)
        target = folder/f'genus-{index}.npy'
        np.save(target, audio, allow_pickle=False)
        results.append(dict(result, sha256=sha256(target)))
        emit(event='qualification', interval=index, **result)
    frozen = remote_freeze(config)
    scratch, root, work = scratch_paths(config, frozen)
    root.mkdir(parents=True, exist_ok=True)
    # Initialization belongs to this single preparation process, never to workers.
    with planning_lock(scratch/'initialize'):
        if (root/MARKER).exists() and read_json(root/MARKER) != marker(frozen):
            raise IdentityError('SSD scratch belongs to another run')
        atomic_json(root/MARKER, marker(frozen))
    atomic_json(folder/'qualification.json', dict(config_sha256=digest_json(config),
        probe_sha256=sha256(folder/'probe.json'), frozen=frozen,
        platform=platform.platform(), results=results))


def verify_qualification(config):
    folder = session_path(config)
    qualification = read_json(folder/'qualification.json')
    if (qualification['config_sha256'] != digest_json(config)
            or qualification['probe_sha256'] != sha256(folder/'probe.json')):
        raise IdentityError('Qualification does not belong to this session')
    probe = read_json(folder/'probe.json')
    if len(qualification['results']) != len(probe['files']):
        raise IdentityError('Incomplete qualification')
    for index, result in enumerate(qualification['results']):
        windows, genus = folder/f'windows-{index}.npy', folder/f'genus-{index}.npy'
        if sha256(windows) != probe['files'][index]['sha256'] or sha256(genus) != result['sha256']:
            raise IdentityError('Qualification audio changed')
        compare_probe(np.load(windows, allow_pickle=False), np.load(genus, allow_pickle=False))
    return qualification


def prepare(args):
    if os.name != 'nt':
        raise ValueError('Prepare the coordinator on Windows')
    frozen = freeze_run(INVENTORY, REVIEW/'CH20_30min/manifest.json')
    if read_json(CANONICAL/MARKER) != marker(frozen):
        raise IdentityError('Current source checkout does not match production')
    inventory = load_inventory(INVENTORY)
    config = dict(schema=1, frozen=frozen, adapter_sha256=sha256(SCRIPT),
        jobs_sha256=digest_json(inventory['jobs']),
        ssh=args.ssh, remote_data=args.remote_data, remote_repo=args.remote_repo,
        remote_uv=args.remote_uv, remote_python=args.remote_python, scratch=args.scratch)
    folder = session_path(config)
    folder.mkdir(parents=True, exist_ok=True)
    with archive_lock(), planning_lock(folder):
        if (folder/'config.json').exists() and read_json(folder/'config.json') != config:
            raise IdentityError('Existing cluster configuration differs; preserve it before starting a new session')
        atomic_json(folder/'config.json', config)
        atomic_json(folder/'jobs.json', inventory['jobs'])
        if not (folder/'probe.json').exists():
            job = next(j for j in inventory['jobs'] if (j['tape'], j['channel']) == ('A15_T510083', 20))
            review = read_json(REVIEW/'CH20_30min/manifest.json')
            total = job['source']['frames']
            positions = [0, review['source_frames'][0]+564*8000,
                         review['source_frames'][0]+1530*8000, total-STEP]
            intervals = [[p//STEP*STEP, min(p//STEP*STEP+STEP, total)] for p in positions]
            files = []
            for index, (audio, contexts) in enumerate(probe_audio(job, BASE, intervals)):
                target = folder/f'windows-{index}.npy'
                np.save(target, audio, allow_pickle=False)
                files.append(dict(sha256=sha256(target), contexts=contexts))
                print(f'Windows qualification {index+1}/{len(intervals)} complete', flush=True)
            atomic_json(folder/'probe.json', dict(job=job, intervals=intervals, files=files))
        remote_config = str(PurePosixPath(args.remote_data)/'.denoise_v2_rollout'/frozen['run_id']/'cluster/config.json')
        subprocess.run(remote_command(config, 'qualify', '--config', remote_config), check=True)
        result = verify_qualification(config)
        print(f'Qualified {args.ssh}: {result["frozen"]["run_id"]}', flush=True)
        print(f'Configuration: {folder/"config.json"}', flush=True)


def release_acknowledged(config, root):
    """Reclaim only SSD results acknowledged by Windows, under their worker lock."""
    for ack in (session_path(config)/'acks').glob('*/CH*.json'):
        key = Path(ack.parent.name)/ack.stem
        completed = exact_path(root/key)
        if not completed.is_dir():
            continue
        try:
            with planning_lock(root.parent/'work'/key):
                manifest = completed/'denoised.json'
                if not manifest.exists():
                    continue
                record = read_json(manifest)
                if read_json(ack)['audio_sha256'] != record['audio']['sha256']:
                    raise IdentityError('Acknowledgement differs from SSD output')
                if root not in completed.parents:
                    raise IdentityError('SSD cleanup escaped output root')
                # Never follow redirects or remove unknown files.
                allowed = {record['audio']['filename'], 'denoised.json', 'cleanup.jsonl'}
                if {p.name for p in completed.iterdir()} != allowed:
                    raise IdentityError(f'Unexpected SSD output files: {completed}')
                for name in allowed:
                    exact_path(completed/name).unlink()
                completed.rmdir()
        except ValueError as exc:
            if 'Another planner' not in str(exc):
                raise


def worker(config, key, host):
    check_code(config)
    folder = session_path(config)
    jobs = read_json(folder/'jobs.json')
    job = next(j for j in jobs if job_key(j).as_posix() == key)
    run = folder.parent
    stop = folder/'stop_requested'
    approval = read_json(run/'approval.json') if (run/'approval.json').exists() else {}
    thresholds = approval.get('checks', {}).get('thresholds')
    if host == 'windows':
        if os.name != 'nt':
            raise IdentityError('Windows worker must run on Windows')
        lower_priority()
        frozen, root, work, hints, originals = config['frozen'], CANONICAL, run/'work', ARCHIVE, ORIGINALS
        prior = read_json(run/'supersede.json').get('prior_run_id') if (run/'supersede.json').exists() else None
    else:
        if os.name == 'nt':
            raise IdentityError('Genus worker must run on Linux')
        qualification = verify_qualification(config)
        frozen = remote_freeze(config)
        if frozen != qualification['frozen']:
            raise IdentityError('Genus environment changed after qualification')
        job = mapped_job(job, config['remote_data'])
        scratch, root, work = check_scratch(config, frozen)
        hints, originals = Path(config['remote_data'])/'denoised_v1', Path(config['remote_data'])/'orig_concatenated'
        prior = None
        release_acknowledged(config, root)
        # Capacity for this channel's unity and normalized WAV plus headroom.
        if shutil.disk_usage(scratch).free < 2*job['expected_audio_bytes']+(20 << 30):
            raise IdentityError('Genus SSD has insufficient space')
    done = threading.Event()
    phase = ['rendering']
    def heartbeat():
        while not done.is_set():
            try:
                checkpoint = work/job_key(job)/'checkpoint.json'
                cursor = read_json(checkpoint)['cursor'] if checkpoint.exists() else 0
                emit(event='progress', host=host, key=key, cursor=cursor,
                     frames=job['source']['frames'], phase=phase[0])
            except (OSError, ValueError):
                pass  # An atomic checkpoint replacement or disconnected console.
            done.wait(10)
    thread = threading.Thread(target=heartbeat, daemon=True)
    thread.start()
    try:
        result = write_channel(job, frozen, root, work, hints, thresholds,
            originals=originals, resume=True, stop=stop, previous_run_id=prior,
            progress=lambda message: emit(event='message', host=host, key=key, message=message),
            verify_freeze=lambda: check_code(config))
        if host == 'genus':
            phase[0] = 'uploading'
            source = root/job_key(job)
            inbox = folder/'inbox'/job_key(job)
            pending = inbox.with_name(inbox.name+'.upload')
            inbox.parent.mkdir(parents=True, exist_ok=True)
            if not inbox.exists():
                pending.mkdir(exist_ok=True)
                for name in (f'CH{job["channel"]:02d}.wav', 'cleanup.jsonl', 'denoised.json'):
                    shutil.copyfile(source/name, pending/name)
                # Copying changes mtime. Preserve the original execution manifest separately.
                shutil.copyfile(source/'denoised.json', pending/'genus.execution.json')
                result['audio']['mtime_ns'] = (pending/f'CH{job["channel"]:02d}.wav').stat().st_mtime_ns
                atomic_json(pending/'denoised.json', result)
                pending.rename(inbox)
            # SSD files remain until Windows has verified and acknowledged publication.
        emit(event='complete', host=host, key=key, worker_seconds=result.get('worker_seconds', 0))
    finally:
        done.set()
        thread.join(timeout=2)


def adopt_remote(config, job, qualification):
    """Called only by the Windows coordinator while it owns archive_lock."""
    if os.name != 'nt':
        raise IdentityError('Only Windows can publish remote audio')
    folder = session_path(config)
    frozen, run = config['frozen'], folder.parent
    key = job_key(job)
    inbox, final, stage = folder/'inbox'/key, CANONICAL/key, run/'work'/key
    with planning_lock(stage):
        if read_json(CANONICAL/MARKER) != marker(frozen):
            raise IdentityError('Production root changed')
        validate_source(job)
        validate_hint(job, ARCHIVE)
        if (final/'denoised.json').exists() and read_json(final/'denoised.json').get('run_id') == frozen['run_id']:
            result = verify_completed(final, job, frozen)
            acknowledge(folder, key, result)
            return result
        remote = read_json(inbox/'denoised.json')
        actual_job = mapped_job(job, config['remote_data'])
        expected = new_record(actual_job, qualification['frozen'], True)
        expected['hint_provenance']['relative_path'] = (job_key(job)/'cleanup.jsonl').as_posix()
        expected.pop('gain')  # Verified independently by verify_completed after adoption.
        if remote.get('status') != 'complete' or any(remote.get(k) != v for k, v in expected.items()):
            raise IdentityError('Remote manifest does not match assigned job/environment')
        if sha256(inbox/'cleanup.jsonl') != remote['cleanup_log_sha256']:
            raise IdentityError('Remote cleanup log changed')
        audio = remote['audio']
        name = f'CH{job["channel"]:02d}.wav'
        verified = output_metrics(inbox/name,
            (job['source']['frames'], job['source']['sample_rate'], 1, 'FLOAT', audio['format']),
            expected_hash=audio['sha256'], expected_decoded=audio['decoded_float32_sha256'],
            header=wav_header(job['source']['frames'], job['source']['sample_rate'], audio['format'] == 'RF64'))
        # Canonical binding is the reviewed processing contract. Keep the actual
        # Linux execution identity explicitly, rather than pretending it was Windows.
        adopted = dict(remote, source=deepcopy(job['source']), frozen=frozen, run_id=frozen['run_id'])
        adopted['signature'] = new_record(job, frozen, True)['signature']
        adopted['hint_provenance']['relative_path'] = str(key/'cleanup.jsonl')
        adopted['audio'] = dict(verified, filename=name)
        adopted['execution'] = dict(host='genus', frozen=qualification['frozen'],
            qualification_sha256=sha256(folder/'qualification.json'),
            original_manifest_sha256=sha256(inbox/'genus.execution.json'), adapter_sha256=config['adapter_sha256'])
        # Keep the inbox immutable; a Windows-owned adoption directory is restartable.
        prepared = folder/'adopting'/key
        prepared.mkdir(parents=True, exist_ok=True)
        for filename in (name, 'cleanup.jsonl', 'genus.execution.json'):
            destination = prepared/filename
            if not destination.exists():
                destination.hardlink_to(inbox/filename)
        atomic_json(prepared/'denoised.json', adopted)
        verify_completed(prepared, job, frozen)
        previous = read_json(run/'supersede.json').get('prior_run_id') if (run/'supersede.json').exists() else None
        # publish_into_existing removes its known files; retain provenance outside that directory too.
        evidence = folder/'executions'/key
        evidence.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(inbox/'genus.execution.json', evidence/'genus.execution.json')
        (prepared/'genus.execution.json').unlink()
        publish(prepared, final, previous)
        result = verify_completed(final, job, frozen)
        acknowledge(folder, key, result)
        return result


def acknowledge(folder, key, result):
    ack = folder/'acks'/key.with_suffix('.json')
    ack.parent.mkdir(parents=True, exist_ok=True)
    atomic_json(ack, dict(audio_sha256=result['audio']['sha256']))


def select_job(pending, active, host, tape_limit, owners):
    """Global tape cap, balanced channels, and checkpoint affinity across resumes."""
    opened = {row['job']['tape'] for row in active.values()}
    candidates = [j for j in pending if owners.get(job_key(j).as_posix(), host) == host
                  and (j['tape'] in opened or len(opened) < tape_limit)]
    if not candidates:
        return None
    return min(candidates, key=lambda j: (
        owners.get(job_key(j).as_posix()) != host,
        j['tape'] not in opened,
        sum(row['job']['tape'] == j['tape'] for row in active.values()),
        j['tape'], j['channel']))


def read_events(process, key, events, log):
    with log.open('a', encoding='utf-8') as stream:
        for line in process.stdout:
            stream.write(line)
            stream.flush()
            try:
                value = json.loads(line)
                if isinstance(value, dict):
                    events.put((key, value))
            except ValueError:
                events.put((key, dict(event='message', message=line.strip())))


def display(statuses, active, limits, tape_limit, started):
    from rich.console import Group
    from rich.table import Table
    from rich.text import Text
    total = len(statuses)
    complete = sum(s.get('status') == 'complete' for s in statuses.values())
    tapes = {row['job']['tape'] for row in active.values()}
    heading = Text(f'Apollo 15 denoise   {complete}/{total} complete   '
                   f'{len(tapes)}/{tape_limit} active tapes globally   elapsed {(time.time()-started)/3600:.1f} h')
    hosts = Table('Machine', 'Workers', 'Active tapes', 'Completed this session')
    for host in limits:
        rows = [r for r in active.values() if r['host'] == host]
        finished = sum(s.get('status') == 'complete' and s.get('host') == host for s in statuses.values())
        hosts.add_row(host, f'{len(rows)}/{limits[host]}',
                      ', '.join(sorted({r['job']['tape'].replace('A15_', '') for r in rows})) or '—', str(finished))
    workers = Table('Machine', 'Tape / channel', 'Progress', 'Source time', 'State', 'Last update')
    for key, row in sorted(active.items(), key=lambda item: (item[1]['host'], item[0])):
        state = statuses[key]
        cursor, frames = state.get('cursor', 0), row['job']['source']['frames']
        age = time.time()-state.get('updated', row['started'])
        workers.add_row(row['host'], key, f'{100*cursor/frames:.1f}%',
                        f'{cursor/8000/3600:.2f} / {frames/8000/3600:.2f} h',
                        state.get('phase', 'starting'), f'{age:.0f}s ago')
    return Group(heading, hosts, workers, Text('Ctrl+C: checkpoint both machines and stop. Repeat the command to resume.'))


def coordinate(args):
    if os.name != 'nt':
        raise ValueError('Run the master console on Windows')
    config = read_json(args.config)
    check_code(config)
    qualification = verify_qualification(config)
    folder, frozen = session_path(config), config['frozen']
    run, stop = folder.parent, folder/'stop_requested'
    limits = dict(windows=args.local_workers, genus=args.genus_workers)
    if not 0 <= min(limits.values()) or max(limits.values()) > 61 or sum(limits.values()) == 0 or args.tapes < 1:
        raise ValueError('Use 0–61 workers per host, at least one worker, and a positive tape limit')
    lower_priority()
    with archive_lock():
        if read_json(CANONICAL/MARKER) != marker(frozen):
            raise IdentityError('Production root differs from this cluster')
        inventory = load_inventory(INVENTORY)
        jobs = inventory['jobs']
        disk_check(CANONICAL, run/'work', jobs, sum(limits.values()))
        memory_guard(args.local_workers)
        statuses, owners, pending = {}, {}, []
        saved = read_json(folder/'status.json') if (folder/'status.json').exists() else {}
        print('Checking completed channels and locating checkpoints...', flush=True)
        for index, job in enumerate(jobs):
            key = job_key(job).as_posix()
            final = CANONICAL/job_key(job)/'denoised.json'
            if final.exists() and read_json(final).get('run_id') == frozen['run_id']:
                validate_source(job)
                completed = verify_completed(final.parent, job, frozen)
                if completed.get('execution', {}).get('host') == 'genus':
                    acknowledge(folder, job_key(job), completed)
                statuses[key] = dict(status='complete', reused=True)
                if sum(s['status'] == 'complete' for s in statuses.values()) % 20 == 0:
                    print(f'Verified {sum(s["status"] == "complete" for s in statuses.values())} completed channels', flush=True)
                continue
            if (folder/'inbox'/job_key(job)/'denoised.json').exists():
                adopt_remote(config, job, qualification)
                statuses[key] = dict(status='complete', host='genus', reused=True)
                continue
            if (run/'work'/job_key(job)/'checkpoint.json').exists():
                owners[key] = 'windows'
            elif saved.get('jobs', {}).get(key, {}).get('host') == 'genus':
                owners[key] = 'genus'
            statuses[key] = dict(status='pending', action='denoise_v2')
            pending.append(job)
        for host in set(owners.values()):
            if not limits[host]:
                raise IdentityError(f'Unfinished {host} checkpoints require at least one {host} worker')
        stop.unlink(missing_ok=True)
        (folder/'logs').mkdir(exist_ok=True)
        active, events = {}, queue.Queue()
        publisher = ThreadPoolExecutor(max_workers=1, thread_name_prefix='publish')
        started, last_report, stopping = time.time(), 0, False
        def request_stop(*_):
            nonlocal stopping
            stopping = True
            stop.touch()
        previous_handler = signal.signal(signal.SIGINT, request_stop)
        from rich.live import Live
        live = Live(refresh_per_second=1) if not args.plain else nullcontext()
        def save():
            counts = dict(Counter(s['status'] for s in statuses.values()))
            record = dict(run_id=frozen['run_id'], coordinator='windows-ssh',
                updated=time.time(), limits=limits, tape_limit=args.tapes, counts=inventory['counts'],
                complete=counts.get('complete', 0), status_counts=counts, jobs=statuses,
                stop_reason='Stopping at checkpoints' if stopping else None)
            atomic_json(folder/'status.json', record)
            atomic_json(run/'status.json', record)
        try:
            with live as view:
                while pending or active:
                    if stop.exists() and not stopping:
                        request_stop()
                    if args.run_seconds and time.time()-started >= args.run_seconds:
                        request_stop()
                    while True:
                        try:
                            key, event = events.get_nowait()
                        except queue.Empty:
                            break
                        if key in statuses:
                            statuses[key].update({k: v for k, v in event.items() if k in ('cursor', 'frames', 'phase', 'message')})
                            statuses[key]['updated'] = event.get('at', time.time())
                    for key, row in list(active.items()):
                        if 'publication' in row:
                            if not row['publication'].done():
                                continue
                            result = row['publication'].result()
                            statuses[key].update(status='complete', phase='complete', quality=result['quality'],
                                worker_seconds=result.get('worker_seconds', 0), cursor=row['job']['source']['frames'])
                            del active[key]
                            continue
                        code = row['process'].poll()
                        if code is None:
                            continue
                        row['reader'].join(timeout=2)
                        if code == 0:
                            if row['host'] == 'genus':
                                statuses[key].update(phase='verifying / publishing', updated=time.time())
                                row['publication'] = publisher.submit(adopt_remote, config, row['job'], qualification)
                                continue
                            result = verify_completed(CANONICAL/job_key(row['job']), row['job'], frozen)
                            statuses[key].update(status='complete', phase='complete', quality=result['quality'],
                                worker_seconds=result.get('worker_seconds', 0), cursor=row['job']['source']['frames'])
                        else:
                            statuses[key].update(status='interrupted' if stopping else 'failed', exit_code=code,
                                                 phase=f'worker exited {code}; see logs/{key.replace("/", "_")}.log')
                            request_stop()
                        del active[key]
                    if not stopping:
                        check_code(config)
                        if read_json(CANONICAL/MARKER) != marker(frozen):
                            raise IdentityError('Production root changed while scheduling')
                        # Fill local checkpoint slots first, then both hosts on the same open tapes.
                        for host, limit in limits.items():
                            while sum(row['host'] == host for row in active.values()) < limit:
                                job = select_job(pending, active, host, args.tapes, owners)
                                if job is None:
                                    break
                                key = job_key(job).as_posix()
                                config_path = (folder/'config.json' if host == 'windows' else
                                    PurePosixPath(config['remote_data'])/'.denoise_v2_rollout'/frozen['run_id']/'cluster/config.json')
                                arguments = ['worker', '--config', str(config_path), '--key', key, '--host', host]
                                command = ([sys.executable, '-u', str(SCRIPT), *arguments] if host == 'windows'
                                           else remote_command(config, *arguments))
                                statuses[key].update(status='running', host=host, phase='starting', updated=time.time())
                                save()  # Persist assignment before spawning, so resume retains SSD affinity.
                                process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True, encoding='utf-8', errors='replace',
                                    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
                                reader = threading.Thread(target=read_events, args=(process, key, events,
                                    folder/'logs'/f'{key.replace("/", "_")}.log'), daemon=True)
                                active[key] = dict(job=job, host=host, process=process, reader=reader, started=time.time())
                                reader.start()
                                pending.remove(job)
                    if time.time()-last_report >= 5:
                        save()
                        if args.plain:
                            print(f'{time.strftime("%H:%M:%S")} complete={sum(s["status"] == "complete" for s in statuses.values())}/{len(jobs)} '
                                  + ' '.join(f'{h}={sum(r["host"] == h for r in active.values())}/{n}' for h,n in limits.items())
                                  + f' tapes={len({r["job"]["tape"] for r in active.values()})}/{args.tapes}', flush=True)
                        last_report = time.time()
                    if not args.plain:
                        view.update(display(statuses, active, limits, args.tapes, started))
                    if stopping and not active:
                        break
                    time.sleep(.5)
        finally:
            if active:
                request_stop()
                print('Waiting for workers to checkpoint on both machines...', flush=True)
                for row in active.values():
                    row['process'].wait()
                for key in active:
                    if statuses[key]['status'] == 'running':
                        statuses[key].update(status='interrupted', phase='stopped; resume to verify checkpoint')
            save()
            publisher.shutdown(wait=True)
            signal.signal(signal.SIGINT, previous_handler)
        return int(stopping or any(s['status'] != 'complete' for s in statuses.values()))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    preparation = sub.add_parser('prepare', help='Compare Windows and Linux before enabling remote production')
    preparation.add_argument('--ssh', default='bfeist@192.168.0.240')
    preparation.add_argument('--remote-data', default='/mnt/d/Apollo_15_30-Track')
    preparation.add_argument('--remote-repo', default='/mnt/f/_repos/Apollo_15')
    preparation.add_argument('--remote-uv', default='/home/bfeist/.local/bin/uv')
    preparation.add_argument('--remote-python', default='/home/bfeist/.local/share/apollo15-venv/bin/python')
    preparation.add_argument('--scratch', default='/home/bfeist/.local/share/apollo15-denoise')
    for name in ('run', 'worker', 'qualify'):
        child = sub.add_parser(name)
        child.add_argument('--config', type=Path, default=CONTROL/read_json(CANONICAL/MARKER)['run_id']/'cluster/config.json'
                           if os.name == 'nt' and (CANONICAL/MARKER).exists() else None)
        if name == 'run':
            child.add_argument('--local-workers', type=int, default=6)
            child.add_argument('--genus-workers', type=int, default=16)
            child.add_argument('--tapes', type=int, default=3, help='Shared global active-tape limit across both machines')
            child.add_argument('--plain', action='store_true')
            child.add_argument('--run-seconds', type=float, help='Optional bounded checkpoint/resume trial')
        elif name == 'worker':
            child.add_argument('--key', required=True)
            child.add_argument('--host', choices=['windows', 'genus'], required=True)
    args = parser.parse_args()
    if args.command == 'prepare':
        return prepare(args)
    if not args.config:
        parser.error('--config is required')
    if args.command == 'run':
        return coordinate(args)
    config = read_json(args.config)
    if args.command == 'qualify':
        return qualify(config)
    return worker(config, args.key, args.host)


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (ValueError, OSError, KeyError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f'Cluster stopped: {exc}', file=sys.stderr, flush=True)
        raise SystemExit(2)
