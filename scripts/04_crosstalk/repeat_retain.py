"""Retain temporary SSD inputs by hard link while a separate qualification pass consumes sealed tapes."""
import json
import os
from pathlib import Path
import shutil
import time

from repeat_archive import SSD


HOLD = Path('F:/Apollo15_crosstalk_qualification')


def capture(base, qualification, tapes, stop=None):
    hold = HOLD/base.name
    hold.mkdir(parents=True, exist_ok=True)
    while stop is None or not stop.is_set():
        sealed = {p.parent.name for p in base.glob('A15_T*/detected.json')}
        if set(tapes) <= sealed:
            return
        occupied = set(p.name for p in hold.glob('A15_T*') if p.is_dir())
        for scratch in (SSD/base.name).glob('A15_T*'):
            tape = scratch.name
            if tape in sealed or (qualification/tape/'qualified.json').exists():
                continue
            if tape not in occupied and (len(occupied) >= 5 or shutil.disk_usage(hold).free < 200_000_000_000):
                continue
            try:
                record = json.loads((scratch/'sources.json').read_bytes())
                dest = hold/tape
                dest.mkdir(exist_ok=True)
                occupied.add(tape)
                for entry in record['channels']:
                    link = dest/entry['name']
                    if not link.exists():
                        os.link(scratch/entry['name'], link)
            except FileNotFoundError:
                # A tape can publish and reclaim its stage while we observe it.
                continue
        if stop is None:
            time.sleep(15)
        elif stop.wait(15):
            return


if __name__ == '__main__':
    import argparse
    from repeat_archive import CANONICAL, DATA
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', required=True)
    args = parser.parse_args()
    if not args.run.isascii() or not args.run.replace('-', '').replace('_','').isalnum():
        parser.error('Use a simple run name')
    capture(DATA/args.run, CANONICAL.parent/'crosstalk_qualification'/args.run,
            [p.name for p in CANONICAL.glob('A15_T*') if p.is_dir()])
