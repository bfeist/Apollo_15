"""Stage verified v2 tapes on F and distribute the within-target census over Windows and Genus."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import queue
import shlex
import shutil
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _bootstrap import GENUS_SSH as GENUS, install
install()
from denoise_archive import CANONICAL, ORIGINALS, atomic_json, sha256
from flutter_planning import inventory
from flutter_plan_store import planning_lock
from crosstalk_stage import denoised_source


HERE = Path(__file__).resolve().parent
DATA = CANONICAL.parent/'crosstalk_detection'
SSD = Path('F:/Apollo15_crosstalk_stage')
COORDINATOR_SOURCES = {'repeat_archive.py', 'repeat_cluster.py', 'repeat_release.py'}
# The qualification source changed only in how it snapshots coordinator files.
QUALIFICATION_SNAPSHOT = (
    '1c4e50a9ead3a4dd192d54c63841f581df3e1eb645a963b6b408395945ebbad0',
    '0ac4ea064e32b8451846626a1526f8a9b1b1e2c766ee272a3703b2cc1d06bd70',
)


def snapshot_sources(folder, copy):
    """Keep the original coordinator snapshot and verify worker sources on resume."""
    for path in sorted(HERE.glob('*.py')):
        frozen = folder/path.name
        if path.name in COORDINATOR_SOURCES and frozen.is_file():
            continue
        if (path.name == 'repeat_qualification.py' and frozen.is_file()
                and (sha256(frozen), sha256(path)) == QUALIFICATION_SNAPSHOT):
            continue
        copy(path, frozen)


def remote_path(path):
    path = path.resolve()
    if path.drive.upper() not in ('F:', 'D:'):
        raise ValueError(f'No Genus mount for {path}')
    return '/mnt/'+path.drive[0].lower()+'/'+path.relative_to(path.anchor).as_posix()


def capture_coordinates(inv):
    from capture_highband import check
    from flutter_alignment import ROOT
    from tape_coordinates import aligned_inventory

    path, assessment = check(ORIGINALS/inv['tape'], ROOT)
    if assessment['action'] == 'duplicate_feed':
        return {}
    physical = aligned_inventory(inv, ROOT)
    shifts = {str(r['channel']): round((r['start_seconds']-r['acquisition_start_seconds'])*r['sample_rate'])
              for r in physical['channels']}
    return dict(clock_shift_frames=shifts, alignment=dict(path=str(path), sha256=sha256(path)))


def stage_tape(tape, scratch, report):
    inv = inventory(ORIGINALS/tape)
    rows = []
    for row in inv['channels']:
        folder = CANONICAL/tape/f'CH{row["channel"]:02d}'
        if not (folder/'denoised.json').is_file():
            continue
        path, saved = denoised_source(row, folder)
        name = f'CH{row["channel"]:02d}.wav'
        rows.append(dict(row=row, name=name, source=str(path), audio=saved['audio'],
                         manifest_sha256=sha256(folder/'denoised.json'), signature=saved['signature']))
    if not rows:
        raise ValueError(f'No completed denoised channels for {tape}')
    report.mkdir(parents=True, exist_ok=True)
    record = dict(tape=tape, channels=rows, scanner_sha256=sha256(HERE/'repeat_scan.py'))
    record.update(capture_coordinates(inv))
    prior = DATA.parent/'crosstalk_qualification'/'repeat-v2'/tape/'catalog'
    if report.parent.name != 'repeat-v2' and prior.is_dir():
        record['prior_catalog'] = str(prior)
    manifest = report/'sources.json'
    if manifest.exists():
        existing = json.loads(manifest.read_bytes())
        if (existing['channels'] != rows or existing['tape'] != tape or
                existing.get('clock_shift_frames') != record.get('clock_shift_frames') and
                (any(record.get('clock_shift_frames',{}).values()) or 'alignment' in existing) or
                'alignment' in existing and existing['alignment'] != record.get('alignment')):
            raise ValueError(f'Inputs changed for {report}')
        # The inventory records the scanner at staging time. Individual job
        # receipts bind the implementation that actually processed the WAV.
        record = existing
    else:
        atomic_json(manifest, record)
    scratch.mkdir(parents=True, exist_ok=True)
    for entry in rows:
        dst = scratch/entry['name']
        expected = entry['audio']['sha256']
        if dst.exists():
            if sha256(dst) != expected:
                raise ValueError(f'Stale staged input {dst}')
            continue
        if shutil.disk_usage(scratch).free < entry['audio']['bytes']:
            raise OSError(f'Insufficient SSD space at {scratch}')
        partial = dst.with_suffix('.partial')
        digest = hashlib.sha256()
        with Path(entry['source']).open('rb') as src, partial.open('wb') as out:
            for block in iter(lambda: src.read(4<<20), b''):
                out.write(block)
                digest.update(block)
            out.flush()
            os.fsync(out.fileno())
        if digest.hexdigest() != expected or sha256(partial) != expected:
            raise ValueError(f'Source or staged hash mismatch {dst}')
        partial.replace(dst)
    atomic_json(scratch/'sources.json', record)
    return rows


def run_jobs(tape, scratch, report, rows, mode, local, genus):
    script = HERE/('repeat_scan.py' if mode == 'scan' else 'repeat_validate.py')
    pending = queue.Queue()
    results = report/mode
    results.mkdir(exist_ok=True)
    for entry in rows:
        ch = Path(entry['name']).stem
        output = results/(ch+('.npy' if mode == 'scan' else '.json'))
        receipt = results/(ch+'.receipt.json')
        identity = dict(code=sha256(script), sources=sha256(report/'sources.json'))
        if mode == 'validate':
            identity['scan'] = sha256(report/'scan'/(ch+'.npy'))
        if receipt.exists():
            saved = json.loads(receipt.read_bytes())
            if saved['identity'] != identity or sha256(output) != saved['sha256']:
                raise ValueError(f'Stale worker output {output}')
            continue
        pending.put((entry, output, receipt, identity))
    stop = threading.Event()

    def worker(remote):
        while not stop.is_set():
            try:
                entry, output, receipt, identity = pending.get_nowait()
            except queue.Empty:
                return
            try:
                args = [script, scratch/entry['name'], output]
                if mode == 'validate':
                    args += [report/'scan'/(output.stem+'.npy')]
                if remote:
                    command = ['env', 'OPENBLAS_NUM_THREADS=1', 'OMP_NUM_THREADS=1', 'taskset', '-c',
                               '0,2,4,8,10,12', '/home/bfeist/.local/bin/uv', 'run', '--no-project',
                               '--python', '/home/bfeist/.local/share/apollo15-numba-venv/bin/python',
                               'python', '-u', *map(remote_path, args)]
                    command = ['ssh', '-n', '-T', '-o', 'BatchMode=yes', '-o', 'ServerAliveInterval=20',
                               GENUS, shlex.join(command)]
                else:
                    command = [sys.executable, '-u', *map(str, args)]
                with output.with_suffix('.log').open('w', encoding='utf-8') as log:
                    completed = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                               stderr=subprocess.STDOUT,
                                               env=dict(os.environ, OPENBLAS_NUM_THREADS='1', OMP_NUM_THREADS='1'))
                    log.write(completed.stdout.decode('utf-8', errors='replace'))
                    completed.check_returncode()
                host = 'Genus' if remote else 'Windows'
                atomic_json(receipt, dict(identity=identity, sha256=sha256(output), host=host))
                print(f'{mode} {tape} {output.stem} {host}', flush=True)
            except BaseException:
                stop.set()
                raise
    with ThreadPoolExecutor(max_workers=local+genus) as pool:
        futures = [pool.submit(worker, remote) for remote in [False]*local+[True]*genus]
        for future in futures:
            future.result()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', required=True)
    parser.add_argument('--mode', choices=['scan', 'validate', 'archive'], default='archive')
    parser.add_argument('--local', type=int, default=6)
    parser.add_argument('--genus', type=int, default=6)
    parser.add_argument('--tape', action='append')
    parser.add_argument('--retain', action='store_true')
    args = parser.parse_args()
    if not args.run.isascii() or not args.run.replace('-', '').replace('_', '').isalnum():
        parser.error('Use a simple run name')
    if not 0 <= args.local <= 6 or not 0 <= args.genus <= 6 or args.local+args.genus == 0:
        parser.error('Use 0 to 6 workers per host and at least one worker overall')
    tapes = sorted(p.name for p in CANONICAL.glob('A15_T*') if p.is_dir())
    if args.tape and set(args.tape)-set(tapes):
        parser.error('Unknown tape')
    selected = args.tape or sorted(tapes, key=lambda t: (t != 'A15_T510083', t))
    if args.mode == 'archive':
        with planning_lock(DATA/args.run/'coordinator'):
            archive(args, selected)
        return
    for tape in selected:
        report = DATA/args.run/tape
        scratch = SSD/args.run/tape
        with planning_lock(scratch):
            print(f'Staging {tape}', flush=True)
            rows = stage_tape(tape, scratch, report)
            run_jobs(tape, scratch, report, rows, args.mode, args.local, args.genus)
            if not args.retain:
                for row in rows:
                    (scratch/row['name']).unlink()
                (scratch/'sources.json').unlink()
                scratch.rmdir()
            print(f'Finished {args.mode} {tape}', flush=True)


def archive(args, tapes):
    from repeat_catalog import compile_channel
    from repeat_qualification import immutable_copy

    base = DATA/args.run
    snapshot_sources(base/'implementation', immutable_copy)
    # One sequential D reader stages the next tape while both hosts analyze
    # the current one. At most two whole tapes occupy the shared SSD.
    def prepare(tape):
        scratch, report = SSD/args.run/tape, base/tape
        print(f'Staging {tape}', flush=True)
        with planning_lock(scratch):
            return stage_tape(tape, scratch, report)

    def finished(tape):
        folder = base/tape
        seal = folder/'detected.json'
        if not seal.exists():
            return False
        saved = json.loads(seal.read_bytes())
        for name, digest in saved['files'].items():
            if sha256(folder/name) != digest:
                raise ValueError(f'Changed archive detection artifact {folder/name}')
        return True

    remaining = [t for t in tapes if not finished(t)]
    with ThreadPoolExecutor(max_workers=1) as copier:
        future = copier.submit(prepare, remaining[0]) if remaining else None
        for i, tape in enumerate(remaining):
            rows = future.result()
            if i+1 < len(remaining):
                future = copier.submit(prepare, remaining[i+1])
            report, scratch = base/tape, SSD/args.run/tape
            with planning_lock(scratch):
                for mode in ('scan', 'validate'):
                    run_jobs(tape, scratch, report, rows, mode, args.local, args.genus)
                for entry in rows:
                    ch = entry['row']['channel']
                    compile_channel(report, ch, report/'catalog'/f'CH{ch:02d}')
                files = {p.relative_to(report).as_posix(): sha256(p) for p in report.rglob('*')
                         if p.is_file() and p.suffix in ('.json', '.npy') and p.name != 'detected.json'}
                atomic_json(report/'detected.json', dict(channels=len(rows), files=files))
                if not args.retain:
                    for entry in rows:
                        (scratch/entry['name']).unlink()
                    (scratch/'sources.json').unlink()
                    scratch.rmdir()
                print(f'Detection complete {tape} {len(rows)} channels', flush=True)
    # An archive-wide barrier keeps rendering behind the complete census.
    if not all(finished(tape) for tape in tapes):
        raise ValueError('Archive detection incomplete')
    from repeat_render import render_archive
    render_archive(base, tapes, min(4, max(1, args.local)))


if __name__ == '__main__':
    main()
