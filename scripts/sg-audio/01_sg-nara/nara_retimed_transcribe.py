"""Transcript of the respeeded NARA flight tapes, on their mission-rate output clock.

Transcribes the transcription mix of each tape written by ``nara_respeed.py``
(``F:/Apollo_15_data/processing/sg_nara/respeed/<tape>/<short>_mix.wav``): the equal-weight mean
of the tape's files with the timecode-bearing stretches gated out. Its clock runs at mission rate:
output seconds = tape seconds + D(tape seconds), and GET = output seconds + the segment constant
recorded in ``<short>.json``. Word and segment ``sf``/``ef`` are frames on that output clock.

Uses the shared recognizer and chunk store (scripts/00_common) with its own store and lock under
``F:/Apollo_15_data/processing/sg_nara/retimed_transcripts``, so it runs alongside the other
transcription stages. A tape is selected only when its render provenance is complete (the mix's
frame count matches the WAV); the provenance checksum and map checksum are part of the source
identity, so a re-rendered tape gets a new identity.

Subcommands are those of 30-track stage 06 (the shared transcribe_cli): inventory, estimate, run, status, watch, verify, stop.
For example:
  uv run python scripts/run.py nara_retimed_transcribe.py run --tape all --batch-size 8 --prep-workers 4
"""
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))
from _bootstrap import install as _install
_install()

from pathlib import Path

from nara_sources import (RESPEED_ROOT, RETIMED_EVIDENCE_ROOT as EVIDENCE_ROOT,
                          RETIMED_TRANSCRIPT_ROOT as TRANSCRIPT_ROOT, retimed_sources)
import transcribe_cli
from transcribe_cli import Collection


def add_selection(group):
    group.add_argument('--tape', action='append', default=[], help="NARA tape such as 640-AAA, or 'all'")
    group.add_argument('--respeed-root', type=Path, default=RESPEED_ROOT)


TAPES = Collection(
    name='sg_nara_respeed', transcript_root=TRANSCRIPT_ROOT, evidence_root=EVIDENCE_ROOT, add_selection=add_selection,
    has_selection=lambda args: bool(args.tape),
    select=lambda args, cache, hash_external, errors: retimed_sources(args.respeed_root, args.tape, cache, errors),
    scope=lambda args: dict(tape=args.tape, respeed_root=str(args.respeed_root)),
    selection_hint='Select tapes with --tape NAME or --tape all', window_help='retimed output seconds')


def main(argv=None):
    return transcribe_cli.main(TAPES, __doc__, list(_sys.argv[1:] if argv is None else argv))


if __name__ == '__main__':
    raise SystemExit(main())
