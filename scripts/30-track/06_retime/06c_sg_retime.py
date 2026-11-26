"""Stage 06c: retime the 30-track tapes against the space-to-ground set (IA and NARA).

docs/20_2026-11-25_sg_retime_30track.md. Every step reads saved artifacts only and writes a new
edition named by the digest of its inputs; completed editions are reused, never rewritten.

Subcommands
  status     Rescue state of the three transcript stores; per tape the channel audit and editions.
  reference  SG words on GET (IA through catalog/sg_drift, NARA through the respeed segments).
  anchors    Text pairs on every channel, pooled per tape: stagger, events, preliminary curve.
  refine     GCC-PHAT audio refinement at the text-located windows (precision step).
  quindar    Quindar bursts: NARA generator calibration, matches (a check) and rate witnesses.
  fit        One joint fit over the selected tapes; one shared-layout plan per tape, a proposed IA
             segment revision and coverage as the union of supported tape time.
  run        reference, anchors, [refine], [quindar] and fit in order.

Word times are the CTC-aligned ones (scripts/00_common/mission_align.py). While a store has a
running transcription session, or a transcript read is not fully aligned with the current
aligner, steps refuse unless ``--allow-provisional`` is given; such editions are named
``provisional-...`` and their plans cannot be applied by 06b. Apply a final plan with:

  uv run python scripts/run.py 06b_retime.py --tape TAPE --all-channels \\
      --quindar-plan <root>/sg_fit/<edition>/tapes/TAPE/plan.json --retimed-root D:/Apollo_15_30-Track/retimed_sg
"""
# Resolve shared modules from the stage folders.
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))
from _bootstrap import install as _install
_install()

import argparse
import sys
from pathlib import Path

import sg_stage
from sg_inputs import RETIMING_ROOT, STORES, store_state, tape_channels, tape_names, transcript_state


BLOCKED = []


def tapes_of(args):
    if args.all_tapes:
        return tape_names()
    if not args.tape:
        raise SystemExit('Select tapes with --tape or --all-tapes')
    return args.tape


def reference(args):
    return sg_stage.build_reference(args.allow_provisional, args.root)


def anchors(args, ref=None):
    ref = ref or reference(args)
    out = {}
    for tape in tapes_of(args):
        try:
            out[tape] = sg_stage.build_anchors(tape, ref, args.allow_provisional, args.root)
        except (ValueError, RuntimeError, KeyError) as error:
            print(f'{tape}: BLOCKED {error}', flush=True)
            BLOCKED.append(tape)
    return out


def current_anchors(args):
    ref, *_ = sg_stage.reference_edition(args.root)
    if not sg_stage.complete(ref):
        raise SystemExit(f'No reference edition {ref.name}; run the reference step first')
    out = {}
    for tape in tapes_of(args):
        folder, *_ = sg_stage.anchors_folder(tape, ref, args.root)
        if sg_stage.complete(folder):
            out[tape] = folder
        else:
            print(f'{tape}: no anchors edition {folder.name}; run the anchors step first', flush=True)
            BLOCKED.append(tape)
    return out


def refine(args, found=None):
    for tape, folder in (found or current_anchors(args)).items():
        try:
            sg_stage.build_refine(tape, folder, args.root, workers=args.workers)
        except (ValueError, RuntimeError, KeyError, OSError) as error:
            print(f'{tape}: BLOCKED {error}', flush=True)
            BLOCKED.append(tape)


def quindar(args, found=None):
    for tape, folder in (found or current_anchors(args)).items():
        try:
            sg_stage.build_quindar(tape, folder, args.root, workers=args.workers, include_ia=args.include_ia)
        except (ValueError, RuntimeError, KeyError, OSError) as error:
            print(f'{tape}: BLOCKED {error}', flush=True)
            BLOCKED.append(tape)


def fit(args, found=None):
    found = found or current_anchors(args)
    folder = sg_stage.build_fit(list(found), args.root, allow_provisional=args.allow_provisional, anchors_by_tape=found)
    print(f'Fit edition: {folder}')


def run(args):
    ref = reference(args)
    found = anchors(args, ref)
    if not args.skip_refine:
        refine(args, found)
    if not args.skip_quindar:
        quindar(args, found)
    fit(args, found)


def status(args):
    for name, store in STORES.items():
        state = store_state(store)
        print(f"{name:5s} {store.settings_id}: aligner {state['align_id']}; "
              + (f"running: {', '.join(state['running'])}" if state['running'] else 'no session running'))
    ref, _, _, reasons = sg_stage.reference_edition(args.root)
    print(f"Reference edition {ref.name}: {'present' if sg_stage.complete(ref) else 'not built'}"
          + (f" ({'; '.join(reasons)})" if reasons else ''))
    if not (args.tape or args.all_tapes):
        return
    for tape in tapes_of(args):
        channels = tape_channels(tape)
        refused = [c for c in channels if c['problem']]
        offsets = sorted({round(c['origin_s'], 6) for c in channels if c['origin_s']})
        line = f"{tape}: {len(channels)} channels, {len(refused)} refused" + (f", origins {offsets}" if offsets else '')
        if args.scan:
            states = [transcript_state(c['identity_dir']) for c in channels if c['identity_dir'] and not c['problem']]
            line += f", {sum(s['aligned_chunks'] for s in states):,}/{sum(s['chunks'] for s in states):,} chunks aligned"
        if sg_stage.complete(ref) and args.scan:
            folder, *_ = sg_stage.anchors_folder(tape, ref, args.root)
            line += f"; anchors {'present' if sg_stage.complete(folder) else 'missing'}"
            if sg_stage.complete(folder):
                for step in ('audio', 'quindar'):
                    child = sg_stage.latest_child(folder, step)
                    line += f", {step} {'present' if child else 'missing'}"
        print(line)
        for c in refused:
            print(f"   CH{c['channel']:02d}: {c['problem']}")


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest='command', required=True)
    for name in ('status', 'reference', 'anchors', 'refine', 'quindar', 'fit', 'run'):
        s = sub.add_parser(name)
        s.add_argument('--tape', action='append', default=[])
        s.add_argument('--all-tapes', action='store_true')
        s.add_argument('--allow-provisional', action='store_true',
                       help='Build from transcripts not yet fully aligned, or while a session runs (provisional editions)')
        s.add_argument('--root', type=Path, default=RETIMING_ROOT)
        s.add_argument('--workers', type=int, default=8)
        s.add_argument('--scan', action='store_true',
                       help='status: read every chunk receipt (aligned-chunk counts, anchor editions; slow)')
        s.add_argument('--include-ia', action='store_true', help='quindar: also scan and match the IA reels')
        s.add_argument('--skip-refine', action='store_true', help='run: no audio refinement')
        s.add_argument('--skip-quindar', action='store_true', help='run: no Quindar step')
    args = p.parse_args(argv)
    try:
        dict(status=status, reference=reference, anchors=anchors, refine=refine, quindar=quindar, fit=fit, run=run)[args.command](args)
    except sg_stage.Provisional as error:
        p.exit(2, f'{error}\n')
    if BLOCKED:
        print(f'Blocked: {", ".join(sorted(set(BLOCKED)))}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
