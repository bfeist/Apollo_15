"""Stage 05a: plan flutter correction from original canonical timing evidence."""
import os
for name in ('OPENBLAS_NUM_THREADS', 'OMP_NUM_THREADS', 'MKL_NUM_THREADS'):
    os.environ.setdefault(name, '1')

import argparse
from pathlib import Path
from continuous_planning import plan_tape
from flutter_plan_store import DEFAULT_PLANNING_ROOT, planning_directory, planning_lock


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, required=True, help='Canonical tape folder, one timestamped WAV per channel')
    parser.add_argument('--output', type=Path, help='Override the persistent per-tape destination')
    parser.add_argument('--planning-root', type=Path, default=DEFAULT_PLANNING_ROOT)
    parser.add_argument('--monitor-seconds', type=float, default=60, help='Quality-check cadence; does not end a reference lifetime')
    parser.add_argument('--chunk-seconds', type=float, default=7, help='Bounded reference I/O size; does not change the estimator')
    parser.add_argument('--context-seconds', type=float, default=4)
    parser.add_argument('--start', type=float, default=0)
    parser.add_argument('--duration', type=float, help='Optional bounded audit; omitted means the whole tape')
    parser.add_argument('--candidates-per-channel', type=int, default=8)
    parser.add_argument('--reference-limit', type=int, default=8)
    parser.add_argument('--max-windows', type=int, help='Checkpoint after this many new monitoring windows')
    parser.add_argument('--resume', action='store_true')
    args = parser.parse_args()
    try:
        output = args.output or planning_directory(args.input.name, args.planning_root, args.start, args.duration)
        if args.input.resolve() == output.resolve() or args.input.resolve() in output.resolve().parents or output.resolve() in args.input.resolve().parents:
            raise ValueError('Input and output must be non-overlapping')
        with planning_lock(output):
            plan = plan_tape(args.input, output, start=args.start, duration=args.duration,
                monitor_seconds=args.monitor_seconds, chunk_seconds=args.chunk_seconds,
                context_seconds=args.context_seconds, candidates_per_channel=args.candidates_per_channel,
                reference_limit=args.reference_limit, max_windows=args.max_windows, resume=args.resume, reuse=True)
            outcome = plan.get('optimization_outcome', {})
            print(f"{args.input.name}: {outcome.get('status', 'incomplete')}; "
                  f"supported correction coverage={outcome.get('correction_coverage', 0.):.2%}; "
                  f"restoration-goal channel-seconds={outcome.get('restoration_goal_seconds', 0.):.3f}; "
                  f"supported-improvement channel-seconds={outcome.get('supported_improvement_seconds', 0.):.3f}; "
                  'planning evidence only, no production acceptance', flush=True)
    except (ValueError, OSError, RuntimeError, KeyError) as exc:
        parser.error(str(exc))


if __name__ == '__main__':
    main()
