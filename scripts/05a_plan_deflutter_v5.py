"""Version 5: survey every recorded channel, then plan and assess supported correction."""
import os
for name in ('OPENBLAS_NUM_THREADS', 'OMP_NUM_THREADS', 'MKL_NUM_THREADS'):
    os.environ.setdefault(name, '1')

import argparse
import json
from pathlib import Path

from flutter_plan_store import planning_directory, planning_lock
from v5_resume import plan_tape
from v5_safe_assessment import assess_plan, assessment_implementation_digest
from flutter_policy import digest

DEFAULT_ROOT = Path('D:/Apollo_15_30-Track/deflutter_planning_v5_allchannel')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--output', type=Path, help='Fresh v5 bundle destination; saved map will be under plan/')
    parser.add_argument('--planning-root', type=Path, default=DEFAULT_ROOT)
    parser.add_argument('--denoised-root', type=Path, default=Path('D:/Apollo_15_30-Track/denoised'),
        help='Existing stage-03 root; candidates are discovered in both versions and tracked on originals')
    parser.add_argument('--original-only', action='store_true', help='Disable supplementary denoised discovery')
    parser.add_argument('--start', type=float, default=0.)
    parser.add_argument('--duration', type=float)
    parser.add_argument('--monitor-seconds', type=float, default=60.)
    parser.add_argument('--context-seconds', type=float, default=4.)
    parser.add_argument('--chunk-seconds', type=float, default=7.)
    parser.add_argument('--candidates-per-channel', type=int, default=8)
    parser.add_argument('--reference-limit', type=int, default=8)
    parser.add_argument('--max-windows', type=int, help='Limit planning windows after the requested survey finishes')
    parser.add_argument('--resume', action='store_true', help='Resume compatible survey or planner checkpoints and reuse completed assessments')
    args = parser.parse_args()
    output = args.output or planning_directory(args.input.name, args.planning_root, args.start, args.duration)
    try:
        with planning_lock(output):
            plan = plan_tape(args.input, output, start=args.start, duration=args.duration,
                monitor_seconds=args.monitor_seconds, context_seconds=args.context_seconds,
                chunk_seconds=args.chunk_seconds, candidates_per_channel=args.candidates_per_channel,
                reference_limit=args.reference_limit, max_windows=args.max_windows, resume=args.resume,
                denoised_root=None if args.original_only else args.denoised_root/args.input.name)
            if plan['complete']:
                assessment = output/'joined_assessment.json'
                report = json.loads(assessment.read_text(encoding='utf-8')) if args.resume and assessment.exists() else None
                if (report is not None and report.get('assessment_complete') is True
                    and report.get('plan_sha256') == digest(output/'plan'/'plan.json')
                    and report.get('assessment_implementation_sha256') == assessment_implementation_digest()):
                    print(f'{args.input.name}: reusing completed assessment', flush=True)
                else:
                    report = assess_plan(output/'plan', assessment)
                if report.get('invalid_map_channels'):
                    channels = ', '.join(f'CH{channel:02d}' for channel in report['invalid_map_channels'])
                    print(f'{args.input.name}: assessment completed with rejected maps on {channels}; '
                          'leave those channels uncorrected. See joined_assessment.json.', flush=True)
                print(f"Proposed correction: {report['proposed_correction_channel_seconds']:.3f} channel-seconds; "
                    f"spans with passing reserved checks: {report['spans_with_reserved_improvement_channel_seconds']:.3f}. "
                    'Event measurements do not establish every-second restoration. Listening not performed.')
            else:
                print('Compatible v5 checkpoint saved; repeat with --resume. Final map evaluation is pending.')
            print(f'Saved v5 bundle: {output.resolve()}')
    except (ValueError, OSError, RuntimeError, KeyError) as exc:
        parser.error(str(exc))


if __name__ == '__main__':
    main()
