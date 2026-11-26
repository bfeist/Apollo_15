"""Timing transcript of the space-to-ground (SG) reels.

The words are used to fit each reel's playback drift against TEC and journal
GETs (sg_timing, docs/16 "Space-to-ground reels") and as an audio witness for
the mission transcripts. Whisper output is never published.

Uses the shared recognizer and chunk store (scripts/00_common) with its own
store and lock under F:/Apollo_15_data/processing/sg, so it can run alongside stage 06a. The
recognizer defaults equal stage 06a's, so settings IDs match.

Subcommands are those of stage 06a: inventory, estimate, run, status, watch,
verify, stop. Example:
  uv run python scripts/run.py sg_transcribe.py run --reel 572-AAA

While the local copy is still being made, ``follow`` transcribes each reel as
soon as its copy is complete and waits for the rest:
  uv run python scripts/run.py sg_transcribe.py follow --prep-workers 4
It passes any other ``run`` options through, and ends when every reel is
copied and transcribed, or after ``stop``.
"""
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))
from _bootstrap import install as _install
_install()

from pathlib import Path

import argparse
import time

from mission_store import Store
from sg_sources import (EVIDENCE_ROOT, LEGACY_EVIDENCE_ROOT, MASTER_ROOT, SG_ROOT, TRANSCRIPT_ROOT, copy_status,
                        sg_sources)
import transcribe_cli
from transcribe_cli import Collection


def add_selection(group):
    group.add_argument('--reel', action='append', default=[], help="SG reel such as 572-AAA, or 'all'")
    group.add_argument('--sg-root', type=Path, default=SG_ROOT)


REELS = Collection(
    name='sg', transcript_root=TRANSCRIPT_ROOT, evidence_root=EVIDENCE_ROOT, add_selection=add_selection,
    has_selection=lambda args: bool(args.reel),
    select=lambda args, cache, hash_external, errors: sg_sources(args.sg_root, args.reel, cache, errors),
    scope=lambda args: dict(reel=args.reel), selection_hint="Select reels with --reel NAME or --reel all",
    window_help='reel source seconds', hash_seed=LEGACY_EVIDENCE_ROOT)


def follow(argv):
    parser = argparse.ArgumentParser(prog='sg_transcribe.py follow',
                                     description='Transcribe each reel once its local copy is complete.')
    parser.add_argument('--sg-root', type=Path, default=SG_ROOT, help='Local working copy being filled')
    parser.add_argument('--master-root', type=Path, default=MASTER_ROOT, help='Master reels the copy comes from')
    parser.add_argument('--poll', type=float, default=60.0, help='Seconds between copy checks while waiting')
    parser.add_argument('--transcript-root', type=Path, default=TRANSCRIPT_ROOT)
    parser.add_argument('--evidence-root', type=Path, default=EVIDENCE_ROOT)
    args, passthrough = parser.parse_known_args(argv)
    store = Store(args.transcript_root, args.evidence_root)
    (store.evidence / 'STOP').unlink(missing_ok=True)
    started = set()
    while True:
        status = copy_status(args.sg_root, args.master_root)
        ready = sorted(r for r, s in status.items() if s == 'complete')
        waiting = sorted(r for r, s in status.items() if s != 'complete')
        new = [r for r in ready if r not in started]
        if new:
            print(f'follow: {len(ready)}/{len(status)} reels copied; transcribing {", ".join(new)}', flush=True)
            command = ['run', '--sg-root', str(args.sg_root), '--transcript-root', str(args.transcript_root),
                       '--evidence-root', str(args.evidence_root), *passthrough]
            for reel in ready:  # completed reels are reused, so passing all copied reels is cheap
                command += ['--reel', reel]
            code = transcribe_cli.main(REELS, __doc__, command)
            if code:
                return code
            started.update(ready)
            if store.stop_requested():
                print('follow: stop requested; rerun follow to resume.', flush=True)
                return 0
            continue
        if not waiting:
            print(f'follow: all {len(status)} reels copied and transcribed.', flush=True)
            return 0
        copying = [r for r in waiting if status[r] == 'copying']
        print(f'follow: {len(ready)}/{len(status)} done; waiting for '
              f'{copying[0] + " (copying)" if copying else waiting[0]} and {len(waiting) - 1} more', flush=True)
        try:
            time.sleep(args.poll)
        except KeyboardInterrupt:
            return 130
        if store.stop_requested():
            print('follow: stop requested while waiting.', flush=True)
            return 0


def main(argv=None):
    argv = list(_sys.argv[1:] if argv is None else argv)
    if argv[:1] == ['follow']:
        return follow(argv[1:])
    return transcribe_cli.main(REELS, __doc__, argv)


if __name__ == '__main__':
    raise SystemExit(main())
