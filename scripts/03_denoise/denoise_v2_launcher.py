"""Qualified backend and affinity launcher around the frozen coordinator.

Keep reference worker files/config intact. An explicit accelerated execution
adapter preserves checkpoint prefixes and records the backend for new spans.
"""

# Resolve shared modules from the stage folders.
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))
from _bootstrap import install as _install
_install()

import argparse
from pathlib import Path
import shlex
import subprocess
import sys

import denoise_v2_cluster as cluster
import denoise_v2_worker_runtime as worker_runtime
from _bootstrap import ScriptProcesses
from denoise_v2_verification import cached_verification

GENUS_CPUS = '0,2,4,8,10,12'


def cpu_list(value):
    if value == 'none':
        return value
    parts = value.split(',')
    if not all(p.isascii() and p.isdecimal() for p in parts):
        raise argparse.ArgumentTypeError('Use comma-separated CPU IDs, or none')
    cpus = [int(p) for p in parts]
    if len(set(cpus)) != len(cpus):
        raise argparse.ArgumentTypeError('CPU IDs must be unique')
    return ','.join(map(str, cpus))


def with_affinity(command, cpus):
    if cpus == 'none':
        return command
    if not command[-1].startswith('exec '):
        raise ValueError('Expected an exec-based remote command')
    return [*command[:-1], 'exec '+shlex.join(['taskset', '-c', cpus])+' '+command[-1][5:]]


def check_affinity(config, cpus):
    """Confirm taskset and the exact inherited CPU mask before starting workers."""
    if cpus == 'none':
        return
    expected = [int(p) for p in cpus.split(',')]
    probe = ('import os; actual=sorted(os.sched_getaffinity(0)); '
             f'assert actual == {sorted(expected)!r}, actual; '
             'print("Genus worker CPUs: " + ",".join(map(str, actual)))')
    command = cluster.remote_command(config, 'worker')
    command[-1] = 'exec '+shlex.join([config['remote_python'], '-c', probe])
    result = subprocess.run(with_affinity(command, cpus), check=True,
                            capture_output=True, text=True, timeout=20)
    print(result.stdout.strip(), flush=True)


def coordinate(args):
    if args.genus_cpus != 'none' and args.genus_workers > len(args.genus_cpus.split(',')):
        raise ValueError('Genus workers exceed the CPU mask; expand --genus-cpus or use --genus-cpus none')
    accelerated = None
    config = None
    requested_backend = getattr(args, 'backend', None)
    if requested_backend is not None:
        config = cluster.read_json(args.config)
    if requested_backend == 'numba' or (requested_backend == 'reference' and
            (cluster.session_path(config)/'active_accelerator.json').exists()):
        import denoise_v2_accelerated as accelerated
        config = cluster.read_json(args.config)
        execution_id, execution = accelerated.active_execution(config)
        accelerated.load_execution(config, execution_id, 'windows')
    if args.genus_workers:
        check_affinity(config or cluster.read_json(args.config), args.genus_cpus)
    original = cluster.remote_command
    original_processes = cluster.subprocess

    def remote_command(config, *arguments):
        command = original(config, *arguments)
        if accelerated and arguments and arguments[0] == 'worker':
            command = accelerated.remote_worker_command(command, config, execution_id, execution, requested_backend)
        if arguments and arguments[0] == 'worker':
            command = worker_runtime.remote_worker_command(command, config)
        return (with_affinity(command, args.genus_cpus)
                if arguments and arguments[0] == 'worker' else command)

    # The frozen coordinator resolves this function for every new worker,
    # including replacement jobs. A coordinator-local proxy redirects local
    # workers too; qualification and subprocesses elsewhere remain unchanged.
    cluster.remote_command = remote_command
    processes = worker_runtime.WorkerProcesses(ScriptProcesses(original_processes))
    if accelerated:
        cluster.subprocess = accelerated.WorkerProcesses(processes, execution_id, requested_backend)
        print('Qualified accelerated execution: '+execution_id, flush=True)
    else:
        cluster.subprocess = processes
    try:
        with worker_runtime.resilient_json_writes(), \
             cached_verification(config or cluster.read_json(args.config)):
            return cluster.coordinate(args)
    finally:
        cluster.remote_command = original
        cluster.subprocess = original_processes


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--local-workers', type=int, default=6)
    parser.add_argument('--genus-workers', type=int, default=6)
    parser.add_argument('--genus-cpus', type=cpu_list, default=GENUS_CPUS)
    parser.add_argument('--backend', choices=['reference', 'numba'], default='numba')
    parser.add_argument('--tapes', type=int, default=3)
    parser.add_argument('--plain', action='store_true')
    parser.add_argument('--run-seconds', type=float)
    return coordinate(parser.parse_args())


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (ValueError, OSError, KeyError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f'Launcher stopped: {exc}', file=sys.stderr, flush=True)
        raise SystemExit(2)
