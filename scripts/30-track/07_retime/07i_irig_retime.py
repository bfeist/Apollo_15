"""Stage 07i: retiming plans from IRIG-B time code on the tape (hybrid with 07a SG evidence).

Tapes whose time-code track survives (true CH01; delivered CH30 on T510075-078 and T510085)
are timed from the time code; 07a stays available for them, and 07b prefers the 07i plan.
Every other tape is a "non-time-code" tape and is timed by 07a alone. Method and artifacts:
``irig_timing.py``.

    uv run python scripts/run.py 07i_irig_retime.py decode --all-tapes        # decode CH28-30 of every 05b tape
    uv run python scripts/run.py 07i_irig_retime.py fit --all-tapes           # plans where time code exists
    uv run python scripts/run.py 07i_irig_retime.py run --all-tapes           # both
    uv run python scripts/run.py 07i_irig_retime.py status --all-tapes

``fit`` uses the newest complete 07a anchors edition of a tape when one exists (hybrid; build it
with ``07a_sg_retime.py anchors --tape TAPE``, plus ``refine``/``quindar`` for audio and rate
evidence). Without it the plan comes from the time code alone and is flagged. Then apply:

    uv run python scripts/run.py 07b_retime.py --tape TAPE --all-channels        # prefers the 07i plan
"""
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))
from _bootstrap import install as _install
_install()

import argparse
import json
from pathlib import Path

import irig_timing as it
from sg_inputs import DEFLUTTERED_ROOT, RETIMING_ROOT, read_json


def tapes_of(args):
    if args.all_tapes:
        return sorted(p.name for p in Path(DEFLUTTERED_ROOT).glob('A15_T*') if p.is_dir())
    if not args.tape:
        raise SystemExit('Give --tape or --all-tapes')
    return args.tape


def status(tapes, root):
    print(f'{"tape":<12} {"time code":<22} {"07i plan":<28} {"07a anchors":<26}')
    for tape in tapes:
        track, recs = it.timecode_track(tape, Path(root) / 'irig')
        tc = (f'CH{track["channel"]:02d} {track["strict"]:,} frames' if track else
              ('none on ' + ','.join(f'{r["channel"]:02d}' for r in recs) if recs else 'not decoded'))
        plan = it.irig_plan(tape, root=root)
        if plan:
            p = read_json(plan)
            plan_s = f'{p["fit_edition"][:20]} {p["support"]["timecode_share"]:.0%} tc'
        else:
            plan_s = '-'
        anchors = it.latest_anchors(tape, root)
        print(f'{tape:<12} {tc:<22} {plan_s:<28} {anchors.name[:24] if anchors else "-":<26}')


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('step', choices=('decode', 'fit', 'run', 'status'))
    p.add_argument('--tape', action='append', default=[])
    p.add_argument('--all-tapes', action='store_true')
    p.add_argument('--channels', default=','.join(map(str, it.DECODE_CHANNELS)), help='05b channels to decode')
    p.add_argument('--workers', type=int, default=6)
    p.add_argument('--no-sg', action='store_true', help='fit from the time code alone even when 07a anchors exist')
    p.add_argument('--root', type=Path, default=RETIMING_ROOT)
    args = p.parse_args(argv)
    tapes = tapes_of(args)
    if args.step == 'status':
        status(tapes, args.root)
        return 0
    if args.step in ('decode', 'run'):
        it.decode(tapes, tuple(int(c) for c in args.channels.split(',')), args.workers, Path(args.root) / 'irig')
    if args.step in ('fit', 'run'):
        failures = []
        for tape in tapes:
            try:
                plan = it.fit_tape(tape, args.root, use_sg=not args.no_sg)
                if plan:
                    print(f'{tape}: plan {plan}', flush=True)
            except (ValueError, OSError, RuntimeError, KeyError) as exc:
                failures.append(f'{tape}: {exc}')
                print(f'BLOCKED {failures[-1]}', flush=True)
        if failures:
            return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
