"""Stage 05b: apply the same saved tape map to stage-04 channels derived from v2."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _bootstrap import install
install()

import argparse
import json
from denoise_archive import CANONICAL
from crosstalk_stage import DEFAULT_CROSSTALK_ROOT
from shared_render import render_shared_channel
from shared_tape import DEFAULT_PLANNING_ROOT, DEFAULT_DEFLUTTERED_ROOT, index_path


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--tape', required=True)
    p.add_argument('--channel', type=int, required=True)
    p.add_argument('--output', type=Path)
    p.add_argument('--flutter-plan', type=Path, help='Shared stage-05a plan, never a per-channel plan')
    p.add_argument('--planning-root', type=Path, default=DEFAULT_PLANNING_ROOT)
    sources = p.add_mutually_exclusive_group()
    sources.add_argument('--crosstalk-input', type=Path, help='Completed stage-04 CHnn folder')
    sources.add_argument('--denoised-input', type=Path, help='Explicit bypass of stage 04 for a denoised-only comparison')
    sources.add_argument('--skip-crosstalk', action='store_true', help='Explicit comparison using --denoised-root')
    p.add_argument('--denoised-root', type=Path, default=CANONICAL)
    p.add_argument('--crosstalk-root', type=Path, default=DEFAULT_CROSSTALK_ROOT)
    p.add_argument('--dry-run', action='store_true')
    args = p.parse_args(argv)
    try:
        if Path(args.tape).name != args.tape or args.tape in {'.', '..'}:
            raise ValueError('Tape must be a simple folder name')
        plan = args.flutter_plan or args.planning_root/args.tape/'plan.json'
        if json.loads(index_path(plan).read_text(encoding='utf-8'))['inventory']['tape'] != args.tape:
            raise ValueError('Requested tape and shared plan disagree')
        root = args.denoised_root if args.skip_crosstalk else args.crosstalk_root
        source = args.denoised_input or args.crosstalk_input or root/args.tape/f'CH{args.channel:02d}'
        if args.denoised_input is None and not args.skip_crosstalk and not (source/'crosstalk.json').is_file():
            raise ValueError('Run stage 04 first; completed crosstalk.json is required')
        result = render_shared_channel(plan, args.channel,
            source,
            args.output or DEFAULT_DEFLUTTERED_ROOT/args.tape/f'CH{args.channel:02d}', dry_run=args.dry_run)
        print(json.dumps(result, indent=2))
        return 0
    except (OSError, ValueError, RuntimeError, KeyError, TypeError) as error:
        p.error(str(error))


if __name__ == '__main__':
    main()
