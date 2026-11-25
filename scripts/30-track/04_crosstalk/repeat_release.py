"""Discover, qualify and publish the complete archive with an all-tape qualification barrier."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import json
from pathlib import Path
import sys
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _bootstrap import install
install()

from repeat_archive import CANONICAL, DATA, SSD, atomic_json, planning_lock, snapshot_sources
from repeat_cluster import detect
from repeat_qualification import frozen_qualification, immutable_copy, qualified, qualify_tape
from repeat_render import render_archive
from repeat_retain import HOLD, capture


def qualification_loop(raw, dest, tapes, implementation, stop):
    remaining = {tape for tape in tapes if not qualified(dest/tape)}
    while remaining and not stop.is_set():
        sealed = {p.parent.name for p in raw.glob('A15_T*/detected.json')}
        finished_discovery = set(tapes) <= sealed
        did_work = False
        for tape in sorted(remaining & sealed):
            if (not finished_discovery and
                    ((SSD/raw.name/tape).exists() or not (HOLD/raw.name/tape).is_dir())):
                continue
            if not finished_discovery:
                channels = json.loads((raw/tape/'sources.json').read_bytes())['channels']
                if not all((HOLD/raw.name/tape/r['name']).is_file() for r in channels):
                    continue
            qualify_tape(raw/tape,dest/tape,implementation)
            remaining.remove(tape)
            did_work = True
            if stop.is_set():
                return
        if not did_work:
            stop.wait(15)


def wait_lease(stack, path):
    while True:
        try:
            return stack.enter_context(planning_lock(path))
        except ValueError as exc:
            if 'Another planner is already using' not in str(exc):
                raise
            time.sleep(2)


def run(args):
    raw = DATA/args.run
    dest = DATA.parent/'crosstalk_qualification'/args.run
    tapes = sorted(p.name for p in CANONICAL.glob('A15_T*') if p.is_dir())
    if not tapes:
        raise ValueError('No stage-03 tapes')
    stop = threading.Event()
    with planning_lock(dest/'release'), ExitStack() as owner, ThreadPoolExecutor(max_workers=2) as pool:
        if not args.adopt_detection:
            owner.enter_context(planning_lock(raw/'coordinator'))
            snapshot_sources(raw/'implementation', immutable_copy)
        implementation = frozen_qualification(dest,raw)
        retention = pool.submit(capture,raw,dest,tapes,stop)
        checks = pool.submit(qualification_loop,raw,dest,tapes,implementation,stop)
        try:
            if not args.adopt_detection:
                # Reserve one Windows analysis slot for periodicity qualification.
                detect(raw,tapes,max(1,args.local-1) if args.local else 0,args.genus,args.tapes)
            checks.result()
            retention.result()
        finally:
            stop.set()
        if not all(qualified(dest/tape) for tape in tapes):
            raise ValueError('All tapes must qualify before rendering')
        if args.adopt_detection:
            wait_lease(owner,raw/'coordinator')
            # A handoff can briefly retain the first render slot after its
            # detection coordinator releases the larger archive lock.
            with ExitStack() as handoff:
                wait_lease(handoff,SSD/raw.name/tapes[0])
        print('All tape catalogs qualified. Starting stage-04 WAV publication.',flush=True)
        render_archive(dest,tapes,min(4,max(1,args.local)))
        finished = json.loads((dest/'complete.json').read_bytes())
        finished.update(qualification_root=str(dest),detection_root=str(raw))
        atomic_json(raw/'complete.json',finished)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run',required=True)
    parser.add_argument('--local',type=int,default=6)
    parser.add_argument('--genus',type=int,default=6)
    parser.add_argument('--tapes',type=int,default=3)
    parser.add_argument('--adopt-detection',action='store_true',
                        help='Consume an existing detection run whose direct renderer is held for handoff')
    args = parser.parse_args()
    if (not args.run.isascii() or not args.run.replace('-','').replace('_','').isalnum()
            or not 0 <= args.local <= 6 or not 0 <= args.genus <= 6 or not args.local+args.genus
            or not 1 <= args.tapes <= 3):
        parser.error('Use a simple run name, 0–6 workers per host, and 1–3 staged tapes')
    run(args)
