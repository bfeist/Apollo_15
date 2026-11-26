"""Stage 07b: apply one frozen tape-speed plan (07a SG fit, or a Quindar plan) to every defluttered channel.

Inputs: the 05b defluttered channels, the stage-06 transcripts and a 07a plan (built from the
retimed SG reels). Outputs, per channel under the retimed root (D:/Apollo_15_30-Track/retimed):
``retimed.wav`` (32-bit float, the 05b sample rate, constant peak gain) with ``retime.json``,
``timing_map.npz`` and ``curve.json``; and ``timed_transcript.tsv``/``.json``, the 06 transcript
on the same output clock (timed_transcript.py).
"""

# Resolve shared modules from the stage folders.
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))
from _bootstrap import install as _install
_install()

import argparse
import json
from pathlib import Path

from flutter_plan_store import tape_directory
from shared_tape import DEFAULT_DEFLUTTERED_ROOT
from quindar_retime import DEFAULT_RETIMED_ROOT, retime
from quindar_survey import DEFAULT_ANALYSIS_ROOT
from quindar_tape import build_shared_plan, load_shared_plan
from sg_inputs import RETIMING_ROOT
from timed_transcript import write_timed_transcript


def sg_plan(tape, fit=None, root=RETIMING_ROOT):
    """The 07a plan of ``tape``: from the named fit edition, else the newest final edition with that tape."""
    if fit is not None:
        plan = Path(fit)/'tapes'/tape/'plan.json'
        if not plan.is_file():
            raise ValueError(f'07a fit edition {fit} has no plan for {tape}')
        return plan
    fits = [f for f in (Path(root)/'sg_fit').glob('fit-*')  # provisional editions are named provisional-fit-*
            if (f/'tapes'/tape/'plan.json').is_file() and (f/'manifest.json').is_file()
            and json.loads((f/'manifest.json').read_text(encoding='utf-8')).get('status') == 'complete']
    return max(fits, key=lambda f: (f/'tapes'/tape/'plan.json').stat().st_mtime_ns)/'tapes'/tape/'plan.json' if fits else None


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--input', type=Path, help='Defluttered mono WAV or its containing folder')
    p.add_argument('--output', type=Path, help='New output folder; defaults to the retimed root in whole-channel mode')
    p.add_argument('--tape')
    p.add_argument('--all-tapes', action='store_true', help='With --plan-only, freeze one plan per fully surveyed defluttered tape')
    p.add_argument('--channel', type=int)
    p.add_argument('--all-channels', action='store_true', help='Apply the same saved tape plan to all completed defluttered channels')
    p.add_argument('--plan-only', action='store_true', help='Freeze the tape plan after exhaustive discovery and post-deflutter measurement')
    p.add_argument('--quindar-analysis-root', type=Path, default=DEFAULT_ANALYSIS_ROOT)
    p.add_argument('--quindar-plan', type=Path, help='Frozen shared tape plan override')
    p.add_argument('--sg-fit', type=Path, help='07a fit edition (sg_fit/<edition>) whose plan to apply; default: '
                   'the newest final edition with this tape, else the Quindar plan')
    p.add_argument('--clock-anchors', type=Path, help='Authenticated anchors carried through the actual 05b map; for one-tape --plan-only')
    p.add_argument('--experimental-single-channel', action='store_true', help='Explicitly permit the original per-file assessment workflow')
    p.add_argument('--defluttered-root', type=Path, default=DEFAULT_DEFLUTTERED_ROOT)
    p.add_argument('--retimed-root', type=Path, default=DEFAULT_RETIMED_ROOT)
    p.add_argument('--candidates', type=Path, help='Reuse a complete candidate inventory for the exact input file')
    p.add_argument('--smoothing-seconds', type=float, help='Gaussian kernel sigma >=300 seconds; default selects by block validation')
    p.add_argument('--target-dbfs', type=float, default=-3.)
    p.add_argument('--max-boost-db', type=float, default=12.)
    args = p.parse_args()
    try:
        if args.clock_anchors and (not args.plan_only or args.all_tapes):
            p.error('--clock-anchors requires one-tape --plan-only')
        plan = args.quindar_plan
        if args.all_tapes:
            if not args.plan_only or args.tape or args.channel or args.input or args.all_channels or plan:
                p.error('--all-tapes requires --plan-only and no per-tape overrides')
            folders=sorted(args.quindar_analysis_root.glob('A15_T*'))
            if not folders:p.error('No completed post-deflutter surveys found')
            failures=[]
            for folder in folders:
                try:
                    record=build_shared_plan(folder,args.smoothing_seconds)
                    print(f"{record['tape']}: frozen shared plan; {record['unique_calibration_events']} unique calibration events",flush=True)
                except (ValueError,OSError,RuntimeError,KeyError,TypeError) as error:
                    failures.append(f'{folder.name}: {error}')
            if failures:raise ValueError('; '.join(failures))
            return
        if args.plan_only:
            if not args.tape or args.input or args.channel or args.all_channels or plan:
                p.error('--plan-only requires --tape; omit input/channel/plan overrides')
            record = build_shared_plan(tape_directory(args.tape, args.quindar_analysis_root), args.smoothing_seconds, args.clock_anchors)
            print(f"Frozen tape plan: {record['tape']}; {record['unique_calibration_events']} unique anchors; {record['reference_count']} post-deflutter references")
            return
        if args.tape and not args.experimental_single_channel and plan is None:
            plan = sg_plan(args.tape, args.sg_fit)
            if plan is not None:
                print(f'Applying the 07a SG plan {plan}', flush=True)
            plan = plan or tape_directory(args.tape, args.quindar_analysis_root)/'shared_plan'/'plan.json'
        if not args.experimental_single_channel:
            if plan is None or not plan.exists():
                p.error('A frozen tape Quindar plan is required. Complete the survey, then run --tape TAPE --plan-only')
            saved = load_shared_plan(plan)
            if args.tape and saved['tape'] != args.tape:
                p.error('Shared plan belongs to another tape')
        if args.all_channels:
            if not args.tape or args.channel or args.input or args.output or args.experimental_single_channel or args.candidates or args.smoothing_seconds is not None:
                p.error('--all-channels requires --tape and the shared plan; omit input/output/channel')
            sources = sorted(tape_directory(args.tape, args.defluttered_root).glob('CH*/deflutter.wav'))
            if not sources:
                p.error('No defluttered channels available for this tape')
            failures = []
            for source in sources:
                destination = tape_directory(args.tape, args.retimed_root)/source.parent.name
                try:
                    record = retime(source, destination, target_dbfs=args.target_dbfs, max_boost_db=args.max_boost_db, shared_plan=plan)
                    write_timed_transcript(destination, Path(record['source']['path']))
                except (ValueError, OSError, RuntimeError, KeyError, TypeError) as error:
                    failures.append(f'{source.parent.name}: {error}')
                    print(f'BLOCKED {failures[-1]}', flush=True)
            if failures:
                raise ValueError('; '.join(failures))
            return
        if args.channel is not None:
            if not args.tape or not 1 <= args.channel <= 30 or args.input:
                p.error('Whole-channel mode requires --tape and --channel 1-30; omit --input')
            source = tape_directory(args.tape, args.defluttered_root)/f'CH{args.channel:02d}'/'deflutter.wav'
            destination = args.output or tape_directory(args.tape, args.retimed_root)/f'CH{args.channel:02d}'
        else:
            if args.input is None or args.output is None:
                p.error('File mode requires --input and --output')
            source, destination = args.input, args.output
        record = retime(source, destination, args.candidates, args.smoothing_seconds, args.target_dbfs, args.max_boost_db, shared_plan=plan)
        write_timed_transcript(destination, Path(record['source']['path']))
    except (ValueError, OSError, RuntimeError, KeyError, TypeError) as error:
        p.error(str(error))


if __name__ == '__main__':
    main()
