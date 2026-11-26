"""Timing transcript of the DVD audio streams (Spacecraft Films MP4s).

Uses the shared recognizer and chunk store (scripts/00_common) with its own
store and lock under F:/Apollo_15_data/processing/dvd, so it can run alongside stage 06a.

Subcommands are those of stage 06a: inventory, estimate, run, status, watch,
verify, stop. Example:
  uv run python scripts/run.py dvd_transcribe.py run --dvd 'D04-T02-P0[12]'
"""
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))
from _bootstrap import install as _install
_install()

from pathlib import Path

from dvd_sources import DVD_ROOT, EVIDENCE_ROOT, LEGACY_EVIDENCE_ROOT, TRANSCRIPT_ROOT, dvd_inventory
import transcribe_cli
from transcribe_cli import Collection


def add_selection(group):
    group.add_argument('--dvd', action='append', default=[],
                       help="DVD label regex such as 'D04-T02-P0[12]', or 'all'; every audio stream is a source")
    group.add_argument('--dvd-root', type=Path, default=DVD_ROOT)


DVDS = Collection(
    name='dvd', transcript_root=TRANSCRIPT_ROOT, evidence_root=EVIDENCE_ROOT, add_selection=add_selection,
    has_selection=lambda args: bool(args.dvd),
    select=lambda args, cache, hash_external, errors: dvd_inventory(args.dvd_root, args.dvd, cache,
                                                                    measure=hash_external, errors=errors),
    scope=lambda args: dict(dvd=args.dvd), selection_hint="Select DVD titles with --dvd REGEX or --dvd all",
    window_help='decoded stream seconds', hash_seed=LEGACY_EVIDENCE_ROOT)


def main(argv=None):
    return transcribe_cli.main(DVDS, __doc__, argv)


if __name__ == '__main__':
    raise SystemExit(main())
