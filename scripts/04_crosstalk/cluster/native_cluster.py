"""Distribute discovery by whole tape, keeping each host's WAV reads on its own SSD."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, nullcontext
import json
from pathlib import Path
import shlex
import subprocess
import sys
import threading

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from repeat_archive import (CANONICAL, DATA, GENUS, HERE, ORIGINALS, SSD, atomic_json,
                            denoised_source, inventory, planning_lock, remote_path, run_jobs,
                            sha256, stage_tape)
from repeat_catalog import compile_channel
from repeat_cluster import Tape, complete, finish_tape


def canonical_sources(tape, report):
    rows = []
    for row in inventory(ORIGINALS/tape)['channels']:
        name = f'CH{row["channel"]:02d}'
        folder = CANONICAL/tape/name
        manifest = folder/'denoised.json'
        if manifest.is_file():
            path, saved = denoised_source(row, folder)
            rows.append(dict(row=row, name=name+'.wav', source=str(path), audio=saved['audio'],
                             manifest_sha256=sha256(manifest), signature=saved['signature']))
    if not rows:
        raise ValueError(f'No completed denoised inputs for {tape}')
    report.mkdir(parents=True, exist_ok=True)
    spec = report/'sources.json'
    if spec.exists():
        saved = json.loads(spec.read_bytes())
        if saved['tape'] != tape or saved['channels'] != rows:
            raise ValueError(f'Changed canonical inputs for {tape}')
    else:
        atomic_json(spec, dict(tape=tape, channels=rows, scanner_sha256=sha256(HERE/'repeat_scan.py')))
    return rows


def remote(command, *, check=True):
    return subprocess.run(['ssh', '-n', '-T', '-o', 'BatchMode=yes', '-o', 'ServerAliveInterval=20',
                           GENUS, shlex.join(command)], stdin=subprocess.DEVNULL, check=check).returncode


def native(action, run, tape):
    remote(['env', 'OPENBLAS_NUM_THREADS=1', 'OMP_NUM_THREADS=1', 'taskset', '-c', '0,2,4,8,10,12',
            '/home/bfeist/.local/bin/uv', 'run', '--no-project', '--python',
            '/home/bfeist/.local/share/apollo15-numba-venv/bin/python', 'python', '-u',
            remote_path(Path(__file__).with_name('genus_scratch.py')), action, run, tape])


def remote_seal(report, rows):
    sources = sha256(report/'sources.json')
    for entry in rows:
        ch = Path(entry['name']).stem
        scan_hash = None
        for mode, ext in [('scan', '.npy'), ('validate', '.json')]:
            record = json.loads((report/mode/(ch+'.receipt.json')).read_bytes())
            identity = dict(code=sha256(report.parent/'implementation'/f'repeat_{mode}.py'), sources=sources)
            if mode == 'validate':
                identity['scan'] = scan_hash
            result_hash = sha256(report/mode/(ch+ext))
            if record['identity'] != identity or record['sha256'] != result_hash:
                raise ValueError(f'Invalid {mode} receipt for {report.name} {ch}')
            if mode == 'scan':
                scan_hash = result_hash
        compile_channel(report, entry['row']['channel'], report/'catalog'/ch)
    files = {p.relative_to(report).as_posix(): sha256(p) for p in report.rglob('*')
             if p.is_file() and p.suffix in ('.json', '.npy') and p.name != 'detected.json'}
    atomic_json(report/'detected.json', dict(channels=len(rows), files=files))
    if not complete(report):
        raise ValueError(f'Unsealed tape {report}')


def wait_windows(pid):
    if pid:
        subprocess.run(['powershell', '-NoProfile', '-Command',
                        f'Get-Process -Id {int(pid)} -ErrorAction SilentlyContinue | Wait-Process'],
                       check=False)


def main(args):
    run = args.run
    base = DATA/run
    transfer = threading.Lock()
    wait_windows(args.drain_pid)
    with planning_lock(base/'coordinator'):
        for name in ('repeat_scan.py','repeat_validate.py','repeat_catalog.py'):
            if sha256(HERE/name) != sha256(base/'implementation'/name):
                raise ValueError(f'Changed scientific implementation {name}')
        pending = [p.name for p in sorted(CANONICAL.glob('A15_T*')) if p.is_dir() and not complete(base/p.name)]
        genus = [t for t in pending if t in args.genus_tape]
        windows = [t for t in pending if t not in genus]
        print(f'Windows local SSD {windows}. Genus native SSD {genus}.', flush=True)

        def local_work():
            for tape in windows:
                scratch, report = SSD/run/tape, base/tape
                with planning_lock(scratch):
                    # A fully staged tape only needs a local integrity check.
                    with nullcontext() if (scratch/'sources.json').is_file() else transfer:
                        rows = stage_tape(tape, scratch, report)
                    for mode in ('scan','validate'):
                        run_jobs(tape, scratch, report, rows, mode, 2, 0)
                    finish_tape(Tape(tape, report, scratch, ExitStack(), rows))
                print(f'Detection complete {tape} Windows', flush=True)

        def prefetch(tape):
            with transfer:
                if tape == args.prefetched_tape:
                    wait_windows(args.prefetch_pid)
                marker = f'/home/bfeist/.local/share/apollo15-crosstalk/{run}/{tape}/prefetch.json'
                if remote(['test','-f',marker],check=False) != 0:
                    native('prefetch',run,tape)

        def native_work():
            with ThreadPoolExecutor(max_workers=1) as copier:
                current = copier.submit(prefetch, genus[0]) if genus else None
                for i, tape in enumerate(genus):
                    current.result()
                    if i+1 < len(genus):
                        current = copier.submit(prefetch, genus[i+1])
                    report = base/tape
                    with planning_lock(SSD/run/tape):
                        rows = canonical_sources(tape,report)
                        native('process',run,tape)
                        remote_seal(report,rows)
                        native('reclaim',run,tape)
                    print(f'Detection complete {tape} Genus SSD', flush=True)

        with ThreadPoolExecutor(max_workers=2) as hosts:
            jobs = [hosts.submit(f) for f in (native_work,local_work)]
            for job in jobs:
                job.result()
        if not all(complete(base/t) for t in pending):
            raise ValueError('Incomplete detection handoff')
        print('Discovery complete. Qualified release owns archive rendering.', flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', required=True)
    parser.add_argument('--genus-tape', action='append', required=True)
    parser.add_argument('--prefetched-tape')
    parser.add_argument('--prefetch-pid', type=int)
    parser.add_argument('--drain-pid', type=int)
    args = parser.parse_args()
    if not args.run.isascii() or not args.run.replace('-','').replace('_','').isalnum():
        parser.error('Use an archive run name')
    main(args)
