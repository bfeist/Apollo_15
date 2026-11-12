"""Operational safeguards around the unchanged, qualified workers.

Unknown SSD files remain untouched and are reported. Reclaiming an already
acknowledged result must not prevent another channel from resuming its render.
Brief Windows reader locks must not abort an atomic checkpoint or status commit.
This entry point changes no DSP, checkpoint identity, or publication checks.
"""

# Resolve shared modules from the stage folders.
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))
from _bootstrap import install as _install
_install()

from contextlib import contextmanager
import errno
import os
from pathlib import Path, PurePosixPath
import runpy
import shlex
import sys
import time
import traceback

import denoise_archive as archive
import denoise_v2_cluster as cluster
import denoise_v2_production as production
from _bootstrap import LAYOUT, source_path

WORKER_SCRIPTS = ('denoise_v2_cluster.py', 'denoise_v2_accelerated.py')
REPLACE_DELAYS = (.05, .1, .2, .4, .8, 1., 1.)
# Each worker keeps its slot while recovering; other workers continue normally.
# About fifteen minutes of backoff, with ten-second heartbeats and one-second stop checks.
STORAGE_DELAYS = (2, 5, 10, 20, 30, *([60]*14))
STORAGE_ERRORS = {errno.EIO, errno.ESTALE, errno.ETIMEDOUT, errno.ECONNRESET,
                  errno.ECONNABORTED, errno.ENOTCONN, errno.ENETUNREACH,
                  errno.EHOSTUNREACH}


class UploadCopies:
    """Use ordinary buffered writes for this worker's private CIFS upload only."""
    def __init__(self, original, pending):
        self.original, self.pending = original, pending

    def __getattr__(self, name):
        return getattr(self.original, name)

    def copyfile(self, source, destination, **kwargs):
        destination = Path(destination)
        if destination.parent != self.pending:
            return self.original.copyfile(source, destination, **kwargs)
        # Linux copyfile can select sendfile/copy_file_range across local/CIFS
        # filesystems. Keep that fast path out of the upload; never change the
        # qualified renderer or any completed local result.
        source, destination = cluster.exact_path(source), cluster.exact_path(destination)
        if source == destination:
            raise self.original.SameFileError(f'{source} and {destination} are the same file')
        # A CIFS error can arrive on the final flush even when every byte landed.
        # Reuse a fully matching private upload instead of rewriting a multi-GB
        # WAV on every attempt. Reopen and flush it again before allowing handoff.
        if (destination.is_file() and source.stat().st_size == destination.stat().st_size
                and cluster.sha256(source) == cluster.sha256(destination)):
            with destination.open('r+b') as writer:
                os.fsync(writer.fileno())
            return str(destination)
        with source.open('rb') as reader, destination.open('wb') as writer:
            self.original.copyfileobj(reader, writer, length=1 << 20)
            writer.flush()
            os.fsync(writer.fileno())
        return str(destination)


def retry_storage_worker(worker, config, key, host):
    """Resume only known Linux I/O failures; identity/quality errors still fail."""
    if host != 'genus':
        return worker(config, key, host)
    folder = cluster.session_path(config)
    stop = folder/'stop_requested'
    original_copies = cluster.shutil
    original_emit = cluster.emit
    progress = {}
    def emit(**value):
        if value.get('event') == 'progress':
            if value.get('phase') == 'uploading':
                value['cursor'] = value['frames']
            progress.update({k:value[k] for k in ('cursor', 'frames') if k in value})
        original_emit(**value)
    cluster.emit = emit
    cluster.shutil = UploadCopies(original_copies, (folder/'inbox'/key).with_name(Path(key).name+'.upload'))
    try:
        for attempt in range(len(STORAGE_DELAYS)+1):
            try:
                # The unchanged writer validates/reuses a completed SSD result,
                # or verifies its committed checkpoint before resuming. A failed
                # upload remains private until the original atomic inbox rename.
                return worker(config, key, host)
            except OSError as exc:
                if exc.errno not in STORAGE_ERRORS or attempt == len(STORAGE_DELAYS):
                    traceback.print_exc()
                    raise
                delay = STORAGE_DELAYS[attempt]
                traceback.print_exc()
                original_emit(event='message', host=host, key=key, phase='storage retry', **progress,
                    message=f'Transient storage error: {exc}; retry {attempt+1}/{len(STORAGE_DELAYS)} '
                            f'in {delay}s; retaining completed audio/checkpoints')
                for remaining in range(delay, 0, -1):
                    try:
                        requested = stop.exists()
                    except OSError as stop_error:
                        if stop_error.errno not in STORAGE_ERRORS:
                            raise
                        requested = False  # The shared stop flag may be unreachable too.
                    if requested:
                        return 75  # Same graceful-checkpoint exit as the writer.
                    if remaining % 10 == 0:
                        original_emit(event='progress', host=host, key=key, phase='storage retry', **progress)
                    time.sleep(1)
    finally:
        cluster.shutil = original_copies
        cluster.emit = original_emit


@contextmanager
def resilient_worker_io():
    original = cluster.worker
    def worker(config, key, host):
        return retry_storage_worker(original, config, key, host)
    cluster.worker = worker
    try:
        yield
    finally:
        cluster.worker = original


def retry_atomic_json(write, path, value):
    """Retry transient Windows file locks, keeping the old JSON record atomic."""
    for attempt in range(len(REPLACE_DELAYS)+1):
        try:
            return write(path, value)
        except OSError as exc:
            if getattr(exc, 'winerror', None) not in (5, 32, 33) or attempt == len(REPLACE_DELAYS):
                raise
            if attempt == 0:
                print(f'Atomic JSON write temporarily blocked: {path}; retrying: {exc}',
                      file=sys.stderr, flush=True)
            time.sleep(REPLACE_DELAYS[attempt])


@contextmanager
def resilient_json_writes():
    # Used by the console, coordinator and workers. Patch imported aliases as
    # well as the source. The accelerated writer is loaded privately later and
    # imports this archive function at construction.
    originals = {module:module.atomic_json for module in (archive, cluster, production)}
    for module, original in originals.items():
        def write(path, value, original=original):
            return retry_atomic_json(original, path, value)
        module.atomic_json = write
    try:
        yield
    finally:
        for module, original in originals.items():
            module.atomic_json = original


class WorkerProcesses:
    """Route local workers through the same safeguards as remote workers."""
    def __init__(self, original):
        self.original = original

    def __getattr__(self, name):
        return getattr(self.original, name)

    def Popen(self, command, *args, **kwargs):
        if (isinstance(command, (list, tuple)) and len(command) >= 4
                and command[0] == sys.executable and command[1] == '-u'
                and command[3] == 'worker'):
            script = Path(command[2])
            if script.name in WORKER_SCRIPTS and script.absolute() == source_path(script.name):
                command = [*command[:2], str(Path(__file__).resolve()), script.name, *command[3:]]
        return self.original.Popen(command, *args, **kwargs)


def remote_worker_command(command, config):
    argv = shlex.split(command[-1])
    scripts = PurePosixPath(config['remote_repo'])/'scripts'
    matches = [i for i, arg in enumerate(argv)
               if arg in {str(scripts/name) for name in WORKER_SCRIPTS}]
    if len(matches) != 1 or 'worker' not in argv:
        raise ValueError('Expected a qualified remote worker command')
    index = matches[0]
    original = PurePosixPath(argv[index]).name
    argv[index:index+1] = [str(scripts/LAYOUT[Path(__file__).name]), original]
    return [*command[:-1], shlex.join(argv)]


def release_acknowledged(config, root):
    """Keep the frozen cleanup checks, but defer directories with extra files."""
    root = cluster.exact_path(root)
    for ack in (cluster.session_path(config)/'acks').glob('*/CH*.json'):
        key = Path(ack.parent.name)/ack.stem
        completed = cluster.exact_path(root/key)
        if not completed.is_dir():
            continue
        try:
            with cluster.planning_lock(root.parent/'work'/key):
                manifest = completed/'denoised.json'
                if not manifest.exists():
                    continue
                record = cluster.read_json(manifest)
                if cluster.read_json(ack)['audio_sha256'] != record['audio']['sha256']:
                    raise cluster.IdentityError('Acknowledgement differs from SSD output')
                if root not in completed.parents:
                    raise cluster.IdentityError('SSD cleanup escaped output root')
                filename = record['audio']['filename']
                if Path(filename).name != filename or '/' in filename or '\\' in filename:
                    raise cluster.IdentityError('Invalid SSD audio filename')
                allowed = {filename, 'denoised.json', 'cleanup.jsonl'}
                present = {p.name for p in completed.iterdir()}
                if present != allowed:
                    print(f'SSD cleanup deferred: {completed}; '
                          f'extra={sorted(present-allowed)!r}; '
                          f'missing={sorted(allowed-present)!r}. '
                          'Retained all files; continuing worker.', file=sys.stderr, flush=True)
                    continue
                # Resolve every file before deleting any; never follow redirects.
                paths = [cluster.exact_path(completed/name) for name in allowed]
                if not all(path.is_file() for path in paths):
                    raise cluster.IdentityError(f'Unexpected SSD output entries: {completed}')
                for path in paths:
                    path.unlink()
                completed.rmdir()
        except ValueError as exc:
            if 'Another planner' not in str(exc):
                raise


def main():
    if len(sys.argv) < 3 or sys.argv[1] not in WORKER_SCRIPTS or sys.argv[2] != 'worker':
        raise ValueError('Expected a qualified worker script followed by worker arguments')
    script = source_path(sys.argv[1])
    original_cleanup, original_argv = cluster.release_acknowledged, sys.argv
    cluster.release_acknowledged = release_acknowledged
    sys.argv = [str(script), *sys.argv[2:]]
    try:
        with resilient_json_writes(), resilient_worker_io():
            return run_worker(script)
    finally:
        cluster.release_acknowledged = original_cleanup
        sys.argv = original_argv


def run_worker(script):
    if script.name == 'denoise_v2_cluster.py':
        # Run the imported module so worker() sees the cleanup replacement.
        # run_path would define a second, unpatched release_acknowledged.
        try:
            return cluster.main()
        except (ValueError, OSError, KeyError, RuntimeError, cluster.subprocess.CalledProcessError) as exc:
            print(f'Cluster stopped: {exc}', file=sys.stderr, flush=True)
            return 2
    runpy.run_path(str(script), run_name='__main__')


if __name__ == '__main__':
    raise SystemExit(main())
