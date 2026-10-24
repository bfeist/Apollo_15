"""Numbered Apollo 15 audio pipeline with a Rich terminal dashboard."""
import argparse
from collections import deque
from datetime import datetime
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
import uuid

from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from normalization import DEFAULT_TARGET_DBFS, DEFAULT_MAX_BOOST_DB, validate_settings
from audio_processing import DEFAULT_BROADBAND_CAP_DB
from flutter_plan_store import DEFAULT_PLANNING_ROOT, DEFAULT_DENOISED_ROOT, DEFAULT_DEFLUTTERED_ROOT, planning_directory, tape_directory
from quindar_retime import DEFAULT_RETIMED_ROOT
from quindar_survey import DEFAULT_ANALYSIS_ROOT

SCRIPTS = Path(__file__).resolve().parent
STAGES = [
    ('01', 'Concatenate', '01_concatenate.py', 'Verify fragment order and join channels'),
    ('03', 'Denoise', '03_denoise.py',
     f'Remove tones and residual hum, then reduce broadband noise by up to {DEFAULT_BROADBAND_CAP_DB} dB'),
    ('05a', 'Plan deflutter', '05a_plan_deflutter.py', 'Analyze original timing evidence; preserve reference tones'),
    ('05b', 'Deflutter', '05b_deflutter.py', 'Correct timing, then normalize with bounded constant gain'),
    ('05c', 'Retime', '05c_retime.py', 'Apply a smoothed Quindar anchor curve to defluttered audio'),
]


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['plan', 'run'], nargs='?', default='plan')
    parser.add_argument('--stages', default=None, help='Comma-separated stage numbers; always run in pipeline order')
    parser.add_argument('--input', type=Path, help='Speech excerpt folder; or previous stage output when starting midway')
    parser.add_argument('--output', type=Path, default=Path('runs'), help='Parent for unique run folders')
    parser.add_argument('--delivery', type=Path, default=Path('D:/Apollo_15_30-Track/orig_delivery'))
    parser.add_argument('--concatenated', type=Path, default=Path('D:/Apollo_15_30-Track/orig_concatenated'))
    tapes = parser.add_mutually_exclusive_group()
    tapes.add_argument('--tape', help='Select one canonical tape folder')
    tapes.add_argument('--all-tapes', action='store_true', help='Denoise all canonical tape folders under --concatenated')
    channels = parser.add_mutually_exclusive_group()
    channels.add_argument('--channel', type=int, help='Whole-channel mode; defaults to stages 03,05a,05b,05c for one tape')
    channels.add_argument('--all-channels', action='store_true', help='Denoise every available channel, including CH01/IRIG')
    parser.add_argument('--denoised-root', type=Path, default=DEFAULT_DENOISED_ROOT)
    parser.add_argument('--denoise-workers', type=int, default=14,
                        help='Whole-channel stage 03 workers (default: 14); 0 selects up to 16, 1 is serial')
    parser.add_argument('--defluttered-root', type=Path, default=DEFAULT_DEFLUTTERED_ROOT)
    parser.add_argument('--retimed-root', type=Path, default=DEFAULT_RETIMED_ROOT)
    parser.add_argument('--quindar-analysis-root', type=Path, default=DEFAULT_ANALYSIS_ROOT)
    parser.add_argument('--quindar-plan', type=Path, help='Stage 05c frozen shared tape plan')
    parser.add_argument('--experimental-single-channel', action='store_true', help='Allow the original per-file Quindar assessment workflow')
    parser.add_argument('--retime-candidates', type=Path, help='Stage 05c: full candidate inventory for the exact defluttered input')
    parser.add_argument('--retime-smoothing-seconds', type=float, help='Stage 05c: smoothing kernel sigma; default chooses by block validation')
    parser.add_argument('--experimental-transfer', action='store_true')
    parser.add_argument('--min-age', type=float, default=60)
    parser.add_argument('--flutter-config', type=Path, help='One-tape original reference and aligned target manifest')
    parser.add_argument('--flutter-planning-root', '--planning-root', type=Path, default=DEFAULT_PLANNING_ROOT,
                        help='Persistent per-tape plans; shared by stages 05a and 05b')
    parser.add_argument('--flutter-plan', type=Path, help='Stage 05b saved plan override; defaults to the tape plan')
    parser.add_argument('--flutter-targets', type=Path, help='Stage 05b region, target channels and excerpt alignment JSON')
    parser.add_argument('--flutter-region-seconds', type=float, default=60, help='Stage 05a region length (15-120 s)')
    parser.add_argument('--flutter-plan-start', type=float, default=0, help='Stage 05a bounded audit start; default whole tape')
    parser.add_argument('--flutter-plan-duration', type=float, help='Stage 05a optional bounded audit duration')
    parser.add_argument('--flutter-plan-workers', type=int, default=0, help='Stage 05a concurrent regions; 0 chooses up to eight workers')
    parser.add_argument('--skip-deflutter', action='store_true', help='Skip stage 05b correction and normalization')
    parser.add_argument('--target-dbfs', type=float, default=DEFAULT_TARGET_DBFS, help='Stages 05b/05c sample-peak target (default: -3 dBFS)')
    parser.add_argument('--max-boost-db', type=float, default=DEFAULT_MAX_BOOST_DB, help='Stages 05b/05c maximum boost (default: 12 dB)')
    parser.add_argument('--plain', action='store_true', help='Print sequential logs without a live dashboard')
    args = parser.parse_args(argv)
    # Explicit tape selection without excerpt input is also a whole-tape denoise request.
    args.batch_denoise = args.all_tapes or args.all_channels or (
        args.tape is not None and args.channel is None and args.input is None
        and (args.stages is None or args.stages.strip() in {'3', '03'}))
    args.tape = args.tape or 'A15_T510073'
    if args.denoise_workers < 0:
        parser.error('--denoise-workers must be nonnegative')
    try:
        requested = args.stages or ('03' if args.batch_denoise else
                                    '03,05a,05b,05c' if args.channel is not None else '03,05b')
        selected = {value.strip().lower() if value.strip().lower() in {'05a','05b','05c'} else f'{int(value):02d}' for value in requested.split(',')}
    except ValueError:
        parser.error('--stages must contain 01,03,05a,05b,05c')
    if not selected or not selected <= {s[0] for s in STAGES}:
        parser.error('--stages must contain 01,03,05a,05b,05c')
    args.batch_retime = args.batch_denoise and selected == {'05c'}
    if args.batch_retime:
        args.batch_denoise = False
        if args.input or args.quindar_plan or args.retime_candidates or args.retime_smoothing_seconds or args.experimental_single_channel:
            parser.error('Batch 05c uses one saved plan per tape; omit file-specific overrides')
    if args.skip_deflutter and {'05b', '05c'} <= selected:
        parser.error('Cannot retime a skipped deflutter stage; run 05c alone on existing defluttered audio')
    if Path(args.tape).name != args.tape or args.tape in {'.', '..'}:
        parser.error('--tape must be a folder name')
    if args.batch_denoise:
        if selected != {'03'}:
            parser.error('Batch mode supports denoising only; use --stages 03')
        if args.input or args.flutter_config or args.flutter_targets or args.flutter_plan or args.retime_candidates:
            parser.error('Batch denoising uses original tapes under --concatenated; omit excerpt/deflutter inputs')
    if args.channel is not None:
        if not 1 <= args.channel <= 30:
            parser.error('--channel must be 1-30')
        if args.flutter_config or args.flutter_targets or args.input:
            parser.error('Whole-channel mode uses stored stage folders; use --concatenated for original input')
        if args.flutter_plan_start != 0 or args.flutter_plan_duration is not None:
            parser.error('Whole-channel mode requires a whole-tape plan; omit bounded planning arguments')
    if args.flutter_config and (args.flutter_plan or args.flutter_targets):
        parser.error('Choose legacy --flutter-config or saved-plan rendering, not both')
    if args.action == 'run':
        if not args.batch_denoise and args.channel is None and selected & {'03', '05b'} and (args.input is None or not args.input.is_dir()):
            parser.error('Audio stages require --input pointing to a speech excerpt folder')
        if not args.batch_retime and args.channel is None and '05c' in selected and '05b' not in selected and (args.input is None or not args.input.exists()):
            parser.error('Standalone 05c requires --input pointing to defluttered audio')
        if args.input and (args.input.resolve() == args.output.resolve() or args.input.resolve() in args.output.resolve().parents):
            parser.error('--output must be outside --input')
    try:
        validate_settings(args.target_dbfs, args.max_boost_db)
    except ValueError as exc:
        parser.error(str(exc))
    return args, selected


def make_denoise_batch(args):
    """Inventory originals once and queue independent, reusable whole-channel jobs."""
    from flutter_planning import inventory
    from channel_review import completed_denoised

    if args.all_tapes:
        if not args.concatenated.is_dir():
            raise ValueError(f'Canonical tape root does not exist: {args.concatenated}')
        folders = sorted(p for p in args.concatenated.glob('A15_T*') if p.is_dir())
    else:
        folders = [args.concatenated / args.tape]
    if not folders:
        raise ValueError('No canonical tape folders found under --concatenated')
    jobs = []
    for folder in folders:
        try:
            inv = inventory(folder)
        except (ValueError, OSError, RuntimeError) as error:
            jobs.append(dict(number='03', name=f'{folder.name} inventory', tape=folder.name,
                             channel=None, script='03_denoise.py', description=str(error),
                             command=[], status='failed', elapsed=0.0, returncode=2,
                             error=str(error)))
            continue
        for row in inv['channels']:
            channel = row['channel']
            if args.channel is not None and channel != args.channel:
                continue
            output = tape_directory(inv['tape'], args.denoised_root) / f'CH{channel:02d}'
            status = 'skipped existing' if completed_denoised(row, output, 0, row['frames']) else 'pending'
            jobs.append(dict(number='03', name=f"{inv['tape']} CH{channel:02d}",
                tape=inv['tape'], channel=channel, script='03_denoise.py',
                description='Denoise whole channel',
                command=[sys.executable, '-u', str(SCRIPTS/'03_denoise.py'),
                         '--input', str(folder), '--output', str(output),
                         '--channel', str(channel), '--workers', str(args.denoise_workers)],
                log=f"03_{inv['tape']}_CH{channel:02d}.log",
                status=status, elapsed=0.0, returncode=0 if status == 'skipped existing' else None))
    if not jobs:
        raise ValueError('No available channels match the selection')
    return jobs


def make_jobs(args, selected, run_dir):
    if args.batch_retime:
        folders = sorted(args.defluttered_root.glob('A15_T*')) if args.all_tapes else [args.defluttered_root/args.tape]
        folders = [p for p in folders if p.is_dir()]
        if not folders:
            raise ValueError('No defluttered tapes are available')
        jobs = []
        for folder in folders:
            if not list(folder.glob('CH*/deflutter.wav')):
                continue
            command = [sys.executable, '-u', str(SCRIPTS/'05c_retime.py'), '--tape', folder.name,
                       '--defluttered-root', str(args.defluttered_root), '--retimed-root', str(args.retimed_root),
                       '--quindar-analysis-root', str(args.quindar_analysis_root),
                       '--target-dbfs', str(args.target_dbfs), '--max-boost-db', str(args.max_boost_db)]
            command += ['--channel', str(args.channel)] if args.channel else ['--all-channels']
            jobs.append(dict(number='05c', name=folder.name, script='05c_retime.py',
                             description='Apply one frozen tape curve to available defluttered channels', command=command,
                             status='pending', elapsed=0., returncode=None, log=f'05c_{folder.name}.log'))
        if not jobs:
            raise ValueError('No defluttered channels are available')
        return jobs
    if args.batch_denoise:
        return make_denoise_batch(args)
    current = args.input
    jobs = []
    for number, name, script, description in STAGES:
        status = 'pending' if number in selected else 'not selected'
        if number == '05b' and number in selected and args.skip_deflutter:
            status = 'skipped explicitly'
        command = [sys.executable, '-u', str(SCRIPTS / script)]
        output = run_dir / f'{number}_{name.lower().replace(" ", "_").replace("/", "_")}'
        if number == '01':
            command += ['--input', str(args.delivery), '--output', str(args.concatenated), '--min-age', str(args.min_age), '--strict']
        elif number == '05a':
            output = planning_directory(args.tape, args.flutter_planning_root,
                                        args.flutter_plan_start, args.flutter_plan_duration)
            command += ['--input', str(args.concatenated / args.tape), '--output', str(output),
                        '--region-seconds', str(args.flutter_region_seconds),
                        '--start', str(args.flutter_plan_start),
                        '--workers', str(args.flutter_plan_workers)]
            if args.flutter_plan_duration is not None:
                command += ['--duration', str(args.flutter_plan_duration)]
        elif number == '03' and args.channel is not None:
            output = tape_directory(args.tape,args.denoised_root)/f'CH{args.channel:02d}'
            name, description = 'Denoise channel', 'Cache tone/hum removal and 3 dB broadband denoising'
            command = [sys.executable,'-u',str(SCRIPTS/script),'--input',str(args.concatenated/args.tape),
                       '--output',str(output),'--channel',str(args.channel),'--workers',str(args.denoise_workers)]
            if number in selected:
                from flutter_planning import inventory
                from channel_review import completed_denoised

                inv = inventory(args.concatenated/args.tape)
                row = next((r for r in inv['channels'] if r['channel'] == args.channel), None)
                if row is None:
                    raise ValueError('Requested channel is unavailable')
                if completed_denoised(row, output, 0, row['frames']):
                    status = 'skipped existing'
        elif number == '05b' and args.channel is not None:
            output = tape_directory(args.tape,args.defluttered_root)/f'CH{args.channel:02d}'
            plan = args.flutter_plan or planning_directory(args.tape,args.flutter_planning_root)/'plan.json'
            denoised = tape_directory(args.tape,args.denoised_root)/f'CH{args.channel:02d}'
            command += ['--tape',args.tape,'--channel',str(args.channel),'--output',str(output),
                        '--flutter-plan',str(plan),'--denoised-input',str(denoised)]
            if args.experimental_transfer:
                command += ['--experimental-transfer']
        elif number == '05c':
            command += ['--quindar-analysis-root', str(args.quindar_analysis_root)]
            if args.quindar_plan:
                command += ['--quindar-plan', str(args.quindar_plan)]
            if args.experimental_single_channel:
                command += ['--experimental-single-channel']
            if args.channel is not None:
                output = tape_directory(args.tape, args.retimed_root)/f'CH{args.channel:02d}'
                command += ['--tape', args.tape, '--channel', str(args.channel),
                            '--defluttered-root', str(args.defluttered_root), '--output', str(output)]
            else:
                command += ['--input', str(current or '<defluttered-audio>'), '--output', str(output)]
            if args.retime_candidates is not None:
                command += ['--candidates', str(args.retime_candidates)]
            if args.retime_smoothing_seconds is not None:
                command += ['--smoothing-seconds', str(args.retime_smoothing_seconds)]
        elif number in {'03', '05b'}:
            command += ['--input', str(current or '<speech-excerpt-folder>'), '--output', str(output)]
            if number == '05b' and args.flutter_config is not None:
                command += ['--flutter-config', str(args.flutter_config.resolve())]
            elif number == '05b':
                folder = planning_directory(args.tape, args.flutter_planning_root,
                    args.flutter_plan_start if '05a' in selected else 0,
                    args.flutter_plan_duration if '05a' in selected else None)
                plan = args.flutter_plan or folder/'plan.json'
                command += ['--flutter-plan', str(plan), '--tape', args.tape]
                if args.flutter_targets is not None:
                    command += ['--flutter-targets', str(args.flutter_targets.resolve())]
            if number in selected and status != 'skipped explicitly':
                current = output
        if number in {'05b', '05c'}:
            command += ['--target-dbfs',str(args.target_dbfs),'--max-boost-db',str(args.max_boost_db)]
        jobs.append(dict(number=number, name=name, script=script, description=description,
                         command=command, status=status, elapsed=0.0,
                         returncode=0 if status == 'skipped existing' else None))
    return jobs


def dashboard(jobs, tail, location):
    table = Table(expand=True)
    for column in ['Stage', 'Script / activity', 'Status', 'Time']:
        table.add_column(column)
    visible = jobs
    if jobs and 'tape' in jobs[0]:
        completed = {'done', 'failed', 'cancelled', 'skipped existing'}
        finished = sum(j['channel'] is not None and j['status'] in completed for j in jobs)
        total = sum(j['channel'] is not None for j in jobs)
        failed = sum(j['channel'] is not None and j['status'] == 'failed' for j in jobs)
        invalid = sum(j['channel'] is None for j in jobs)
        skipped = sum(j['status'] == 'skipped existing' for j in jobs)
        table.caption = f'{finished}/{total} channels finished; {skipped} skipped existing; {failed} failed; {invalid} invalid tapes'
        visible = ([j for j in jobs if j['status'] in completed][-5:]
                   + [j for j in jobs if j['status'] == 'running']
                   + [j for j in jobs if j['status'] in {'pending', 'not run'}][:5])
    for job in visible:
        style = {'running': 'cyan', 'done': 'green', 'failed': 'red', 'blocked': 'yellow'}.get(job['status'], 'dim')
        table.add_row(f'{job["number"]} {job["name"]}', Text(job['script'] + '\n' + job['description']),
                      Text(job['status'], style=style), f'{job["elapsed"]:.1f}s')
    return Group(Panel(table, title='Apollo 15 â€¢ audio pipeline'),
                 Panel(Text('\n'.join(tail) or 'Waiting to start'), title='Current script output'),
                 Text(f'Run: {location}'))


def execute(job, jobs, tail, run_dir, live, console):
    started = time.monotonic()
    job['status'] = 'running'
    messages = queue.Queue()
    process = subprocess.Popen(job['command'], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               text=True, encoding='utf-8', errors='replace',
                               creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == 'nt' else 0,
                               env={**os.environ, 'PYTHONIOENCODING': 'utf-8'})

    def reader():
        for line in process.stdout:
            messages.put(line.rstrip())
        messages.put(None)

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    try:
        with (run_dir / job.get('log', f'{job["number"]}.log')).open('w', encoding='utf-8') as log:
            while True:
                job['elapsed'] = time.monotonic() - started
                try:
                    line = messages.get(timeout=0.1)
                    if line is None:
                        break
                    log.write(line + '\n')
                    log.flush()
                    tail.append(line)
                    if live is None:
                        console.print(Text(line))
                except queue.Empty:
                    pass
                if live:
                    live.update(dashboard(jobs, tail, run_dir))
        job['returncode'] = process.wait()
        job['status'] = 'done' if process.returncode == 0 else ('blocked' if job['number'] in {'05b', '05c'} else 'failed')
        return process.returncode
    finally:
        if process.poll() is None:
            if os.name == 'nt':
                # Keep Ctrl+C in the runner, then stop the entire stage tree.
                # Terminating just its parent strands ProcessPool workers.
                subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               check=False)
            else:
                process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        thread.join(timeout=2)
        process.stdout.close()
        job['elapsed'] = time.monotonic() - started


def main(argv=None):
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    args, selected = parse_args(argv)
    console = Console()
    run_dir = args.output.resolve() / (datetime.now().strftime('%Y%m%d_%H%M%S') + '_' + uuid.uuid4().hex[:8])
    try:
        jobs = make_jobs(args, selected, run_dir)
    except (ValueError, OSError, RuntimeError) as error:
        console.print(Text(f'Cannot prepare pipeline: {error}'))
        return 2
    tail = deque(maxlen=10)
    invalid = [job for job in jobs if job['status'] == 'failed']
    for job in invalid:
        console.print(Text(f'{job["name"]}: {job["error"]}'))
    if args.action == 'plan':
        console.print(dashboard(jobs, tail, run_dir))
        for job in jobs:
            if job['status'] == 'pending':
                console.print(Text(subprocess.list2cmdline(job['command'])))
        console.print('Commands only; no outputs written.' if args.batch_denoise else
                      'Commands only. Whole-channel order: 03 denoise, 05a plan on originals, 05b deflutter, 05c Quindar retime.')
        return 2 if invalid else 0
    run_dir.mkdir(parents=True)
    live = None if args.plain or not console.is_terminal else Live(console=console, refresh_per_second=5)
    result = 2 if invalid else 0
    try:
        if live:
            live.start()
        for job in jobs:
            if job['status'] != 'pending':
                continue
            tail.clear()
            console.print(Text(f'{job["number"]} {job["name"]}: {job["description"]}'))
            try:
                code = execute(job, jobs, tail, run_dir, live, console)
            except Exception as error:
                if not (args.batch_denoise or args.batch_retime):
                    raise
                job.update(status='failed', returncode=1, error=str(error))
                console.print(Text(f'{job["name"]}: {error}'))
                code = 1
            if code:
                result = result or code
            # Persist each completed job so an overnight interruption leaves a record.
            (run_dir / 'run.json').write_text(json.dumps(dict(returncode=result, stages=jobs), indent=2), encoding='utf-8')
            if code and not (args.batch_denoise or args.batch_retime):
                break
    except KeyboardInterrupt:
        job['status'] = 'cancelled'
        result = 130
    except Exception as error:
        job['status'] = 'failed'
        tail.append(str(error))
        result = 1
    finally:
        for pending in jobs:
            if pending['status'] == 'pending':
                pending['status'] = 'not run'
        (run_dir / 'run.json').write_text(json.dumps(dict(returncode=result, stages=jobs), indent=2), encoding='utf-8')
        if live:
            live.update(dashboard(jobs, tail, run_dir))
            live.stop()
        else:
            console.print(dashboard(jobs, tail, run_dir))
        if args.batch_denoise:
            for failed in jobs:
                if failed['status'] == 'failed':
                    detail = failed.get('error') or str(run_dir/failed['log'])
                    console.print(Text(f'Failed: {failed["name"]}: {detail}'))
    return result


if __name__ == '__main__':
    sys.exit(main())
