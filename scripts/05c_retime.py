"""Stage 05c: retime defluttered audio using a smoothed Quindar anchor curve."""
import argparse
from pathlib import Path

from flutter_plan_store import DEFAULT_DEFLUTTERED_ROOT, tape_directory
from quindar_retime import DEFAULT_RETIMED_ROOT, retime
from quindar_survey import DEFAULT_ANALYSIS_ROOT
from quindar_tape import build_shared_plan, load_shared_plan


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
        if args.tape and not args.experimental_single_channel:
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
                    retime(source, destination, target_dbfs=args.target_dbfs, max_boost_db=args.max_boost_db, shared_plan=plan)
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
        retime(source, destination, args.candidates, args.smoothing_seconds, args.target_dbfs, args.max_boost_db, shared_plan=plan)
    except (ValueError, OSError, RuntimeError, KeyError, TypeError) as error:
        p.error(str(error))


if __name__ == '__main__':
    main()
