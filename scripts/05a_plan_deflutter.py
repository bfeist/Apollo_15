"""Stage 05a: plan flutter correction from original canonical timing evidence."""
import os
for name in ('OPENBLAS_NUM_THREADS', 'OMP_NUM_THREADS', 'MKL_NUM_THREADS'):
    os.environ.setdefault(name, '1')

import argparse
from pathlib import Path
from flutter_planning import plan_tape
from flutter_plan_store import DEFAULT_PLANNING_ROOT, planning_directory, planning_lock
from flutter_policy import DEFAULT_SURVEY_ROOT, require_rollout


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, required=True, help='Canonical tape folder, one timestamped WAV per channel')
    parser.add_argument('--output', type=Path, help='Override the persistent per-tape destination')
    parser.add_argument('--planning-root', type=Path, default=DEFAULT_PLANNING_ROOT)
    parser.add_argument('--carrier-survey-root', type=Path, default=DEFAULT_SURVEY_ROOT)
    parser.add_argument('--rollout-approval', type=Path, help='Frozen pilot/listening acceptance; required for full-tape planning')
    parser.add_argument('--region-seconds', type=float, default=60)
    parser.add_argument('--context-seconds', type=float, default=4)
    parser.add_argument('--start', type=float, default=0)
    parser.add_argument('--duration', type=float, help='Optional bounded audit; omitted means the whole tape')
    parser.add_argument('--candidates-per-channel', type=int, default=4)
    parser.add_argument('--reference-limit', type=int, default=4)
    parser.add_argument('--max-regions', type=int, help='Checkpoint after this many new regions; remaining regions stay pending')
    parser.add_argument('--resume', action='store_true')
    parser.add_argument('--workers', type=int, default=0, help='Concurrent regions; 0 chooses up to eight CPU workers, 1 is serial')
    args = parser.parse_args()
    try:
        if args.duration is None:
            require_rollout(args.rollout_approval)
        output = args.output or planning_directory(args.input.name, args.planning_root, args.start, args.duration)
        if args.input.resolve() == output.resolve() or args.input.resolve() in output.resolve().parents or output.resolve() in args.input.resolve().parents:
            raise ValueError('Input and output must be non-overlapping')
        with planning_lock(output):
            plan_tape(args.input, output, region_seconds=args.region_seconds,
                      context_seconds=args.context_seconds, start=args.start, duration=args.duration,
                      candidates_per_channel=args.candidates_per_channel, reference_limit=args.reference_limit,
                      max_regions=args.max_regions, resume=args.resume, workers=args.workers, reuse=True,
                      carrier_survey=args.carrier_survey_root/args.input.name)
    except (ValueError, OSError, RuntimeError, KeyError) as exc:
        parser.error(str(exc))


if __name__ == '__main__':
    main()
