"""Best-effort SSD housekeeping around the unchanged, qualified workers.

Unknown SSD files remain untouched and are reported. Reclaiming an already
acknowledged result must not prevent another channel from resuming its render.
This entry point changes no DSP, checkpoint identity, or publication checks.
"""

# Resolve shared modules from the stage folders.
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))
from _bootstrap import install as _install
_install()

from pathlib import Path, PurePosixPath
import runpy
import shlex
import sys

import denoise_v2_cluster as cluster
from _bootstrap import LAYOUT, source_path

WORKER_SCRIPTS = ('denoise_v2_cluster.py', 'denoise_v2_accelerated.py')


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
        if script.name == 'denoise_v2_cluster.py':
            # Run the imported module so worker() sees the cleanup replacement.
            # run_path would define a second, unpatched release_acknowledged.
            try:
                return cluster.main()
            except (ValueError, OSError, KeyError, RuntimeError, cluster.subprocess.CalledProcessError) as exc:
                print(f'Cluster stopped: {exc}', file=sys.stderr, flush=True)
                return 2
        runpy.run_path(str(script), run_name='__main__')
    finally:
        cluster.release_acknowledged = original_cleanup
        sys.argv = original_argv


if __name__ == '__main__':
    raise SystemExit(main())
