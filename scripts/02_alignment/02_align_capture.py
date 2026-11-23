"""Stage 02. Measure the registration of original tracks without retiming their audio."""
import argparse
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _bootstrap import install
install()

from capture_highband import check
from flutter_alignment import ORIGINALS, ROOT


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--originals', type=Path, default=ORIGINALS)
    p.add_argument('--alignment-root', type=Path, default=ROOT)
    selection = p.add_mutually_exclusive_group(required=True)
    selection.add_argument('--tape', action='append')
    selection.add_argument('--all-tapes', action='store_true')
    p.add_argument('--exclude-tape', action='append', default=[])
    args = p.parse_args()
    folders = sorted(args.originals.glob('A15_T*')) if args.all_tapes else [args.originals/t for t in args.tape]
    if not folders:
        p.error('No original tapes found')
    held = []
    for folder in folders:
        if folder.name in args.exclude_tape:
            continue
        path, report = check(folder,args.alignment_root)
        print(f"{folder.name} {report['action']} {path}",flush=True)
        if report['action']=='review':
            held.append((folder.name,report['unresolved']))
    if held:
        print(f'Unresolved channels by tape: {held}',flush=True)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
