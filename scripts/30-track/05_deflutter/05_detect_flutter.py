"""Stage 05 detection: check-tone-free tape-motion census on canonical originals.

An explicit preparation step before stage 05a. One Proprius coordinator owns
the block queue and assigns each tape/10-minute block once to persistent
workers on Proprius and Genus. Completed, verified blocks are reused; changed
settings, code or inputs need a new run ID. See docs/10_2026-11-15_deflutter_detection.md.
"""
import os
import sys
from pathlib import Path

for _name in ('OPENBLAS_NUM_THREADS', 'MKL_NUM_THREADS', 'OMP_NUM_THREADS',
              'BLIS_NUM_THREADS', 'NUMEXPR_NUM_THREADS', 'VECLIB_MAXIMUM_THREADS'):
    os.environ[_name] = '1'  # One thread per worker; set before numpy loads.

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _bootstrap import GENUS_SSH, genus_ssh, install
install()

import argparse
import json

import flutter_detection as detection
from flutter_alignment import ROOT as ALIGNMENT_ROOT
from capture_highband import check as audit


def tape_folders(args):
    root = Path(args.originals)
    if args.all_tapes:
        folders = sorted(p for p in root.glob('A15_T*') if p.is_dir() and p.name not in args.exclude_tape)
    elif args.tape:
        folders = [root/tape for tape in args.tape]
    else:
        raise ValueError('Select --tape or --all-tapes')
    missing = [str(f) for f in folders if not f.is_dir()]
    if missing or not folders:
        raise ValueError(f'Canonical tape folders are unavailable: {missing or root}')
    return folders


def open_selected(args, create=True):
    return detection.open_run(args.run_dir, root=args.root, originals=args.originals, tag=args.tag, create=create, alignment_root=args.alignment_root)


def pool(args, run_dir):
    chosen = detection.pool_settings(run_dir)
    tapes = chosen['tapes'] if args.tapes is None else args.tapes
    mode = chosen.get('input_mode', 'ssd')
    if args.command == 'benchmark' or (args.tapes is not None and tapes > 1):
        mode = 'direct'
    elif chosen.get('benchmarked') and 'input_mode' not in chosen and tapes > 1:
        mode = 'direct'  # Older multi-tape pool selections read from the archive.
    mode = args.input_mode or mode
    if mode == 'ssd' and args.tapes not in (None, 1):
        raise ValueError('SSD staging processes one tape at a time; use --tapes 1 or --input-mode direct')
    if not chosen.get('benchmarked'):
        print('Worker counts are unbenchmarked starting points (6 Proprius + 6 Genus); '
              'run the benchmark before the archive run.', flush=True)
    return dict(local_workers=chosen['windows'] if args.local_workers is None else args.local_workers,
                genus_workers=chosen['genus'] if args.genus_workers is None else args.genus_workers,
                genus_cpus=chosen['genus_cpus'] if args.genus_cpus is None else args.genus_cpus,
                stage_inputs=mode == 'ssd',
                tape_limit=1 if mode == 'ssd' else tapes,
                connection=dict(ssh=genus_ssh(args.genus_ssh or GENUS_SSH)))


def add_selection(p):
    p.add_argument('--alignment-root', type=Path, default=ALIGNMENT_ROOT)
    p.add_argument('--originals', type=Path, default=detection.DEFAULT_ORIGINALS)
    p.add_argument('--root', type=Path, default=detection.DEFAULT_DETECTION_ROOT,
                   help='Parent of run folders, deflutter_detection_census/<run-id>/')
    p.add_argument('--run-dir', type=Path, help='Explicit deflutter_detection_census/<run-id>/ folder')
    p.add_argument('--tag', help='Optional label bound into a new run ID')


def add_pool(p):
    p.add_argument('--tape', action='append', help='Canonical tape folder name; may be repeated')
    p.add_argument('--all-tapes', action='store_true')
    p.add_argument('--exclude-tape', action='append', default=[])
    p.add_argument('--local-workers', type=int, help=f'Proprius workers (0-{detection.WINDOWS_MAX_WORKERS})')
    p.add_argument('--input-mode', choices=('ssd', 'direct'), help='Stage active-tape WAVs sequentially on F or read originals directly')
    p.add_argument('--genus-workers', type=int, help='Genus SSH workers')
    p.add_argument('--genus-cpus', help='Genus CPU mask, or none')
    p.add_argument('--tapes', type=int, help='Simultaneously active tapes across both hosts')
    p.add_argument('--genus-ssh', help=f'Override {GENUS_SSH}')
    p.add_argument('--run-seconds', type=float, help='Bounded trial: stop scheduling after this many seconds')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    run = sub.add_parser('run', help='Coordinate detection on Proprius and Genus')
    add_selection(run)
    add_pool(run)
    run.add_argument('--rerun-offsets', action='store_true', help='Retrace only tapes with measured nonzero delivery offsets')
    run.add_argument('--plain', action='store_true', help='Print periodic status lines instead of the live dashboard')
    ident = sub.add_parser('run-id', help='Print the run ID for the current settings and code')
    add_selection(ident)
    status = sub.add_parser('status', help='Summarise the saved coordinator status')
    add_selection(status)
    stop = sub.add_parser('stop', help='Request a graceful stop at committed blocks')
    add_selection(stop)
    bench = sub.add_parser('benchmark', help='Isolated worker-count trial over a fixed block set')
    add_selection(bench)
    add_pool(bench)
    bench.add_argument('--trial', required=True)
    bench.add_argument('--blocks', required=True, help='Comma-separated TAPE/BLOCK keys, or @file')
    compare = sub.add_parser('compare-trials', help='Check identical gates and curves across hosts/pools')
    add_selection(compare)
    compare.add_argument('first', help='Benchmark trial name, or run for published blocks')
    compare.add_argument('second')
    compare.add_argument('--tolerance', type=float, default=1e-5)
    select = sub.add_parser('select-pool', help='Bind measured worker counts for later runs')
    add_selection(select)
    for name in ('--local-workers', '--genus-workers', '--tapes'):
        select.add_argument(name, type=int, required=True)
    select.add_argument('--genus-cpus', required=True)
    select.add_argument('--input-mode', choices=('ssd', 'direct'))
    select.add_argument('--trial', action='append', required=True, help='Supporting benchmark trial; repeat')
    worker = sub.add_parser('worker', help=argparse.SUPPRESS)
    worker.add_argument('--run-dir', type=Path, required=True)
    worker.add_argument('--cluster-dir', type=Path, required=True)
    worker.add_argument('--host', choices=['windows', 'genus'], required=True)
    worker.add_argument('--path-map', action='append', default=[], help='WINDOWS_PREFIX=POSIX_PREFIX')
    worker.add_argument('--scratch', type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == 'worker':
            mapping = [tuple(item.split('=', 1)) for item in args.path_map]
            if args.host == 'genus' and args.scratch is None:
                raise ValueError('Genus workers need --scratch for local staging')
            return detection.worker(args.run_dir, args.cluster_dir, args.host, mapping, args.scratch)
        if args.command == 'run-id':
            run_id, _ = detection.run_identity(detection.census.SETTINGS, args.originals, args.tag, args.alignment_root)
            run_dir = (args.run_dir or args.root/run_id).resolve()
            print(json.dumps(dict(run_id=run_id, run_dir=str(run_dir), exists=(run_dir/'config.json').is_file()),
                             indent=2))
            return 0
        create = args.command in ('run', 'benchmark')
        run_dir, config = open_selected(args, create=create)
        if args.command == 'status':
            path = run_dir/'cluster/status.json'
            state = detection.read_json(path)
            state.pop('jobs', None)
            print(json.dumps(state, indent=2))
            return 0
        if args.command == 'stop':
            (run_dir/'cluster').mkdir(parents=True, exist_ok=True)
            (run_dir/'cluster/stop_requested').touch()
            print('Stop requested; workers finish their current blocks.')
            return 0
        if args.command == 'compare-trials':
            result = detection.compare_trials(run_dir, config, args.first, args.second, args.tolerance)
            detection.atomic_json(run_dir/'benchmarks'/f'compare_{args.first}_{args.second}.json', result)
            print(json.dumps({k: v for k, v in result.items() if k != 'blocks'}, indent=2))
            return 0 if result['equivalent'] else 2
        if args.command == 'select-pool':
            for trial in args.trial:
                if not (run_dir/'benchmarks'/trial/'report.json').is_file():
                    raise ValueError(f'Benchmark trial report is missing: {trial}')
            if not 0 <= args.local_workers <= detection.WINDOWS_MAX_WORKERS:
                raise ValueError(f'Proprius workers must be between 0 and {detection.WINDOWS_MAX_WORKERS}')
            if args.input_mode == 'ssd' and args.tapes != 1:
                raise ValueError('Select one active tape for SSD inputs')
            selection = dict(run_id=config['run_id'], pool=dict(
                windows=args.local_workers, genus=args.genus_workers,
                genus_cpus=detection.cpu_list(args.genus_cpus), tapes=args.tapes,
                input_mode=args.input_mode or ('ssd' if args.tapes == 1 else 'direct')),
                trials={t: detection.sha256(run_dir/'benchmarks'/t/'report.json') for t in args.trial})
            detection.atomic_json(run_dir/'benchmarks'/'selection.json', selection)
            print(json.dumps(selection, indent=2))
            return 0
        folders = tape_folders(args)
        held = []
        if config['identity'].get('alignment_root'):
            ready = []
            for folder in folders:
                _, report = audit(folder, args.alignment_root)
                action = report['action']
                if action == 'review':
                    held.append(folder.name)
                    print(f"{folder.name}: timing unresolved for {report['unresolved']}", flush=True)
                elif action != 'duplicate_feed' and (not getattr(args, 'rerun_offsets', False) or action == 'retrace'):
                    ready.append(folder)
                else:
                    print(f'{folder.name}: {action}; skipping retrace', flush=True)
            folders = ready
            if not folders:
                return 2 if held else 0
        options = pool(args, run_dir)
        print(f'Detection run {config["run_id"]}: {run_dir}', flush=True)
        if args.command == 'benchmark':
            text = args.blocks
            if text.startswith('@'):
                text = Path(text[1:]).read_text(encoding='utf-8')
            keys = [k.strip() for k in text.replace('\n', ',').split(',') if k.strip()]
            report = detection.benchmark(run_dir, config, folders, args.trial, keys,
                                         run_seconds=args.run_seconds, **options)
            print(json.dumps({k: v for k, v in report.items() if k != 'per_block'}, indent=2))
            return report['returncode']
        session = detection.Session(run_dir, config)
        if not args.plain and sys.stdout.isatty():
            from rich.live import Live
            from census_monitor import CensusMonitor
            monitor = CensusMonitor(run_dir)
            with Live(monitor, refresh_per_second=1, vertical_overflow='crop'):
                return detection.coordinate(session, folders, run_seconds=args.run_seconds,
                                            output=monitor.message, on_status=monitor.update, **options) or (2 if held else 0)
        return detection.coordinate(session, folders, run_seconds=args.run_seconds, **options) or (2 if held else 0)
    except (OSError, ValueError, KeyError, RuntimeError) as error:
        print(f'Detection stopped: {error}', file=sys.stderr, flush=True)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
