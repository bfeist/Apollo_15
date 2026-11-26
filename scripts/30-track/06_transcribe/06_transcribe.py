"""Stage 06: timing transcript of the 30-track stage-05b (defluttered) channels.

The transcript places the tapes on mission time: stage 07a matches it against the
space-to-ground transcripts, and stage 07b carries it onto the retimed timeline
(``timed_transcript.tsv`` beside each retimed WAV). Formerly stage 06a.

The recognizer, chunk store and commands are the shared engine in
scripts/00_common (transcribe_cli, transcribe_run, mission_asr). The SG reels, the
NARA flight tapes and the DVD audio streams use the same engine through their own
entry points and stores. Earlier runs of this stage also held those sources; their
records under this stage's transcript root are left unchanged.

Subcommands
  inventory  List selected sources, their identities and channel-hours.
  estimate   Project the remaining first pass from measured (or pilot) throughput.
  run        Transcribe and align pending chunks; align published chunks that are not yet
             aligned (Whisper is not run again). Completed chunks are reused. Resumable.
  status     Coverage, speech occupancy, throughput, failures and ETA.
  watch      Read-only live dashboard: progress plus a scrolling feed of new transcript.
  verify     Recompute source checksums and transcript-chunk checksums.
  stop       Ask a running session to stop after its current group.

The archive-wide run is always an explicit ``run --all-tapes`` command.
"""
# Resolve shared modules from the stage folders.
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))
from _bootstrap import install as _install
_install()

from pathlib import Path

from mission_sources import DEFAULT_ROOTS, EVIDENCE_ROOT, TRANSCRIPT_ROOT, original_archive_hours, tape_sources
import transcribe_cli
from transcribe_cli import Collection


def add_selection(group):
    group.add_argument('--tape', action='append', default=[], help='Stage-05b tape folder; may be repeated')
    group.add_argument('--all-tapes', action='store_true', help='Every completed stage-05b tape')
    group.add_argument('--channel', type=int, action='append', default=[], help='Limit tape channels; may be repeated')
    group.add_argument('--tape-root', type=Path, default=DEFAULT_ROOTS['tape'])


def select(args, cache, hash_external, errors):
    return tape_sources(args.tape_root, None if args.all_tapes else args.tape, args.channel or None, errors)


def inventory_note(args, by):
    if args.all_tapes:
        archive = original_archive_hours(args.tape_root)
        published = by['tape'][1]
        print(f"Original 30-track archive: {archive['channel_hours']:,.2f} channel-hours in {archive['files']} WAVs "
              f"on {archive['tapes']} tapes; stage-05b inputs cover {published:,.2f} ({published / archive['channel_hours']:.1%}).")


TAPES = Collection(
    name='tape', transcript_root=TRANSCRIPT_ROOT, evidence_root=EVIDENCE_ROOT, add_selection=add_selection,
    has_selection=lambda args: bool(args.tape or args.all_tapes), select=select,
    scope=lambda args: dict(tape=args.tape, all_tapes=args.all_tapes, channel=args.channel),
    selection_hint='Select sources with --tape or --all-tapes', window_help='canonical tape seconds',
    inventory_note=inventory_note)


def main(argv=None):
    return transcribe_cli.main(TAPES, __doc__, argv)


if __name__ == '__main__':
    raise SystemExit(main())
