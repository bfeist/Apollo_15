"""Stage-05 detection census: run identity, block jobs, Windows/Genus pool and streams.

One Proprius coordinator owns the queue and assigns each tape/block once to a
persistent worker on this computer or on Genus. Workers read canonical
originals, write a provisional block result into the run's isolated inbox, and
the coordinator verifies job/source/code bindings, shape, finite values and
hashes before atomic publication. Completed tapes are joined once into a
``consensus.f64`` stream and a hash-bound ``evidence.json``. Planning and
rendering never start this analysis; it is an explicit preparation step.
"""
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import platform
import queue
import shlex
import shutil
import signal as signals
import subprocess
import sys
import threading
import time
import traceback

import numpy as np

import flutter_census as census

HERE = Path(__file__).resolve().parent
DATA = Path('D:/Apollo_15_30-Track')
DEFAULT_DETECTION_ROOT = DATA/'deflutter_detection_census'
DEFAULT_ORIGINALS = DATA/'orig_concatenated'
SCHEMA = 1
IMPLEMENTATION = ('flutter_census.py', 'flutter_detection.py', 'flutter_tracking.py', 'flutter.py')
THREAD_VARIABLES = ('OPENBLAS_NUM_THREADS', 'MKL_NUM_THREADS', 'OMP_NUM_THREADS',
                    'BLIS_NUM_THREADS', 'NUMEXPR_NUM_THREADS', 'VECLIB_MAXIMUM_THREADS')
GENUS = dict(ssh='bfeist@192.168.0.3', data='/mnt/d/Apollo_15_30-Track', repo='/mnt/f/_repos/Apollo_15',
             uv='/home/bfeist/.local/bin/uv', python='/home/bfeist/.local/share/apollo15-venv/bin/python',
             scratch='/home/bfeist/.local/share/apollo15-census')
WINDOWS_MAX_WORKERS = 6
# Benchmark starting points only (docs/09_2026-11-15_deflutter_detection_guide.md section 5);
# replace them with a measured selection under the run's benchmarks/ folder.
PROVISIONAL_POOL = dict(windows=6, genus=6, genus_cpus='0,2,4,8,10,12', tapes=3, benchmarked=False)
STORAGE_DELAYS = (2, 5, 10, 20, 30, *([60]*14))
STORAGE_ERRNOS = {11, 5, 116, 110, 104, 103, 107, 101, 113}  # EAGAIN EIO ESTALE ETIMEDOUT ECONN*/ENET*
REPLACE_DELAYS = (.05, .1, .2, .4, .8, 1., 1.)
BLOCK_FILES = ('result.json', 'curves.npy')
EMIT_LOCK = threading.Lock()


class IdentityError(ValueError):
    """Source, code, run or result identity does not match; never retried."""


# ---------------------------------------------------------------- utilities

def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def atomic_json(path, value):
    """Atomic replace; brief Windows reader locks are retried, never ignored."""
    path = Path(path)
    pending = path.with_name(path.name+'.partial')
    for attempt in range(len(REPLACE_DELAYS)+1):
        try:
            with pending.open('w', encoding='utf-8') as stream:
                json.dump(value, stream, indent=1, allow_nan=False, default=_json_default)
                stream.flush()
                os.fsync(stream.fileno())
            pending.replace(path)
            return
        except PermissionError as exc:
            if getattr(exc, 'winerror', None) not in (5, 32, 33) or attempt == len(REPLACE_DELAYS):
                raise
            time.sleep(REPLACE_DELAYS[attempt])


def _json_default(value):
    if isinstance(value, (np.floating, np.integer, np.bool_)):
        return value.item()
    raise TypeError(f'Not JSON serializable: {type(value).__name__}')


def versions():
    import scipy
    import soundfile
    return dict(python=platform.python_version(), numpy=np.__version__, scipy=scipy.__version__,
                soundfile=soundfile.__version__, libsndfile=soundfile.__libsndfile_version__)


def implementation():
    return {name: sha256(HERE/name) for name in IMPLEMENTATION}


def emit(**value):
    with EMIT_LOCK:
        print(json.dumps(dict(at=time.time(), **value), allow_nan=False, default=_json_default), flush=True)


def source_identity(inventory):
    """Canonical source binding: files, formats and recorded channel offsets."""
    return {str(r['channel']): {k: r[k] for k in
            ('path', 'bytes', 'mtime_ns', 'sample_rate', 'frames', 'start_seconds')}
            for r in inventory['channels']}


def map_path(path, mapping):
    """Map a recorded Windows path through ``WIN=POSIX`` prefixes (Genus mounts)."""
    text = str(path).replace('\\', '/')
    for windows, posix in mapping:
        prefix = windows.replace('\\', '/').rstrip('/')
        if text.lower() == prefix.lower() or text.lower().startswith(prefix.lower()+'/'):
            return Path(posix.rstrip('/')+text[len(prefix):])
    return Path(path)


# ---------------------------------------------------------------- run identity

def run_identity(settings, originals, tag=None):
    value = dict(schema=SCHEMA, methodology=census.METHODOLOGY,
                 settings=census.normalized(settings), implementation=implementation(),
                 originals=Path(originals).resolve().as_posix(), tag=tag)
    return 'census-'+digest(value)[:16], value


def open_run(run_dir=None, *, root=DEFAULT_DETECTION_ROOT, settings=None, originals=DEFAULT_ORIGINALS,
             tag=None, create=True):
    """Create or reopen a run; a changed setting, code or input root needs a new ID."""
    run_id, identity = run_identity(settings or census.SETTINGS, originals, tag)
    run_dir = Path(root)/run_id if run_dir is None else Path(run_dir)
    run_dir = run_dir.resolve()
    path = run_dir/'config.json'
    if path.exists():
        config = read_json(path)
        if config.get('run_id') != run_id or config.get('identity') != identity:
            raise IdentityError(f'{run_dir} was created with different detector settings, code or '
                                f'input root; the current configuration is run {run_id}')
        return run_dir, config
    if run_dir.name != run_id:
        raise IdentityError(f'A new detection run directory must be named {run_id}')
    if not create:
        raise IdentityError(f'Detection run does not exist: {run_dir}')
    run_dir.mkdir(parents=True, exist_ok=True)
    config = dict(schema=SCHEMA, kind='census_run', run_id=run_id, identity=identity,
                  settings_sha256=census.settings_digest(settings or census.SETTINGS),
                  created=time.strftime('%Y-%m-%dT%H:%M:%S%z'), environment=versions(),
                  host=platform.node(),
                  interpretation='Detection evidence from canonical originals; not listening acceptance')
    atomic_json(path, config)
    return run_dir, config


def load_run(run_dir):
    """Read an existing run without requiring the current source to match."""
    run_dir = Path(run_dir).resolve()
    config = read_json(run_dir/'config.json')
    if (config.get('kind') != 'census_run' or config.get('run_id') != run_dir.name
            or 'census-'+digest(config['identity'])[:16] != config['run_id']):
        raise IdentityError(f'Invalid or relocated detection run: {run_dir}')
    return config


def check_code(config):
    if implementation() != config['identity']['implementation']:
        raise IdentityError('Detection implementation changed since this run was created; '
                            'completed blocks stay valid, but new work needs a new run ID')


def settings_of(config):
    return config['identity']['settings']


# ---------------------------------------------------------------- tape jobs

def duplicate_audit(inventory):
    """Cheap guard: byte fingerprints of 3 x 2 s, e.g. A15_T510096's identical files."""
    import soundfile as sf
    groups = {}
    for row in inventory['channels']:
        value = hashlib.sha1()
        with sf.SoundFile(row['path']) as stream:
            for q in (.1, .5, .9):
                stream.seek(int(stream.frames*q))
                value.update(stream.read(2*stream.samplerate, dtype='int16').tobytes())
        groups.setdefault(value.hexdigest(), []).append(row['channel'])
    duplicates = sorted(sorted(v) for v in groups.values() if len(v) > 1)
    return dict(channels=len(inventory['channels']), distinct=len(groups), duplicate_groups=duplicates,
                flag=('Effectively one channel; request re-delivery' if len(groups) == 1 and len(inventory['channels']) > 1
                      else 'Identical channel files count once' if duplicates else None))


def block_plan(duration, settings):
    block, halo = settings['block_seconds'], settings['halo_seconds']
    length = settings['window']['seconds']
    blocks = []
    for index in range(max(1, math.ceil(duration/block-1e-9))):
        core_start, core_end = index*block, min((index+1)*block, duration)
        read_start = max(0., core_start-halo)
        read_end = min(duration, core_end+halo)
        if read_end-read_start < length:  # A short tail still needs whole windows.
            read_start = max(0., read_end-length)
            read_end = min(duration, read_start+length)
        read_start = math.floor(read_start*census.RATE+1e-9)/census.RATE
        blocks.append(dict(block=f'{index:04d}', core_start=core_start, core_end=core_end,
                           read_start=read_start, read_end=read_end))
    return blocks


def prepare_tape(run_dir, config, folder):
    """Bind one tape's originals and offsets to the run; refuse changed sources."""
    from flutter_planning import inventory as read_inventory
    folder = Path(folder).resolve()
    if folder.parent.as_posix() != config['identity']['originals']:
        raise IdentityError(f'{folder} is outside this run\'s canonical originals')
    inventory = read_inventory(folder)
    if inventory['tape'] != folder.name:
        raise IdentityError('Tape folder and inventory disagree')
    identity = source_identity(inventory)
    path = run_dir/inventory['tape']/'inventory.json'
    if path.exists():
        saved = read_json(path)
        if saved['source_identity'] != identity:
            raise IdentityError(f'{inventory["tape"]} originals or offsets changed since detection began; '
                                'use a new run ID')
        return saved
    rates = {r['sample_rate'] for r in inventory['channels']}
    if rates != {settings_of(config)['sample_rate']}:
        raise IdentityError(f'{inventory["tape"]} sample rate {sorted(rates)} is not supported')
    path.parent.mkdir(parents=True, exist_ok=True)
    record = dict(schema=SCHEMA, kind='census_tape', run_id=config['run_id'], tape=inventory['tape'],
                  inventory=inventory, source_identity=identity, inventory_sha256=digest(identity),
                  duplicate_audit=duplicate_audit(inventory),
                  blocks=block_plan(inventory['duration_seconds'], settings_of(config)))
    atomic_json(path, record)
    return record


# ---------------------------------------------------------------- block work

def read_block(rows, read_start, read_end, sample_rate, mapping=(), check_mtime=True):
    """Canonical reader: canonical time = WAV time + start_seconds."""
    import soundfile as sf
    by_channel = {r['channel']: r for r in rows}
    stats = dict(read_seconds=0., bytes=0)

    def read(channel):
        row = by_channel[channel]
        path = map_path(row['path'], mapping)
        started = time.perf_counter()
        info = path.stat()
        if info.st_size != row['bytes'] or (check_mtime and info.st_mtime_ns != row['mtime_ns']):
            raise IdentityError(f'Original changed since inventory: {path}')
        first = int(round((read_start-row['start_seconds'])*sample_rate))
        count = int(round((read_end-read_start)*sample_rate))
        out = np.zeros(count, np.float32)
        lo, hi = max(first, 0), min(first+count, row['frames'])
        if hi > lo:
            with sf.SoundFile(path) as stream:
                if stream.frames != row['frames'] or stream.samplerate != sample_rate or stream.channels != 1:
                    raise IdentityError(f'Original format changed: {path}')
                stream.seek(lo)
                data = stream.read(hi-lo, dtype='float32')
            if len(data) != hi-lo or not np.isfinite(data).all():
                raise IdentityError(f'Short or non-finite original read: {path}')
            out[lo-first:hi-first] = data
        stats['read_seconds'] += time.perf_counter()-started
        stats['bytes'] += max(0, hi-lo)*2
        return out, max(0, hi-lo)/max(count, 1)
    return read, stats


def block_job(tape_record, block):
    return dict(tape=tape_record['tape'], **block, inventory_sha256=tape_record['inventory_sha256'])


def compute_block(run_dir, config, job, destination, *, host, mapping=(), progress=None):
    """Analyse one tape x block x all-channels job into ``destination``."""
    tape = read_json(Path(run_dir)/job['tape']/'inventory.json')
    if tape['inventory_sha256'] != job['inventory_sha256']:
        raise IdentityError('Job and tape inventory binding disagree')
    settings = settings_of(config)
    rows = tape['inventory']['channels']
    read, stats = read_block(rows, job['read_start'], job['read_end'], settings['sample_rate'],
                             mapping, check_mtime=not mapping)
    known = tape['duplicate_audit']['duplicate_groups']
    started, cpu = time.perf_counter(), time.process_time()
    analysis, curves = census.analyse_block(read, [r['channel'] for r in rows], job['tape'],
                                            job['read_start'], job['read_end'], settings, known, progress)
    seconds = time.perf_counter()-started
    pending = Path(str(destination)+'.partial')
    if pending.exists():
        shutil.rmtree(pending)
    pending.mkdir(parents=True)
    np.save(pending/'curves.npy', curves.astype('<f4'), allow_pickle=False)
    record = dict(schema=SCHEMA, kind='census_block', complete=True, run_id=config['run_id'],
                  **{k: job[k] for k in ('tape', 'block', 'core_start', 'core_end', 'read_start',
                                         'read_end', 'inventory_sha256')},
                  settings_sha256=config['settings_sha256'], implementation=implementation(),
                  execution=dict(host=host, hostname=platform.node(), environment=versions(),
                                 wall_seconds=round(seconds, 3), read_seconds=round(stats['read_seconds'], 3),
                                 compute_seconds=round(seconds-stats['read_seconds'], 3),
                                 cpu_seconds=round(time.process_time()-cpu, 3), bytes_read=stats['bytes']),
                  curves=dict(file='curves.npy', sha256=sha256(pending/'curves.npy'),
                              shape=list(curves.shape), dtype='<f4'),
                  **analysis)
    atomic_json(pending/'result.json', record)
    if Path(destination).exists():
        shutil.rmtree(destination)
    pending.rename(destination)
    return record


def verify_block(folder, job, config):
    """Coordinator check before publication and on every resume."""
    folder = Path(folder)
    if {p.name for p in folder.iterdir()} != set(BLOCK_FILES):
        raise IdentityError(f'Unexpected block files in {folder}')
    record = read_json(folder/'result.json')
    expected = dict(schema=SCHEMA, kind='census_block', complete=True, run_id=config['run_id'],
                    settings_sha256=config['settings_sha256'],
                    implementation=config['identity']['implementation'],
                    **{k: job[k] for k in ('tape', 'block', 'core_start', 'core_end', 'read_start',
                                           'read_end', 'inventory_sha256')})
    wrong = [k for k, v in expected.items() if record.get(k) != v]
    if wrong:
        raise IdentityError(f'Block {job["tape"]}/{job["block"]} does not match its job: {wrong}')
    spec = record['curves']
    if spec.get('file') != 'curves.npy' or spec.get('dtype') != '<f4' or sha256(folder/'curves.npy') != spec['sha256']:
        raise IdentityError(f'Block curves changed: {folder}')
    curves = np.load(folder/'curves.npy', allow_pickle=False)
    w = settings_of(config)['window']
    width = int(round((w['seconds']-2*w['edge_seconds'])*census.RATE))
    if (curves.dtype != np.dtype('<f4') or list(curves.shape) != spec['shape'] or curves.ndim != 2
            or (len(curves) and curves.shape[1] != width)):
        raise IdentityError(f'Block curve shape changed: {folder}')
    if not np.isfinite(curves).all() or (curves.size and np.max(abs(curves)) > .05):
        raise IdentityError(f'Block curves are not finite bounded speed errors: {folder}')
    indices = [r['curve_index'] for r in record['windows'] if r.get('curve_index') is not None]
    if sorted(indices) != list(range(len(curves))):
        raise IdentityError(f'Block windows and curves disagree: {folder}')
    for r in record['windows']:
        if not job['read_start']-1e-6 <= r['start_seconds'] < r['end_seconds'] <= job['read_end']+1e-6:
            raise IdentityError(f'Block window lies outside its read interval: {folder}')
        if r['eligible'] and r.get('curve_index') is None:
            raise IdentityError(f'Eligible window without a consensus curve: {folder}')
    return record, sha256(folder/'result.json')


def copy_verified(source, destination):
    """Buffered, flushed copies; a fully matching earlier copy is reused."""
    destination.mkdir(parents=True, exist_ok=True)
    for name in BLOCK_FILES:
        target = destination/name
        if target.is_file() and target.stat().st_size == (source/name).stat().st_size \
                and sha256(target) == sha256(source/name):
            with target.open('r+b') as writer:
                os.fsync(writer.fileno())
            continue
        with (source/name).open('rb') as reader, target.open('wb') as writer:
            shutil.copyfileobj(reader, writer, length=1 << 20)
            writer.flush()
            os.fsync(writer.fileno())


class Session:
    """Locations for one coordinator session: production run or isolated benchmark."""
    def __init__(self, run_dir, config, cluster=None, published=None, finalize=True):
        self.run_dir, self.config = Path(run_dir), config
        self.cluster = Path(cluster or self.run_dir/'cluster')
        self._published = None if published is None else Path(published)
        self.finalize = finalize

    def published(self, job):
        if self._published is None:
            return self.run_dir/job['tape']/'blocks'/job['block']
        return self._published/job['tape']/job['block']

    def inbox(self, job):
        return self.cluster/'inbox'/job['tape']/job['block']

    def ack(self, job):
        return self.cluster/'acks'/job['tape']/(job['block']+'.json')


def publish(session, job):
    """Verify an inbox result, then atomically publish and acknowledge it."""
    inbox, final = session.inbox(job), session.published(job)
    record, digest_value = verify_block(inbox, job, session.config)
    if final.exists():
        _, existing = verify_block(final, job, session.config)
        if existing != digest_value:
            raise IdentityError(f'Published block {job["tape"]}/{job["block"]} differs from a new result')
    else:
        pending = final.with_name(final.name+'.partial')
        if pending.exists():
            shutil.rmtree(pending)
        copy_verified(inbox, pending)
        verify_block(pending, job, session.config)
        pending.rename(final)
        verify_block(final, job, session.config)
    ack = session.ack(job)
    ack.parent.mkdir(parents=True, exist_ok=True)
    atomic_json(ack, dict(result_sha256=digest_value, published=str(final)))
    for name in BLOCK_FILES:
        (inbox/name).unlink()
    inbox.rmdir()
    return record


# ---------------------------------------------------------------- tape streams

def finalize_tape(run_dir, config, tape_record):
    """Join passing windows once per tape; never extrapolate across failures."""
    folder = Path(run_dir)/tape_record['tape']
    evidence_path = folder/'evidence.json'
    if evidence_path.exists():
        return load_census(run_dir, tape_record['tape'])
    settings = settings_of(config)
    blocks, windows, joined = [], [], []
    for block in tape_record['blocks']:
        job = block_job(tape_record, block)
        path = folder/'blocks'/block['block']
        record, result_hash = verify_block(path, job, config)
        curves = np.load(path/'curves.npy', allow_pickle=False)
        blocks.append(dict(block=block['block'], result_sha256=result_hash,
                           curves_sha256=record['curves']['sha256'], core=[block['core_start'], block['core_end']],
                           read=[block['read_start'], block['read_end']], duplicate_groups=record['duplicate_groups'],
                           execution=record['execution']))
        for row in record['windows']:
            windows.append(dict(row, block=block['block']))
            if row['eligible']:
                joined.append((row['valid_start_seconds'], curves[row['curve_index']].astype(np.float64)))
    duration = tape_record['inventory']['duration_seconds']
    speed, support = census.join_windows(joined, duration, settings)
    pending = folder/'consensus.f64.partial'
    np.column_stack((speed, support)).astype('<f8').tofile(pending)
    pending.replace(folder/'consensus.f64')
    audits = [r['beep_audit'] for r in windows if r.get('beep_audit', {}).get('beeps')]
    beeps = [e for a in audits for e in a['events']]
    beep_summary = dict(windows=len(audits), beeps=len(beeps),
                        # Advisory only: the audit never gates. Listed for inspection.
                        adverse_windows=[dict(start_seconds=r['start_seconds'], eligible=r['eligible'],
                                              beeps=r['beep_audit']['beeps'], curve_ratio=r['beep_audit']['curve_ratio'])
                                         for r in windows if r.get('beep_audit', {}).get('beeps', 0) >= 3
                                         and r['beep_audit']['curve_ratio'] > 1],
                        sign_conflict_windows=sum(bool(r.get('sign_conflict')) for r in windows))
    if beeps:
        table = np.array([[e['identity'], e['curve'], e['wrong_sign'], e['shift']] for e in beeps])
        base = np.maximum(table[:, 0], 1e-9)
        beep_summary.update(median_hz=dict(zip(['identity', 'curve', 'wrong_sign', 'shift'],
                                               np.round(np.median(table, 0), 3).tolist())),
                            curve_ratio=round(float(np.median(table[:, 1]/base)), 4),
                            worse=int((table[:, 1] > table[:, 0]).sum()),
                            note='Simultaneous beeps on several channels are one event; counts overstate independence')
    supported = float(support.sum()/census.RATE)
    evidence = dict(schema=SCHEMA, kind='census_evidence', complete=True, run_id=config['run_id'],
                    tape=tape_record['tape'], methodology=census.METHODOLOGY,
                    inventory=tape_record['inventory'], source_identity=tape_record['source_identity'],
                    inventory_sha256=tape_record['inventory_sha256'],
                    config_sha256=sha256(Path(run_dir)/'config.json'),
                    settings_sha256=config['settings_sha256'],
                    implementation=config['identity']['implementation'],
                    duplicate_audit=tape_record['duplicate_audit'], blocks=blocks,
                    stream=dict(file='consensus.f64', sha256=sha256(folder/'consensus.f64'),
                                frames=len(speed), rate=census.RATE, start_seconds=0., dtype='<f8',
                                columns=['speed', 'support']),
                    coverage=dict(supported_seconds=supported, duration_seconds=duration,
                                  tape_time_fraction=supported/duration if duration else 0.,
                                  windows=len(windows), eligible_windows=sum(r['eligible'] for r in windows),
                                  basis='Union of passing-window tape time, never channel-seconds'),
                    beep_audit=beep_summary, windows=windows,
                    interpretation=('Blind cross-channel tape-motion evidence; check beeps are an audit, '
                                    'never a gate. Not listening or production acceptance.'))
    atomic_json(evidence_path, evidence)
    return load_census(run_dir, tape_record['tape'])


def load_census(run_dir, tape):
    """Validate a completed tape bundle for stage 05a; incomplete or stale is an error."""
    run_dir = Path(run_dir).resolve()
    config = load_run(run_dir)
    folder = run_dir/tape
    path = folder/'evidence.json'
    if not path.is_file():
        raise IdentityError(f'Detection census for {tape} is missing or incomplete in {run_dir}')
    evidence = read_json(path)
    if (evidence.get('kind') != 'census_evidence' or evidence.get('complete') is not True
            or evidence.get('run_id') != config['run_id'] or evidence.get('tape') != tape
            or evidence.get('settings_sha256') != config['settings_sha256']
            or evidence.get('config_sha256') != sha256(run_dir/'config.json')):
        raise IdentityError(f'Detection census for {tape} is incomplete or belongs to another run')
    tape_record = read_json(folder/'inventory.json')
    if tape_record['inventory_sha256'] != evidence['inventory_sha256'] or \
            digest(evidence['source_identity']) != evidence['inventory_sha256']:
        raise IdentityError(f'Detection census for {tape} has inconsistent source bindings')
    for block in evidence['blocks']:
        result = folder/'blocks'/block['block']/'result.json'
        curves = result.with_name('curves.npy')
        if (not result.is_file() or sha256(result) != block['result_sha256']
                or not curves.is_file() or sha256(curves) != block['curves_sha256']):
            raise IdentityError(f'Detection census block {tape}/{block["block"]} changed')
    stream = evidence['stream']
    data = folder/stream['file']
    if (stream['dtype'] != '<f8' or stream['rate'] != census.RATE
            or data.stat().st_size != stream['frames']*16 or sha256(data) != stream['sha256']):
        raise IdentityError(f'Detection census stream for {tape} changed')
    return dict(run_dir=run_dir, config=config, evidence=evidence, evidence_path=path,
                evidence_sha256=sha256(path), stream_path=data.resolve())


# ---------------------------------------------------------------- workers

def lower_priority():
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        kernel.SetPriorityClass.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        if not kernel.SetPriorityClass(kernel.GetCurrentProcess(), 0x4000):
            raise ctypes.WinError(ctypes.get_last_error())


def release_acknowledged(scratch, cluster):
    """Remove local Genus results only after Proprius acknowledged publication."""
    if not scratch.is_dir():
        return
    for folder in scratch.glob('*/*'):
        if not folder.is_dir() or folder.name.endswith('.partial'):
            continue
        ack = cluster/'acks'/folder.parent.name/(folder.name+'.json')
        if not ack.is_file():
            continue
        if read_json(ack)['result_sha256'] != sha256(folder/'result.json'):
            raise IdentityError(f'Acknowledgement differs from local result: {folder}')
        if {p.name for p in folder.iterdir()} != set(BLOCK_FILES):
            emit(event='message', message=f'Local cleanup deferred, unexpected files: {folder}')
            continue
        for name in BLOCK_FILES:
            (folder/name).unlink()
        folder.rmdir()


def upload(local, inbox, stop):
    """Stage locally, copy into a private .upload folder, then hand off atomically."""
    pending = inbox.with_name(inbox.name+'.upload')
    for attempt in range(len(STORAGE_DELAYS)+1):
        try:
            if inbox.exists():
                if all(sha256(inbox/n) == sha256(local/n) for n in BLOCK_FILES):
                    return 'already uploaded'
                raise IdentityError(f'Inbox already holds a different result: {inbox}')
            inbox.parent.mkdir(parents=True, exist_ok=True)
            copy_verified(local, pending)
            if any(sha256(pending/n) != sha256(local/n) for n in BLOCK_FILES):
                raise OSError(5, 'Uploaded block differs from local result')
            pending.rename(inbox)
            return 'uploaded'
        except OSError as exc:
            if isinstance(exc, IdentityError) or exc.errno not in STORAGE_ERRNOS or attempt == len(STORAGE_DELAYS):
                raise
            delay = STORAGE_DELAYS[attempt]
            emit(event='message', phase='storage retry',
                 message=f'Transient storage error: {exc}; retry {attempt+1}/{len(STORAGE_DELAYS)} in {delay}s; '
                         'retaining the local result')
            for _ in range(delay):
                try:
                    if stop.exists():
                        raise InterruptedError('Stop requested during upload retry')
                except OSError:
                    pass
                time.sleep(1)


def worker(run_dir, cluster, host, mapping=(), scratch=None, stream=None):
    """Persistent worker: one JSON job per stdin line, JSON events on stdout."""
    run_dir, cluster = Path(run_dir), Path(cluster)
    config = load_run(run_dir)
    check_code(config)
    if host == 'windows':
        lower_priority()
    affinity = sorted(os.sched_getaffinity(0)) if hasattr(os, 'sched_getaffinity') else None
    emit(event='hello', host=host, hostname=platform.node(), pid=os.getpid(), environment=versions(),
         implementation=implementation(), threads={v: os.environ.get(v) for v in THREAD_VARIABLES},
         affinity=affinity)
    state = dict(key=None, phase='idle', channel=0, channels=0)
    done = threading.Event()

    def heartbeat():
        while not done.wait(10):
            emit(event='progress', host=host, **state)
    threading.Thread(target=heartbeat, daemon=True).start()
    stop = cluster/'stop_requested'
    try:
        for line in (stream or sys.stdin):
            message = json.loads(line)
            if message.get('op') == 'stop':
                break
            job = message['job']
            key = f'{job["tape"]}/{job["block"]}'
            state.update(key=key, phase='starting', channel=0, channels=0)
            started = time.perf_counter()
            try:
                inbox = cluster/'inbox'/job['tape']/job['block']
                timings = {}
                if host == 'genus':
                    local_root = Path(scratch)/config['run_id']/hashlib.sha256(
                        cluster.as_posix().encode()).hexdigest()[:12]
                    release_acknowledged(local_root, cluster)
                    local = local_root/job['tape']/job['block']
                    try:
                        verify_block(local, job, config)
                        reused = True
                    except (OSError, ValueError, KeyError):
                        reused = False
                    if not reused:
                        state['phase'] = 'computing'
                        compute_block(run_dir, config, job, local, host=host, mapping=mapping,
                                      progress=lambda i, n: state.update(channel=i, channels=n))
                    state['phase'] = 'uploading'
                    upload_started = time.perf_counter()
                    result = upload(local, inbox, stop)
                    timings.update(upload_seconds=round(time.perf_counter()-upload_started, 3),
                                   reused_local=reused, upload=result)
                else:
                    if inbox.exists():
                        verify_block(inbox, job, config)
                        timings['reused_inbox'] = True
                    else:
                        state['phase'] = 'computing'
                        compute_block(run_dir, config, job, inbox, host=host, mapping=mapping,
                                      progress=lambda i, n: state.update(channel=i, channels=n))
                record = read_json(inbox/'result.json')
                emit(event='complete', host=host, key=key, seconds=round(time.perf_counter()-started, 3),
                     execution=record['execution'], **timings)
            except Exception as exc:
                emit(event='error', host=host, key=key, identity=isinstance(exc, IdentityError),
                     message=f'{type(exc).__name__}: {exc}', traceback=traceback.format_exc())
            state.update(key=None, phase='idle')
    finally:
        done.set()
    return 0


# ---------------------------------------------------------------- coordinator

def cpu_list(value):
    if value in (None, 'none'):
        return 'none'
    parts = str(value).split(',')
    if not all(p.isascii() and p.isdecimal() for p in parts) or len(set(parts)) != len(parts):
        raise ValueError('Use comma-separated unique CPU IDs, or none')
    return ','.join(str(int(p)) for p in parts)


def windows_to_posix(path, connection):
    mapped = map_path(path, [(DATA.as_posix(), connection['data']),
                             (Path(__file__).resolve().parents[2].as_posix(), connection['repo'])])
    return PurePosixPath(str(mapped).replace('\\', '/'))


def local_command(session):
    return [sys.executable, '-u', str(HERE.parent/'run.py'), '05_detect_flutter.py', 'worker',
            '--run-dir', str(session.run_dir), '--cluster-dir', str(session.cluster), '--host', 'windows']


def remote_command(session, connection, cpus):
    """SSH into Genus with its host-local pinned environment and CPU mask."""
    argv = [connection['uv'], 'run', '--no-project', '--python', connection['python'],
            str(PurePosixPath(connection['repo'])/'scripts/run.py'), '05_detect_flutter.py', 'worker',
            '--run-dir', str(windows_to_posix(session.run_dir, connection)),
            '--cluster-dir', str(windows_to_posix(session.cluster, connection)), '--host', 'genus',
            '--path-map', f'{DATA.as_posix()}={connection["data"]}', '--scratch', connection['scratch']]
    prefix = ['env', *[f'{v}=1' for v in THREAD_VARIABLES]]
    if cpus != 'none':
        prefix += ['taskset', '-c', cpus]
    return ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10', '-o', 'ServerAliveInterval=15',
            '-o', 'ServerAliveCountMax=3', connection['ssh'], 'exec '+shlex.join(prefix+argv)]


def pool_settings(run_dir):
    path = Path(run_dir)/'benchmarks'/'selection.json'
    if path.is_file():
        selection = read_json(path)
        return dict(selection['pool'], benchmarked=True, selection_sha256=sha256(path))
    return dict(PROVISIONAL_POOL)


class Worker:
    def __init__(self, name, host, process, log):
        self.name, self.host, self.process = name, host, process
        self.job, self.ready, self.hello, self.updated = None, False, None, time.time()
        self.state, self.started_job = {}, None
        self.log = log


def kill_tree(process):
    """Windows venv launchers start a child interpreter; stop the whole tree."""
    if process.poll() is not None:
        return
    if os.name == 'nt':
        subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, check=False)
    else:
        process.kill()


def read_events(worker, events):
    with worker.log.open('a', encoding='utf-8') as log:
        for line in worker.process.stdout:
            log.write(line)
            log.flush()
            try:
                value = json.loads(line)
                if isinstance(value, dict):
                    events.put((worker, value))
                    continue
            except ValueError:
                pass
            events.put((worker, dict(event='message', message=line.rstrip())))
    events.put((worker, dict(event='exit')))


def select_job(pending, active_tapes, tape_limit):
    opened = set(active_tapes)
    candidates = [j for j in pending if j['tape'] in opened or len(opened) < tape_limit]
    if not candidates:
        return None
    return min(candidates, key=lambda j: (j['tape'] not in opened, j['tape'], j['block']))


def check_hello(worker, hello, config, expected_cpus):
    if hello.get('implementation') != config['identity']['implementation']:
        raise IdentityError(f'{worker.name} runs different detection code')
    mine = versions()
    for name in ('numpy', 'scipy', 'soundfile', 'libsndfile'):
        if hello['environment'].get(name) != mine[name]:
            raise IdentityError(f'{worker.name} {name} {hello["environment"].get(name)} differs from '
                                f'the coordinator {mine[name]}; use the pinned host environment')
    if any(hello['threads'].get(v) != '1' for v in ('OPENBLAS_NUM_THREADS', 'OMP_NUM_THREADS', 'MKL_NUM_THREADS')):
        raise IdentityError(f'{worker.name} does not limit numerical libraries to one thread')
    if worker.host == 'genus' and expected_cpus != 'none' and hello.get('affinity') != sorted(
            int(c) for c in expected_cpus.split(',')):
        raise IdentityError(f'{worker.name} CPU affinity {hello.get("affinity")} differs from {expected_cpus}')


def coordinate(session, folders, *, local_workers, genus_workers, genus_cpus='none', tape_limit=3,
               connection=None, plain=True, run_seconds=None, worker_factory=None, job_filter=None,
               output=print):
    """Own the queue for both hosts; drain to committed blocks on stop."""
    from flutter_plan_store import planning_lock
    config = session.config
    check_code(config)
    genus_cpus = cpu_list(genus_cpus)
    if not 0 <= local_workers <= WINDOWS_MAX_WORKERS:
        raise ValueError(f'Proprius workers must be between 0 and {WINDOWS_MAX_WORKERS} for developer headroom')
    if genus_workers < 0 or local_workers+genus_workers == 0 or tape_limit < 1:
        raise ValueError('Use at least one worker and a positive active-tape limit')
    if genus_cpus != 'none' and genus_workers > len(genus_cpus.split(',')):
        raise ValueError('Genus workers exceed the CPU mask; expand --genus-cpus or use none')
    connection = dict(GENUS, **(connection or {}))
    session.cluster.mkdir(parents=True, exist_ok=True)
    with planning_lock(session.cluster):
        statuses, pending, tapes, totals = {}, [], {}, {}
        for folder in folders:
            record = prepare_tape(session.run_dir, config, folder)
            audit = record['duplicate_audit']
            if audit['distinct'] == 1 and audit['channels'] > 1:
                # E.g. A15_T510096: one recording copied to every channel file.
                output(f'{record["tape"]}: all channel files are identical; skipped, request re-delivery')
                continue
            tapes[record['tape']] = record
            if session.finalize and (session.run_dir/record['tape']/'evidence.json').exists():
                load_census(session.run_dir, record['tape'])
                statuses.update({f'{record["tape"]}/{b["block"]}': dict(status='complete', reused=True)
                                 for b in record['blocks']})
                continue
            for block in record['blocks']:
                job = block_job(record, block)
                key = f'{job["tape"]}/{job["block"]}'
                if job_filter and key not in job_filter:
                    continue
                totals[key] = job['core_end']-job['core_start']
                if session.published(job).exists():
                    verify_block(session.published(job), job, config)
                    statuses[key] = dict(status='complete', reused=True)
                elif session.inbox(job).exists():
                    publish(session, job)  # A finished upload is never recomputed.
                    statuses[key] = dict(status='complete', reused=True, recovered='inbox')
                else:
                    statuses[key] = dict(status='pending')
                    pending.append(job)
        (session.cluster/'logs').mkdir(exist_ok=True)
        stop = session.cluster/'stop_requested'
        stop.unlink(missing_ok=True)
        if session.finalize:
            for tape, record in tapes.items():
                if all(statuses.get(f'{tape}/{b["block"]}', {}).get('status') == 'complete'
                       for b in record['blocks']):
                    finalize_tape(session.run_dir, config, record)
        events, workers, failures = queue.Queue(), [], []
        stopping, forced, started = False, False, time.time()
        completed = {'windows': 0., 'genus': 0.}

        def request_stop(*_):
            nonlocal stopping, forced
            if stopping:
                forced = True  # Second Ctrl+C: abandon running blocks (never published).
            else:
                output('Stopping: workers finish and publish their current blocks; Ctrl+C again to abandon them')
            stopping = True

        previous = signals.signal(signals.SIGINT, request_stop) if threading.current_thread() is threading.main_thread() else None

        def spawn(host, index):
            name = f'{host}-{index}'
            if worker_factory:
                command = worker_factory(session, host)
            elif host == 'windows':
                command = local_command(session)
            else:
                command = remote_command(session, connection, genus_cpus)
            env = dict(os.environ, PYTHONIOENCODING='utf-8', **{v: '1' for v in THREAD_VARIABLES})
            flags = (subprocess.CREATE_NEW_PROCESS_GROUP | 0x4000) if os.name == 'nt' else 0
            process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT, text=True, encoding='utf-8',
                                       errors='replace', env=env, creationflags=flags)
            item = Worker(name, host, process, session.cluster/'logs'/f'{name}.log')
            threading.Thread(target=read_events, args=(item, events), daemon=True).start()
            workers.append(item)

        def send(item, value):
            try:
                item.process.stdin.write(json.dumps(value)+'\n')
                item.process.stdin.flush()
            except OSError:
                pass

        def save():
            now = time.time()
            elapsed = max(now-started, 1e-9)
            hosts = {}
            for host, limit in (('windows', local_workers), ('genus', genus_workers)):
                mine = [w for w in workers if w.host == host and w.process.poll() is None]
                hosts[host] = dict(workers=len(mine), limit=limit,
                                   active=sum(w.job is not None for w in mine),
                                   tape_hours_completed=round(completed[host]/3600, 4),
                                   tape_hours_per_wall_hour=round(completed[host]/elapsed, 3))
            record = dict(run_id=config['run_id'], updated=now, elapsed_seconds=round(elapsed, 1),
                          stopping=stopping, hosts=hosts, active_tapes=sorted({w.job['tape'] for w in workers if w.job}),
                          tape_limit=tape_limit, counts={s: sum(v['status'] == s for v in statuses.values())
                                                         for s in ('complete', 'pending', 'running', 'failed')},
                          tape_hours_per_wall_hour=round(sum(completed.values())/elapsed, 3),
                          workers=[dict(name=w.name, host=w.host, job=w.job and f'{w.job["tape"]}/{w.job["block"]}',
                                        phase=w.state.get('phase'), channel=w.state.get('channel'),
                                        heartbeat_age=round(now-w.updated, 1), alive=w.process.poll() is None)
                                   for w in workers],
                          failures=failures, jobs=statuses)
            atomic_json(session.cluster/'status.json', record)
            return record

        try:
            if pending:
                for i in range(local_workers):
                    spawn('windows', i)
                for i in range(genus_workers):
                    spawn('genus', i)
            last = printed = 0.
            while any(w.process.poll() is None for w in workers) or not events.empty():
                if stop.exists() or (run_seconds and time.time()-started > run_seconds):
                    stopping = True
                if forced:
                    for item in workers:
                        kill_tree(item.process)
                try:
                    item, event = events.get(timeout=.2)
                except queue.Empty:
                    item, event = None, None
                if item is not None:
                    item.updated = time.time()
                    kind = event.get('event')
                    if kind == 'hello':
                        try:
                            check_hello(item, event, config, genus_cpus)
                            item.hello, item.ready = event, True
                        except IdentityError as exc:
                            failures.append(dict(worker=item.name, message=str(exc)))
                            stopping = True
                            send(item, dict(op='stop'))
                    elif kind == 'progress':
                        item.state = {k: event.get(k) for k in ('phase', 'channel', 'channels', 'key')}
                    elif kind == 'complete' and item.job is not None:
                        job, key = item.job, f'{item.job["tape"]}/{item.job["block"]}'
                        try:
                            record = publish(session, job)
                            statuses[key] = dict(status='complete', host=item.host, worker=item.name,
                                                 seconds=event.get('seconds'), execution=event.get('execution'),
                                                 upload_seconds=event.get('upload_seconds'),
                                                 eligible_windows=record['windows_eligible'])
                            completed[item.host] += job['core_end']-job['core_start']
                            tape = tapes[job['tape']]
                            if session.finalize and all(statuses.get(f'{tape["tape"]}/{b["block"]}', {}).get('status')
                                                        == 'complete' for b in tape['blocks']):
                                finalize_tape(session.run_dir, config, tape)
                                output(f'{tape["tape"]}: joined census stream published')
                        except (OSError, ValueError, KeyError) as exc:
                            statuses[key] = dict(status='failed', host=item.host, message=str(exc))
                            failures.append(dict(key=key, worker=item.name, message=str(exc)))
                            stopping = True
                        item.job = None
                    elif kind == 'error' and item.job is not None:
                        key = f'{item.job["tape"]}/{item.job["block"]}'
                        statuses[key] = dict(status='failed', host=item.host, message=event.get('message'))
                        failures.append(dict(key=key, worker=item.name, message=event.get('message')))
                        output(f'{key} failed on {item.name}: {event.get("message")}')
                        stopping = True
                        item.job = None
                    elif kind == 'exit':
                        item.ready = False
                        if item.job is not None:
                            key = f'{item.job["tape"]}/{item.job["block"]}'
                            statuses[key] = dict(status='interrupted', host=item.host,
                                                 message=f'{item.name} exited; resume verifies its inbox')
                            failures.append(dict(key=key, worker=item.name, message='worker exited during a job'))
                            stopping = True
                            item.job = None
                        elif item.hello is None and not stopping:
                            failures.append(dict(worker=item.name, message='worker exited before start; see its log'))
                            stopping = True
                    elif kind == 'message' and event.get('message'):
                        item.state['message'] = event['message']
                for item in workers:
                    if not item.ready or item.job is not None or item.process.poll() is not None:
                        continue
                    job = None if stopping else select_job(
                        pending, [w.job['tape'] for w in workers if w.job], tape_limit)
                    if job is None:
                        if stopping or not pending:
                            send(item, dict(op='stop'))
                            item.ready = False
                        continue
                    pending.remove(job)
                    item.job = job
                    statuses[f'{job["tape"]}/{job["block"]}'] = dict(status='running', host=item.host,
                                                                      worker=item.name, started=time.time())
                    send(item, dict(op='job', job=job))
                if time.time()-last > 5:
                    state = save()
                    if time.time()-printed > (30 if plain else 10):
                        printed = time.time()
                        output(f'{time.strftime("%H:%M:%S")} complete {state["counts"]["complete"]}/{len(statuses)} '
                               + ' '.join(f'{h}={v["active"]}/{v["workers"]} {v["tape_hours_per_wall_hour"]} h/h'
                                          for h, v in state['hosts'].items())
                               + (' stopping' if stopping else ''))
                    last = time.time()
        finally:
            for item in workers:
                if item.process.poll() is None:
                    send(item, dict(op='stop'))
            for item in workers:
                try:
                    item.process.wait(timeout=None if item.job is None else 3600)
                except subprocess.TimeoutExpired:
                    kill_tree(item.process)
            for key, value in statuses.items():
                if value['status'] == 'running':
                    value.update(status='interrupted')
            state = save()
            if previous is not None:
                signals.signal(signals.SIGINT, previous)
        output(f'Detection session finished: {state["counts"]["complete"]}/{len(statuses)} blocks complete; '
               f'{len(failures)} failures')
        for failure in failures:
            output(f'Failure: {failure}')
        return 0 if not failures and all(v['status'] == 'complete' for v in statuses.values()) else (
            130 if stopping and not failures else 2)


def benchmark(run_dir, config, folders, trial, keys, **pool):
    """Isolated worker-count trial: identical jobs, private inbox and outputs."""
    folder = Path(run_dir)/'benchmarks'/trial
    if folder.exists():
        raise ValueError(f'Benchmark trial already exists: {folder}')
    folder.mkdir(parents=True)
    session = Session(run_dir, config, cluster=folder/'cluster', published=folder/'blocks', finalize=False)
    started = time.time()
    code = coordinate(session, folders, job_filter=set(keys), **pool)
    wall = time.time()-started
    status = read_json(session.cluster/'status.json')
    done = [v for k, v in status['jobs'].items() if k in keys and v['status'] == 'complete' and not v.get('reused')]
    tapes = {read_json(Path(run_dir)/k.split('/')[0]/'inventory.json')['tape'] for k in keys}
    core = 0.
    for key in keys:
        tape, block = key.split('/')
        record = read_json(Path(run_dir)/tape/'inventory.json')
        b = next(b for b in record['blocks'] if b['block'] == block)
        if status['jobs'].get(key, {}).get('status') == 'complete':
            core += b['core_end']-b['core_start']
    report = dict(trial=trial, run_id=config['run_id'], returncode=code, wall_seconds=round(wall, 2),
                  blocks=len(keys), completed=len(done), tapes=sorted(tapes),
                  unique_tape_hours_per_wall_hour=round(core/max(wall, 1e-9), 3),
                  pool={k: v for k, v in pool.items() if k in ('local_workers', 'genus_workers', 'genus_cpus', 'tape_limit')},
                  per_block=[dict(key=k, **{f: v.get(f) for f in ('host', 'seconds', 'upload_seconds', 'execution')})
                             for k, v in status['jobs'].items() if k in keys],
                  failures=status['failures'],
                  note='Record cache state and competing I/O with each trial; compare repeated, reordered trials')
    atomic_json(folder/'report.json', report)
    return report


def compare_trials(run_dir, config, first, second, tolerance=1e-5):
    """Cross-host/pool equivalence: identical gates and curves within tolerance."""
    def blocks(trial):
        root = Path(run_dir)/'benchmarks'/trial/'blocks' if trial != 'run' else Path(run_dir)
        pattern = '*/*/result.json' if trial != 'run' else '*/blocks/*/result.json'
        return {(p.parent.parent.name if trial != 'run' else p.parent.parent.parent.name)+'/'+p.parent.name: p.parent
                for p in root.glob(pattern)}
    a, b = blocks(first), blocks(second)
    rows, worst = [], 0.
    for key in sorted(set(a) & set(b)):
        ra, rb = read_json(a[key]/'result.json'), read_json(b[key]/'result.json')
        gates_a = [(w['start_seconds'], w['eligible'], w.get('reasons')) for w in ra['windows']]
        gates_b = [(w['start_seconds'], w['eligible'], w.get('reasons')) for w in rb['windows']]
        ca, cb = np.load(a[key]/'curves.npy'), np.load(b[key]/'curves.npy')
        difference = float(np.max(abs(ca-cb))) if ca.shape == cb.shape and ca.size else (0. if ca.shape == cb.shape else math.inf)
        worst = max(worst, difference)
        rows.append(dict(key=key, gates_equal=gates_a == gates_b, curve_shape_equal=ca.shape == cb.shape,
                         max_curve_difference=difference,
                         hosts=[ra['execution']['host'], rb['execution']['host']]))
    return dict(first=first, second=second, compared=len(rows), tolerance=tolerance,
                equivalent=bool(rows) and all(r['gates_equal'] and r['max_curve_difference'] <= tolerance for r in rows),
                max_curve_difference=worst, blocks=rows)
