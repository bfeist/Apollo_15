"""Keep fixed Windows and Genus worker pools busy across up to three staged tapes."""
import argparse
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import ExitStack
from dataclasses import dataclass, field
import heapq
import itertools
import json
from pathlib import Path

from repeat_archive import CANONICAL, DATA, SSD, atomic_json, planning_lock, run_jobs, sha256, snapshot_sources, stage_tape
from repeat_catalog import compile_channel
from repeat_render import render_archive


@dataclass
class Tape:
    name: str
    report: Path
    scratch: Path
    lease: ExitStack
    rows: list = field(default_factory=list)
    remaining: int = 0


def complete(folder):
    seal = folder/'detected.json'
    if not seal.exists():
        return False
    saved = json.loads(seal.read_bytes())
    for name, digest in saved['files'].items():
        if sha256(folder/name) != digest:
            raise ValueError(f'Changed detection artifact {folder/name}')
    return True


def finish_tape(tape):
    for entry in tape.rows:
        channel = entry['row']['channel']
        compile_channel(tape.report, channel, tape.report/'catalog'/f'CH{channel:02d}')
    files = {p.relative_to(tape.report).as_posix(): sha256(p) for p in tape.report.rglob('*')
             if p.is_file() and p.suffix in ('.json', '.npy') and p.name != 'detected.json'}
    atomic_json(tape.report/'detected.json', dict(channels=len(tape.rows), files=files))
    for entry in tape.rows:
        (tape.scratch/entry['name']).unlink()
    (tape.scratch/'sources.json').unlink()
    tape.scratch.rmdir()


def detect(base, tapes, local, genus, tape_limit):
    scratch_root = SSD/base.name
    pending = [name for name in tapes if not complete(base/name)]
    # Pick up any fully or partially staged tapes first on a resume.
    pending.sort(key=lambda t: (not (scratch_root/t/'sources.json').is_file(), not (scratch_root/t).exists(), t))
    ready, futures, active = [], {}, {}
    sequence = itertools.count()
    available = dict(windows=local, genus=genus)
    staging = False

    def enqueue(tape, entry, mode):
        # Start expensive validations early. Their sample counts are already
        # reflected in the size of the verified per-channel scan array.
        weight = 0
        if mode == 'validate':
            weight = -(tape.report/'scan'/Path(entry['name']).with_suffix('.npy')).stat().st_size
        heapq.heappush(ready, (weight, next(sequence), tape.name, entry, mode))

    with ExitStack() as leases, ThreadPoolExecutor(max_workers=1) as copier, \
            ThreadPoolExecutor(max_workers=1) as cataloger, ThreadPoolExecutor(max_workers=local+genus) as workers:
        while pending or active:
            if pending and len(active) < tape_limit and not staging:
                name = pending.pop(0)
                lease = leases.enter_context(ExitStack())
                tape = Tape(name, base/name, scratch_root/name, lease)
                lease.enter_context(planning_lock(tape.scratch))
                active[name] = tape
                print(f'Staging {name}', flush=True)
                future = copier.submit(stage_tape, name, tape.scratch, tape.report)
                futures[future] = ('stage', tape, None, None)
                staging = True
            while ready and any(available.values()):
                _, _, name, entry, mode = heapq.heappop(ready)
                tape = active[name]
                host = max(available, key=available.get)
                available[host] -= 1
                future = workers.submit(run_jobs, name, tape.scratch, tape.report, [entry], mode,
                                        int(host == 'windows'), int(host == 'genus'))
                futures[future] = (mode, tape, entry, host)
            done, _ = wait(futures, return_when=FIRST_COMPLETED)
            for future in done:
                mode, tape, entry, host = futures.pop(future)
                result = future.result()
                if host:
                    available[host] += 1
                if mode == 'stage':
                    staging = False
                    tape.rows = result
                    tape.remaining = len(result)
                    for row in result:
                        enqueue(tape, row, 'scan')
                elif mode == 'scan':
                    enqueue(tape, entry, 'validate')
                elif mode == 'validate':
                    tape.remaining -= 1
                    if not tape.remaining:
                        futures[cataloger.submit(finish_tape, tape)] = ('seal', tape, None, None)
                elif mode == 'seal':
                    tape.lease.close()
                    del active[tape.name]
                    print(f'Detection complete {tape.name} {len(tape.rows)} channels', flush=True)
    if not all(complete(base/t) for t in tapes):
        raise ValueError('Archive detection incomplete')


def run(base, tapes, local=6, genus=6, tape_limit=3):
    from repeat_qualification import immutable_copy

    with planning_lock(base/'coordinator'):
        snapshot_sources(base/'implementation', immutable_copy)
        detect(base, tapes, local, genus, tape_limit)
        render_archive(base, tapes, min(4, max(1, local)))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', required=True)
    parser.add_argument('--local', type=int, default=6)
    parser.add_argument('--genus', type=int, default=6)
    parser.add_argument('--tapes', type=int, default=3, help='Maximum simultaneously staged tapes')
    args = parser.parse_args()
    if (not args.run.isascii() or not args.run.replace('-', '').replace('_','').isalnum()
            or not 0 <= args.local <= 6 or not 0 <= args.genus <= 6 or not args.local+args.genus
            or not 1 <= args.tapes <= 3):
        parser.error('Use a simple run name, 0–6 workers per host, and 1–3 staged tapes')
    names = sorted(p.name for p in CANONICAL.glob('A15_T*') if p.is_dir())
    if not names:
        parser.error('No tapes in the stage-03 archive')
    run(DATA/args.run, names, args.local, args.genus, args.tapes)
