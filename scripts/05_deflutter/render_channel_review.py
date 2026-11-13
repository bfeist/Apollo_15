"""Matched Audition WAVs from stage-04 audio and the saved shared tape map."""
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
from shared_tape import DEFAULT_PLANNING_ROOT, index_path


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--tape', required=True)
    p.add_argument('--channel', type=int, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--flutter-plan', type=Path)
    p.add_argument('--planning-root', type=Path, default=DEFAULT_PLANNING_ROOT)
    p.add_argument('--denoised-root', type=Path, default=CANONICAL)
    p.add_argument('--crosstalk-root', type=Path, default=DEFAULT_CROSSTALK_ROOT)
    p.add_argument('--skip-crosstalk', action='store_true', help='Explicit denoised-only comparison')
    p.add_argument('--start', type=float, help='Canonical tape seconds; defaults to channel start')
    p.add_argument('--duration', type=float, help='Crop the existing tape map; never reanchor the excerpt')
    p.add_argument('--render-block-seconds', type=float, default=5.)
    p.add_argument('--dry-run', action='store_true')
    args = p.parse_args(argv)
    try:
        if Path(args.tape).name != args.tape or args.tape in {'.', '..'}:
            raise ValueError('Tape must be a simple folder name')
        path = args.flutter_plan or args.planning_root/args.tape/'plan.json'
        if json.loads(index_path(path).read_text(encoding='utf-8'))['inventory']['tape'] != args.tape:
            raise ValueError('Requested tape and shared plan disagree')
        source = (args.denoised_root if args.skip_crosstalk else args.crosstalk_root)/args.tape/f'CH{args.channel:02d}'
        if not args.skip_crosstalk and not (source/'crosstalk.json').is_file():
            raise ValueError('Run stage 04 first, or explicitly use --skip-crosstalk for a denoised-only comparison')
        result = render_shared_channel(path, args.channel, source, args.output,
            start=args.start, duration=args.duration, comparison=True, dry_run=args.dry_run,
            block_seconds=args.render_block_seconds)
        print(json.dumps(result, indent=2))
        return 0
    except (OSError, ValueError, RuntimeError, KeyError, TypeError) as error:
        p.error(str(error))


if __name__ == '__main__':
    main()
