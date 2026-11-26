"""Shared transcription command line: inventory, estimate, run, status, watch, verify, stop.

Stage 06a (30-track), scripts/sg-audio (space-to-ground reels) and
scripts/video/dvds (DVD audio streams) each call :func:`main` with their own
:class:`Collection`. They share the recognizer and chunk store code but write
separate stores with separate locks, so they can run at the same time.
"""
import argparse
import json
import os
import time
from collections import defaultdict
from pathlib import Path

from dataclasses import dataclass
from typing import Callable

from audio_sources import HashCache, Source, file_sha256
from mission_asr import Settings
from mission_store import Store, now, read_json, write_json
import transcribe_run as runner

CUDA_DIRS = [Path('C:/Program Files/NVIDIA/CUDNN/v9.10/bin/12.9'),
             Path('C:/Program Files/NVIDIA GPU Computing Toolkit/CUDA/v12.8/bin')]


def add_cuda(dirs):
    handles = []
    for path in dirs:
        if path.is_dir() and os.name == 'nt':
            handles.append(os.add_dll_directory(str(path)))
            os.environ['PATH'] = str(path) + os.pathsep + os.environ.get('PATH', '')
    return handles


@dataclass
class Collection:
    """What a transcription CLI selects. Each source family supplies one; stores and locks stay separate."""
    name: str
    transcript_root: Path
    evidence_root: Path
    add_selection: Callable          # (argument group) -> None
    has_selection: Callable          # (args) -> bool
    select: Callable                 # (args, hash_cache, hash_external, errors) -> list[Source]
    scope: Callable                  # (args) -> dict
    selection_hint: str
    window_help: str
    inventory_note: Callable | None = None   # (args, {collection: [sources, hours]}) -> None
    hash_seed: Path | None = None    # read-only checksum cache to seed from (avoids rehashing media)


COLLECTION: Collection = None  # set by main()


def selection_arguments(parser):
    group = parser.add_argument_group('selection')
    COLLECTION.add_selection(group)
    group.add_argument('--start', type=float, help='Window start: ' + COLLECTION.window_help)
    group.add_argument('--end', type=float, help='Window end in the same coordinates')
    group.add_argument('--transcript-root', type=Path, default=COLLECTION.transcript_root)
    group.add_argument('--evidence-root', type=Path, default=COLLECTION.evidence_root)


def settings_arguments(parser):
    group = parser.add_argument_group('recognizer (defaults are the tested configuration)')
    defaults = Settings()
    group.add_argument('--model', default=defaults.model)
    group.add_argument('--batch-size', type=int, default=defaults.batch_size)
    group.add_argument('--beam-size', type=int, default=defaults.beam_size)
    group.add_argument('--vad-threshold', type=float, default=defaults.vad_threshold)
    group.add_argument('--vad-input', choices=('voice_band_rms', 'original'), default=defaults.vad_input)
    group.add_argument('--chunk-seconds', type=float, default=defaults.chunk_seconds)


def settings_from(args):
    return Settings(model=args.model, batch_size=args.batch_size, beam_size=args.beam_size,
                    vad_threshold=args.vad_threshold, vad_input=args.vad_input,
                    chunk_seconds=args.chunk_seconds).validate()


def has_selection(args):
    return COLLECTION.has_selection(args)


def seeded_cache(store):
    cache = HashCache(store.evidence / 'hash_cache.json')
    seed = COLLECTION.hash_seed / 'hash_cache.json' if COLLECTION.hash_seed else None
    if seed and seed.is_file() and seed != cache.path:
        for key, value in HashCache(seed).values.items():
            if key not in cache.values:
                cache.values[key] = value
                cache.changed = True
    return cache


def selected_sources(args, hash_external=True):
    if not has_selection(args):
        raise SystemExit(COLLECTION.selection_hint)
    store = Store(args.transcript_root, args.evidence_root)
    cache = seeded_cache(store)
    if not hash_external:
        cache.get = lambda path, compute=True: HashCache.get(cache, path, compute=False)
    errors = []
    sources = COLLECTION.select(args, cache, hash_external, errors)
    cache.save()
    return store, sources, errors


def windows_from(args):
    if args.start is None and args.end is None:
        return None
    return {'*': (args.start, args.end)}


def scope_of(args):
    return dict(COLLECTION.scope(args), start=args.start, end=args.end)


def table(rows, columns):
    widths = [max(len(str(c)), *(len(str(r[i])) for r in rows)) if rows else len(str(c)) for i, c in enumerate(columns)]
    line = lambda values: '  '.join(str(v).rjust(w) if i else str(v).ljust(w) for i, (v, w) in enumerate(zip(values, widths)))
    print(line(columns))
    for row in rows:
        print(line(row))


def cmd_inventory(args):
    store, sources, errors = selected_sources(args, hash_external=not args.no_hash)
    by = defaultdict(lambda: [0, 0.0])
    for source in sources:
        by[source.collection][0] += 1
        by[source.collection][1] += source.hours
    table([(c, n, f'{h:,.2f}') for c, (n, h) in sorted(by.items())], ('collection', 'sources', 'channel-hours'))
    if COLLECTION.inventory_note:
        COLLECTION.inventory_note(args, by)
    for error in errors:
        print(f"  {'skipped' if error.get('skipped') else 'error'}: {error['key']}: {error['error']}")
    if args.write:
        path = store.evidence / 'inventory' / (time.strftime('%Y%m%d_%H%M%S') + '.json')
        write_json(path, dict(created=now(), scope=scope_of(args), sources=[s.to_json() for s in sources], errors=errors))
        print(f'Wrote {path}')
    if args.list:
        for source in sources:
            print(f'{source.key}\t{source.identity_id}\t{source.hours:.3f} h\torigin {source.canonical_origin_s:.4f} s\t{source.content_sha256[:12]}')
    return 1 if any(not e.get('skipped') for e in errors) else 0


def cmd_estimate(args):
    store, sources, errors = selected_sources(args)
    settings = settings_from(args)
    rows = runner.coverage(store, settings, sources, windows_from(args))
    total = sum(r['hours'] for r in rows)
    done = sum(r['done_hours'] for r in rows)
    retained = sum(r['retained_hours'] for r in rows)
    remaining = total - done
    settings_id = store.resolve_settings(settings)
    sessions = store.sessions()
    model = runner.throughput_model([s for s in sessions if s.get('settings_id') == settings_id])
    aligned = sum(r['aligned_chunks'] for r in rows)
    chunks = sum(r['done_chunks'] for r in rows)
    print(f'Store {settings_id}. Selected {len(sources)} sources, {total:,.2f} channel-hours; {done:,.2f} transcribed; '
          f'{remaining:,.2f} remaining; {aligned:,} of {chunks:,} transcribed chunks aligned.')
    if model:
        print(f"Measured on {model['measured_channel_hours']:.2f} channel-hours in {model['groups']} groups: "
              f"{model['measured_rate']:.0f}x at {model['measured_occupancy']:.1%} speech occupancy; "
              f"wall = {model['seconds_per_source_hour']:.2f} s/source-h + {model['seconds_per_retained_hour']:.2f} s/retained-h.")
    else:
        print('No measured groups for these settings; using the pilot two-point model.')
    observed = retained / done if done else None
    scenarios = [('observed so far', observed)] if observed else []
    scenarios += [('pilot census (33.8%)', 0.338), ('pilot sustained busy (68.3%)', 0.683)]
    out = []
    for label, occupancy in scenarios:
        p = runner.projection(remaining, occupancy, model)
        out.append((label, f"{occupancy:.1%}", f"{p['rate']:.0f}x", f"{p['hours']:.1f} h"))
    table(out, ('scenario', 'occupancy', 'rate', 'remaining first pass'))
    return 0


def cmd_run(args):
    handles = add_cuda(CUDA_DIRS + args.dll_dir)
    store, sources, errors = selected_sources(args)
    for error in errors:
        print(f"  {'skipped' if error.get('skipped') else 'error'}: {error['key']}: {error['error']}")
    missing = [s.key for s in sources if not s.content_sha256]
    if missing:
        raise SystemExit(f'Sources lack content checksums: {missing[:5]}')
    (store.evidence / 'STOP').unlink(missing_ok=True)
    settings = settings_from(args)
    try:
        runner.run(store, settings, sources, windows=windows_from(args), group_hours=args.group_hours,
                   prep_workers=args.prep_workers, max_hours=args.max_hours, max_chunks=args.max_chunks,
                   scope=scope_of(args), log=lambda m: print(m, flush=True))
    except KeyboardInterrupt:
        print('Stopped by keyboard interrupt; rerun the same command to resume.', flush=True)
        return 130
    finally:
        for handle in handles:
            handle.close()
    return 0


def fmt_hours(hours):
    return f'{hours:,.2f}'


def cmd_status(args):
    while True:
        store = Store(args.transcript_root, args.evidence_root)
        settings = settings_from(args)
        settings_id = store.resolve_settings(settings)
        sessions = [s for s in store.sessions() if s.get('settings_id') == settings_id]
        if has_selection(args):
            _, sources, _ = selected_sources(args)
            windows = windows_from(args)
        else:
            last = next((s for s in reversed(sessions) if s.get('kind') == 'first_pass'
                         and (Path(s['folder']) / 'sources.json').is_file()), None)
            if last is None:
                print('No sessions yet; pass a selection to report coverage.')
                return 0
            saved = read_json(Path(last['folder']) / 'sources.json')
            sources = [Source.from_json(item) for item in saved['sources']]
            windows = {k: tuple(v) for k, v in saved['windows'].items()} if saved.get('windows') else None
            print(f"Scope of session {last['session']}: {json.dumps(last['scope'])}")
        rows = (runner.coverage(store, settings, sources, windows) if args.scan
                else runner.coverage_from_ledgers(sessions, settings, sources, windows, settings_id))
        total = sum(r['hours'] for r in rows)
        done = sum(r['done_hours'] for r in rows)
        retained = sum(r['retained_hours'] for r in rows)
        words = sum(r['words'] for r in rows)
        flagged = sum(r['flagged_words'] for r in rows)
        print(f'Store {settings_id}: {done:,.2f} of {total:,.2f} channel-hours transcribed '
              f'({done / total:.1%}); speech occupancy {retained / done if done else 0:.1%}; '
              f'{words:,} words, {flagged:,} timestamp flags.' if total else 'Nothing selected.')
        if args.scan and total:
            aligned = sum(r['aligned_chunks'] for r in rows)
            chunks = sum(r['done_chunks'] for r in rows)
            print(f"Aligned: {aligned:,} of {chunks:,} transcribed chunks; "
                  f"{sum(r['failed_contexts'] for r in rows):,} decoder contexts could not be aligned.")
        superseded = [r for r in rows if r['superseded_identities']]
        if superseded:
            print(f'{len(superseded)} sources have superseded transcript identities (source changed); kept read-only.')
        summaries = [runner.session_summary(s) for s in sessions]
        table([(s['session'], s['kind'], s['status'], fmt_hours(s['hours']), fmt_hours(s['aligned_hours']),
                f"{s['wall_seconds']:.0f}", f"{s['rate']:.0f}x" if s['rate'] else '-',
                f"{s['occupancy']:.1%}" if s['occupancy'] else '-', s['failures']) for s in summaries[-args.sessions:]],
              ('session', 'kind', 'status', 'transcribed ch-h', 'aligned ch-h', 'wall s', 'rate', 'occupancy',
               'failures'))
        model = runner.throughput_model(sessions)
        remaining = total - done
        if model and remaining > 0:
            occupancy = retained / done if done else model['measured_occupancy']
            p = runner.projection(remaining, occupancy, model)
            print(f"ETA {p['hours']:.1f} h for {remaining:,.2f} remaining channel-hours at {p['rate']:.0f}x "
                  f"(measured {model['measured_rate']:.0f}x over {model['measured_channel_hours']:.1f} ch-h).")
        failures = [f for s in sessions for f in s['failures']]
        completed = {(r['key']) for r in rows if r['done_chunks'] == r['chunks']}
        open_failures = [f for f in failures if f['source_key'] not in completed]
        if open_failures:
            print(f'{len(open_failures)} unresolved chunk failures, latest:')
            for f in open_failures[-5:]:
                print(f"  {f['source_key']} c{f['chunk']:05d} {f['phase']}: {f['error']}")
        if args.by_source:
            table([(r['key'], fmt_hours(r['hours']), fmt_hours(r['done_hours']),
                    f"{r['retained_hours'] / r['done_hours']:.0%}" if r['done_hours'] else '-', r['words'])
                   for r in rows], ('source', 'ch-h', 'done', 'occupancy', 'words'))
        if not args.watch:
            return 0
        time.sleep(args.watch)
        print()


def cmd_watch(args):
    import transcribe_watch
    return transcribe_watch.watch(args.transcript_root, args.evidence_root, session_id=args.session,
                                  channels=getattr(args, "channel", None) or None, min_words=args.min_words, grep=args.grep,
                                  max_per_second=args.lines_per_second, poll_seconds=args.poll,
                                  screen=not args.no_screen)


def cmd_verify(args):
    store, sources, _ = selected_sources(args)
    settings = settings_from(args)
    settings_id = store.resolve_settings(settings)
    bad = 0
    for source in sources:
        if args.audio:
            observed = file_sha256(source.path)
            if observed != source.content_sha256:
                print(f'CHANGED {source.key}: {observed} != {source.content_sha256}')
                bad += 1
        for folder, receipt in store.source_chunks(settings_id, source):
            try:
                store.completed(settings_id, source, receipt['index'], receipt['nominal_frames'], verify=True)
            except ValueError as error:
                print(error)
                bad += 1
    print(f'Verified {len(sources)} sources in store {settings_id}; {bad} problems.')
    return 1 if bad else 0


def cmd_stop(args):
    store = Store(args.transcript_root, args.evidence_root)
    path = store.evidence / 'STOP'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(now(), encoding='utf-8')
    print(f'Requested stop: {path}. The running session finishes its current group, publishes it and exits.')
    return 0


def main(collection: Collection, description: str, argv=None):
    global COLLECTION
    COLLECTION = collection
    parser = argparse.ArgumentParser(description=description, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='command', required=True)
    for name, function, help_text in (
            ('inventory', cmd_inventory, 'List sources and channel-hours'),
            ('estimate', cmd_estimate, 'Project remaining time from measured throughput'),
            ('run', cmd_run, 'Transcribe pending chunks (resumable)'),
            ('status', cmd_status, 'Report coverage, throughput, failures and ETA'),
            ('watch', cmd_watch, 'Live dashboard with a scrolling transcript feed (read-only)'),
            ('verify', cmd_verify, 'Check source and chunk checksums'),
            ('stop', cmd_stop, 'Stop a running session after its current group')):
        command = sub.add_parser(name, help=help_text)
        command.set_defaults(function=function)
        selection_arguments(command)
        settings_arguments(command)
        if name == 'inventory':
            command.add_argument('--write', action='store_true', help='Save the inventory under the evidence root')
            command.add_argument('--list', action='store_true', help='Print every source identity')
            command.add_argument('--no-hash', action='store_true', help='Skip hashing new external media (listing only)')
        if name == 'run':
            command.add_argument('--dll-dir', type=Path, action='append', default=[], help='Extra CUDA/cuDNN DLL folder')
        if name == 'run':
            command.add_argument('--group-hours', type=float, default=5.0, help='Source hours per GPU group')
            command.add_argument('--prep-workers', type=int, default=8)
            command.add_argument('--max-hours', type=float, help='Stop after this many wall hours (resumable)')
            command.add_argument('--max-chunks', type=int, help='Process at most this many pending chunks')
        if name == 'status':
            command.add_argument('--watch', type=float, help='Repeat every N seconds')
            command.add_argument('--sessions', type=int, default=8)
            command.add_argument('--by-source', action='store_true')
            command.add_argument('--scan', action='store_true',
                                 help='Read every chunk receipt (slow; also reports superseded identities)')
        if name == 'watch':
            command.add_argument('--session', help='Watch this session instead of the newest first pass')
            command.add_argument('--min-words', type=int, default=1, help='Hide segments with fewer words')
            command.add_argument('--grep', help='Only show segments matching this regex (case-insensitive)')
            command.add_argument('--lines-per-second', type=float, default=6.0,
                                 help='Feed speed cap; busier groups are evenly sampled (0 = show all)')
            command.add_argument('--poll', type=float, default=2.0, help='Seconds between ledger polls')
            command.add_argument('--no-screen', action='store_true', help='Draw inline instead of full-screen')
        if name == 'verify':
            command.add_argument('--audio', action='store_true', help='Recompute full source checksums (slow)')
    args = parser.parse_args(argv)
    return args.function(args)
