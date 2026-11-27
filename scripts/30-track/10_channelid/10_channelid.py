"""Stage 10: identify the true track-sheet channel of every delivered 30-track channel.

The digitized tapes were not aligned on the playhead, so delivered CH<n> need not be
track-sheet channel n. This stage reads the stage-06a timing transcripts (not yet
retimed; only relative tape time is used), isolates each channel's distinctive speech,
asks GLM (HGX endpoint) which console position it is, and fits one map per tape.

    uv run python scripts/30-track/10_channelid/10_channelid.py run --tape A15_T510093
    uv run python scripts/30-track/10_channelid/10_channelid.py run --all-tapes --workers 6
    uv run python scripts/30-track/10_channelid/10_channelid.py shared    # final maps
    uv run python scripts/30-track/10_channelid/10_channelid.py report
    uv run python scripts/30-track/10_channelid/10_channelid.py show A15_T510093

Outputs go to ``D:/Apollo_15_30-Track/channel_id`` (``--root``): ``evidence/``, ``glm/``
(every prompt, reasoning and answer), ``maps/<tape>.json``, ``labels/<tape>.md``,
``channel_map.json``, ``channel_map.tsv`` and ``CHANNEL_MAP.md``. Evidence and GLM answers
are reused when their inputs are unchanged, so reruns only do new work.

``run`` solves each tape alone. ``shared`` then fits the missing true tracks once per head
stack (HR1U, HR1L, HR2U, HR2L) with one placement per reel, and writes the final maps
to ``<root>/shared/`` (same file layout); see ``channelid_shared.py``.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import channelid_evidence as ce  # noqa: E402
import channelid_irig_nara as cin  # noqa: E402
import channelid_llm as cl  # noqa: E402
import channelid_report as cr  # noqa: E402
import channelid_sheets as cs  # noqa: E402
import channelid_solve as cv  # noqa: E402
from channelid_dashboard import State  # noqa: E402

DATA = Path('D:/Apollo_15_30-Track')
DEFAULT_ROOT = DATA / 'channel_id'
DEFAULT_TRANSCRIPTS = DATA / 'timing_transcripts' / 'asr-c1198b48a6d3'


def all_tapes(transcripts: Path):
    return sorted(p.name for p in (transcripts / 'sources' / 'tape').glob('A15_T*') if p.is_dir())


def load_evidence(root: Path, transcripts: Path, tape: str, rebuild=False, log=print):
    path = root / 'evidence' / f'{tape}.json.gz'
    if path.is_file() and not rebuild:
        ev = ce.read(path)
        current = {str(d): ce.identity_state(folder)['sha256']
                   for d, (folder, _) in ce.tape_sources(transcripts, tape).items()}
        stored = {d: p['transcript_sha256'] for d, p in ev['provenance'].items()}
        if ev.get('version') == ce.EVIDENCE_VERSION and current == stored:
            return ev
        log(f'{tape}: transcripts changed, rebuilding evidence')
    ev = ce.build(transcripts, tape, log=lambda *a: None)
    ce.write(path, ev)
    return ce.read(path)          # JSON round trip: channel keys are strings everywhere


def run(args):
    root, transcripts = Path(args.root), Path(args.transcripts)
    tapes = all_tapes(transcripts) if args.all_tapes else args.tape
    if not tapes:
        raise SystemExit('Give --tape or --all-tapes')
    model = cl.probe()
    state = State(model, tapes, args.workers)
    solve_lock = threading.Lock()
    plain = args.plain or not sys.stdout.isatty()

    def say(text):
        state.event(text)
        if plain:
            print(text, flush=True)

    def assess_one(tape, d, prompt, sheet):
        state.start(tape, d)
        cache = root / 'glm' / tape / f'CH{d:02d}.json'
        on_delta = None if plain else state.delta
        record, cached = cl.assess(prompt, cache, on_delta=on_delta, model=model)
        state.finish(tape, d, record, cached, sheet)
        if plain:
            top = record['parsed']['candidates'][0]
            print(f'  {tape} CH{d:02d} -> {top["channel"]:>2} p={top["p"]:.2f} '
                  f'{"(cached) " if cached else ""}{record["parsed"]["summary"][:150]}', flush=True)
        return record['parsed']

    def finish_tape(tape, ev, futures):
        results = {}
        for d, f in futures.items():
            try:
                results[d] = f.result()
            except Exception as exc:  # noqa: BLE001 - reported, tape solved without it
                state.fail(tape, d, exc)
        with solve_lock:
            sol = cv.solve(results, evidence=ev, irig=cin.tape_irig(root, tape))
            m = cr.tape_map(tape, ev, results, sol, model)
            cr.write_tape(root, m)
            cr.write_combined(root)
            line = (f'{m["rule"]:<30} {m["confidence"].upper():<6} median p {m["median_channel_posterior"]:.2f}  '
                    f'anchor {("CH%02d" % m["anchor"]["delivered_channel"]) if m["anchor"]["delivered_channel"] else m["anchor"]["status"]} {"ok" if m["anchor"]["consistent"] else "--"}  '
                    f'GLM confirmed {m["confirmed"]}/{len(m["channels"])}')
            state.result(tape, line)
            say(f'{tape}: {line}')

    prep = cf.ThreadPoolExecutor(1, thread_name_prefix='prep')
    pool = cf.ThreadPoolExecutor(args.workers, thread_name_prefix='glm')
    done_pool = cf.ThreadPoolExecutor(1, thread_name_prefix='solve')
    ev_futures = {t: prep.submit(load_evidence, root, transcripts, t, args.rebuild_evidence, say) for t in tapes}
    tape_done = []

    def driver():
        for tape in tapes:
            state.tape(tape, 'evidence')
            try:
                ev = ev_futures[tape].result()
            except Exception as exc:  # noqa: BLE001
                say(f'{tape}: evidence failed: {exc}')
                state.tape(tape, 'failed')
                continue
            sheet = cs.sheet(ev['recorder'])
            labels = {int(d): cr.parse_filename(p['source_filename']).get('label', '')
                      for d, p in ev['provenance'].items()}
            chans = sorted(int(d) for d in ev['channels'])
            state.add_jobs(tape, chans, labels)
            state.tape(tape, 'assessing')
            say(f'{tape} {ev["head"]}: {len(chans)} channels, annotation readout on CH{ev["anchors"]["channel"] or 0:02d}')
            futures = {}
            for d in chans:
                c = ev['channels'][str(d)]
                prompt = cl.build_prompt(ev['recorder'], sheet, c, c['excerpt'])
                futures[d] = pool.submit(assess_one, tape, d, prompt, sheet)
            tape_done.append(done_pool.submit(lambda t=tape, e=ev, f=futures: (cf.wait(f.values()),
                                                                                   finish_tape(t, e, f))))

    if plain:
        driver()
        for f in tape_done:
            f.result()
    else:
        from rich.console import Console
        from rich.live import Live
        with Live(state, console=Console(), refresh_per_second=6, screen=not args.no_screen):
            driver()
            for f in tape_done:
                f.result()
            time.sleep(1.5)
        for tape, line in state.results:
            print(f'{tape}: {line}')
    prep.shutdown(); pool.shutdown(); done_pool.shutdown()
    cr.write_combined(root)
    print(f'Wrote {root / "CHANNEL_MAP.md"}')


def shared(args):
    import channelid_shared
    channelid_shared.main(['--root', args.root, '--grouping', args.grouping])


def report(args):
    maps = cr.write_combined(Path(args.root))
    print(Path(args.root, 'CHANNEL_MAP.md').read_text(encoding='utf-8'))
    print(f'{len(maps)} tapes')


def show(args):
    print(Path(args.root, 'labels', f'{args.tape_name}.md').read_text(encoding='utf-8'))


def main(argv=None):
    sys.stdout.reconfigure(encoding='utf-8')
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--root', default=str(DEFAULT_ROOT))
    p.add_argument('--transcripts', default=str(DEFAULT_TRANSCRIPTS))
    sub = p.add_subparsers(dest='cmd', required=True)
    r = sub.add_parser('run')
    r.add_argument('--tape', action='append', default=[])
    r.add_argument('--all-tapes', action='store_true')
    r.add_argument('--workers', type=int, default=4)
    r.add_argument('--rebuild-evidence', action='store_true')
    r.add_argument('--plain', action='store_true', help='line output instead of the dashboard')
    r.add_argument('--no-screen', action='store_true', help='dashboard without the alternate screen')
    r.set_defaults(func=run)
    sh = sub.add_parser('shared', help='final maps: missing-track pattern shared per head stack (no GLM calls)')
    sh.add_argument('--grouping', choices=('all', 'recorder', 'head', 'tape'), default='tape')
    sh.set_defaults(func=shared)
    sub.add_parser('report').set_defaults(func=report)
    s = sub.add_parser('show')
    s.add_argument('tape_name')
    s.set_defaults(func=show)
    args = p.parse_args(argv)
    args.func(args)


if __name__ == '__main__':
    main()
