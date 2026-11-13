"""Stage 04: remove evidenced delayed repeats after denoising, before de-flutter."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _bootstrap import install
install()

import argparse
import json
from denoise_archive import CANONICAL, ORIGINALS
from flutter_planning import inventory
from crosstalk_stage import DEFAULT_CROSSTALK_ROOT, DEFAULT_EVIDENCE_ROOT, render_channel


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--tape', required=True)
    p.add_argument('--channel', type=int, required=True)
    p.add_argument('--concatenated', type=Path, default=ORIGINALS)
    p.add_argument('--denoised-root', type=Path, default=CANONICAL)
    p.add_argument('--evidence-root', type=Path, default=DEFAULT_EVIDENCE_ROOT)
    p.add_argument('--output', type=Path)
    p.add_argument('--dry-run', action='store_true')
    args = p.parse_args(argv)
    try:
        if Path(args.tape).name != args.tape or args.tape in {'.', '..'} or not 1 <= args.channel <= 30:
            raise ValueError('Expected a simple tape folder and channel 1-30')
        result = render_channel(inventory(args.concatenated/args.tape), args.channel,
            args.denoised_root, args.output or DEFAULT_CROSSTALK_ROOT/args.tape/f'CH{args.channel:02d}',
            args.evidence_root, dry_run=args.dry_run)
        print(json.dumps(result, indent=2))
        return 0
    except (OSError, ValueError, RuntimeError, KeyError, TypeError) as error:
        p.error(str(error))


if __name__ == '__main__':
    main()
