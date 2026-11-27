"""Stage 07b: apply one frozen tape-speed plan (07i time code, 07a SG fit, or a Quindar plan) to every defluttered channel.

Inputs: the 05b defluttered channels, the stage-06 transcripts and a plan: by default the newest
07i time-code plan of the tape (irig_timing.py; time-code tapes only), else the newest final
07a plan (built from the retimed SG reels), else the Quindar plan. The plan defines GET for the
earlier head row (real time). Each channel of the later row is moved earlier by its measured
stagger through the same tape map (head_rows.py, ``head_rows.json`` per tape; docs/23).
``--scratch`` stages inputs and outputs on a local disk so the archive disk sees one sequential
copy at a time (staged_io.py). Outputs, per channel under the retimed root (D:/Apollo_15_30-Track/retimed):
``retimed.wav`` (32-bit float, the 05b sample rate, unity gain; stage 08 sets the delivery gain) with ``retime.json``,
``timing_map.npz`` and ``curve.json``; and ``timed_transcript.tsv``/``.json``, the 06 transcript
on the same output clock (timed_transcript.py), with ``transcript_times.tsv``/``.json``, the mission
transcripts' GETs checked against the words heard (transcript_times.py; one combined edition per
finished tape under stage_evidence/retiming/transcript_times/<tape>/).
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
from quindar_retime import retime
from quindar_survey import DEFAULT_ANALYSIS_ROOT
from quindar_tape import build_shared_plan, load_shared_plan
from sg_inputs import DEFLUTTERED_ROOT, RETIMING_ROOT, TAPE_DATA
from irig_timing import irig_plan, timecode_track
from timed_transcript import write_timed_transcript
import head_rows
import retime_cluster
import staged_io
import transcript_times

DEFAULT_DEFLUTTERED_ROOT = DEFLUTTERED_ROOT
DEFAULT_RETIMED_ROOT = TAPE_DATA / 'retimed'


def sg_plan(tape, fit=None, root=RETIMING_ROOT):
    """The 07a plan of ``tape``: from the named fit edition, else the newest final edition with that tape."""
    if fit is not None:
        plan = Path(fit)/'tapes'/tape/'plan.json'
        if not plan.is_file():
            raise ValueError(f'07a fit edition {fit} has no plan for {tape}')
        return plan
    import sg_stage
    fits = [f for f in (Path(root)/'sg_fit').glob('fit-*')  # provisional editions are named provisional-fit-*
            if (f/'tapes'/tape/'plan.json').is_file() and (f/'manifest.json').is_file()
            and json.loads((f/'manifest.json').read_text(encoding='utf-8')).get('status') == 'complete'
            and json.loads((f/'manifest.json').read_text(encoding='utf-8')).get('version') == sg_stage.VERSION]
    return max(fits, key=lambda f: (f/'tapes'/tape/'plan.json').stat().st_mtime_ns)/'tapes'/tape/'plan.json' if fits else None


def tape_row_plan(tape, saved, sources, retimed_root, log=None):
    """Head-row plan of the tape (head_rows.py), frozen beside its retimed channels.

    Computed once per tape from every delivered channel of the tape; an existing record that
    differs means the evidence changed: write to a new retimed root instead of mixing."""
    all_sources = sorted(Path(sources[0]).parents[1].glob('CH*/deflutter.wav'))
    channels = [int(s.parent.name[2:]) for s in all_sources]
    track, _ = timecode_track(tape)
    rows = head_rows.row_plan(tape, channels, float(saved['input_duration_seconds']),
                              timecode_channel=track['channel'] if track else None)
    if rows['anchors'] is None or rows['late_row_gap_s'] is None:
        raise ValueError(f'{tape}: no current 07a anchors edition with two head rows; the later-row channels cannot be '
                         'placed on real time (run 07a anchors, or pass --no-row-shift for a comparison render)')
    path = tape_directory(tape, retimed_root)/'head_rows.json'
    text = json.dumps(rows, indent=1, sort_keys=True)
    if path.is_file():
        if path.read_text(encoding='utf-8') != text:
            raise ValueError(f'{path} records another head-row plan; use a new --retimed-root')
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name+'.partial')
        tmp.write_text(text, encoding='utf-8')
        tmp.replace(path)
    log = log or (lambda m: print(m, flush=True))
    flagged = {d: c['flags'] for d, c in rows['channels'].items() if c['flags']}
    log(f"{tape}: head rows from {rows['anchors'] or 'no 07a anchors edition'}; later row "
          f"{rows['late_row_gap_s'] if rows['late_row_gap_s'] is not None else 'unmeasured'} s; "
          + ', '.join(f"CH{int(d):02d} {c['rows']}" for d, c in rows['channels'].items()))
    for d, f in flagged.items():
        log(f'  CH{int(d):02d}: ' + '; '.join(f))
    return rows


def choose_plan(tape, args, log=print):
    """The plan 07b applies to ``tape``: 07i v2, else final 07a v3, else (only if allowed) Quindar."""
    plan = None
    if not args.no_irig and args.sg_fit is None:
        plan = irig_plan(tape, args.irig_fit)
        if plan is not None:
            log(f'{tape}: applying the 07i time-code plan {plan}')
    elif args.irig_fit is not None:
        raise ValueError('--irig-fit cannot be combined with --no-irig or --sg-fit')
    if plan is None:
        plan = sg_plan(tape, args.sg_fit)
        if plan is not None:
            log(f'{tape}: applying the 07a SG plan {plan}')
    if plan is None:
        if not args.allow_quindar_plan:
            raise ValueError(f'{tape}: no 07i time-code plan and no final 07a SG plan; refusing the Quindar-only '
                             'fallback (pass --allow-quindar-plan to use it)')
        plan = tape_directory(tape, args.quindar_analysis_root)/'shared_plan'/'plan.json'
    if not plan.exists():
        raise ValueError(f'{tape}: plan {plan} does not exist')
    return plan


def prepare_tape(tape, args, log=print):
    """(plan, jobs) of one tape: its plan and every channel [(source, destination, knots)]."""
    plan = choose_plan(tape, args, log)
    saved = load_shared_plan(plan)
    if saved['tape'] != tape:
        raise ValueError(f'{tape}: shared plan belongs to {saved["tape"]}')
    sources = sorted(tape_directory(tape, args.defluttered_root).glob('CH*/deflutter.wav'))
    if not sources:
        raise ValueError(f'{tape}: no defluttered channels')
    rows = None if args.no_row_shift else tape_row_plan(tape, saved, sources, args.retimed_root, log)
    jobs = [(source, tape_directory(tape, args.retimed_root)/source.parent.name,
             None if rows is None else rows['channels'][str(int(source.parent.name[2:]))]['knots'])
            for source in sources]
    return plan, jobs


def render_tape(tape, args, log=print):
    """Every channel of one tape with its plan and head-row shift. Returns failure texts."""
    plan, jobs = prepare_tape(tape, args, log)
    if args.scratch:
        failures = staged_io.run_staged(jobs, plan, args.scratch / tape, args.workers, lock_root=TAPE_DATA, log=log)
    else:
        failures = []
        for source, destination, knots in jobs:
            try:
                record = retime(source, destination, shared_plan=plan, row_shift=knots)
                write_timed_transcript(destination, Path(record['source']['path']))
            except (ValueError, OSError, RuntimeError, KeyError, TypeError) as error:
                failures.append(f'{source.parent.name}: {error}')
                log(f'BLOCKED {failures[-1]}')
    if not failures:
        transcript_times.tape_check_safe(tape, args.retimed_root, progress=log)
    return failures


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--input', type=Path, help='Defluttered mono WAV or its containing folder')
    p.add_argument('--output', type=Path, help='New output folder; defaults to the retimed root in whole-channel mode')
    p.add_argument('--tape')
    p.add_argument('--all-tapes', action='store_true',
                   help='With --all-channels: render every tape, shared out between this host and Genus through a tape '
                        'queue on the archive disk (retime_cluster.py). With --plan-only: freeze one Quindar plan per '
                        'surveyed tape')
    p.add_argument('--channel', type=int)
    p.add_argument('--all-channels', action='store_true', help='Apply the same saved tape plan to all completed defluttered channels')
    p.add_argument('--plan-only', action='store_true', help='Freeze the tape plan after exhaustive discovery and post-deflutter measurement')
    p.add_argument('--quindar-analysis-root', type=Path, default=DEFAULT_ANALYSIS_ROOT)
    p.add_argument('--quindar-plan', type=Path, help='Frozen shared tape plan override')
    p.add_argument('--sg-fit', type=Path, help='07a fit edition (sg_fit/<edition>) whose plan to apply; default: '
                   'the newest final edition with this tape, else the Quindar plan')
    p.add_argument('--irig-fit', type=Path, help='07i edition folder (irig/<tape>/fit-<digest>) to apply')
    p.add_argument('--no-irig', action='store_true', help='ignore 07i time-code plans (apply 07a or Quindar)')
    p.add_argument('--clock-anchors', type=Path, help='Authenticated anchors carried through the actual 05b map; for one-tape --plan-only')
    p.add_argument('--experimental-single-channel', action='store_true', help='Explicitly permit the original per-file assessment workflow')
    p.add_argument('--defluttered-root', type=Path, default=DEFAULT_DEFLUTTERED_ROOT)
    p.add_argument('--retimed-root', type=Path, default=DEFAULT_RETIMED_ROOT)
    p.add_argument('--candidates', type=Path, help='Reuse a complete candidate inventory for the exact input file')
    p.add_argument('--smoothing-seconds', type=float, help='Gaussian kernel sigma >=300 seconds; default selects by block validation')
    p.add_argument('--no-row-shift', action='store_true',
                   help='comparison only: keep the head-row stagger (later-row channels stay ~1.38 s late; docs/23)')
    p.add_argument('--scratch', type=Path,
                   help='local work disk (F: on Proprius, the SSD on Genus): inputs are copied in and outputs out one '
                        'file at a time, so the archive disk only ever sees one sequential stream')
    p.add_argument('--workers', type=int, default=8, help='with --scratch: channels rendered in parallel on this host')
    p.add_argument('--allow-quindar-plan', action='store_true',
                   help='use the Quindar-only plan when the tape has neither a 07i nor a final 07a plan')
    retime_cluster.add_arguments(p)
    args = p.parse_args()
    if args.stop:
        raise SystemExit(retime_cluster.request_stop(args))
    try:
        if args.clock_anchors and (not args.plan_only or args.all_tapes):
            p.error('--clock-anchors requires one-tape --plan-only')
        plan = args.quindar_plan
        if args.all_tapes and args.all_channels:
            if args.plan_only or args.tape or args.channel or args.input or args.output or plan:
                p.error('--all-tapes --all-channels renders every tape; omit --tape/--channel/--input/--plan-only')
            raise SystemExit(retime_cluster.run(args, prepare_tape, TAPE_DATA))
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
        if args.all_channels and not args.experimental_single_channel and plan is None:
            if not args.tape or args.channel or args.input or args.output or args.candidates or args.smoothing_seconds is not None:
                p.error('--all-channels requires --tape and the shared plan; omit input/output/channel')
            failures = render_tape(args.tape, args, log=lambda m: print(m, flush=True))
            if failures:
                raise ValueError('; '.join(failures))
            return
        if args.tape and not args.experimental_single_channel and plan is None:
            plan = choose_plan(args.tape, args, log=lambda m: print(m, flush=True))
        if not args.experimental_single_channel:
            if plan is None or not plan.exists():
                p.error('A frozen tape Quindar plan is required. Complete the survey, then run --tape TAPE --plan-only')
            saved = load_shared_plan(plan)
            if args.tape and saved['tape'] != args.tape:
                p.error('Shared plan belongs to another tape')
        if args.all_channels:
            p.error('--all-channels with a --quindar-plan override or --experimental-single-channel is not supported')
        if args.channel is not None:
            if not args.tape or not 1 <= args.channel <= 30 or args.input:
                p.error('Whole-channel mode requires --tape and --channel 1-30; omit --input')
            source = tape_directory(args.tape, args.defluttered_root)/f'CH{args.channel:02d}'/'deflutter.wav'
            destination = args.output or tape_directory(args.tape, args.retimed_root)/f'CH{args.channel:02d}'
        else:
            if args.input is None or args.output is None:
                p.error('File mode requires --input and --output')
            source, destination = args.input, args.output
        knots = None
        if plan is not None and args.channel is not None and not args.no_row_shift:
            rows = tape_row_plan(args.tape, saved, [source], args.retimed_root)
            knots = rows['channels'][str(args.channel)]['knots']
        record = retime(source, destination, args.candidates, args.smoothing_seconds, shared_plan=plan, row_shift=knots)
        write_timed_transcript(destination, Path(record['source']['path']))
    except (ValueError, OSError, RuntimeError, KeyError, TypeError) as error:
        p.error(str(error))


if __name__ == '__main__':
    main()
