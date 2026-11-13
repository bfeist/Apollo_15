"""Numbered Apollo 15 audio pipeline with a Rich terminal dashboard."""

# Resolve shared modules from the stage folders.
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent))
from _bootstrap import install as _install
_install()

import argparse
from collections import deque
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
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
from _bootstrap import command as script_command, source_path

from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from normalization import DEFAULT_TARGET_DBFS, DEFAULT_MAX_BOOST_DB, validate_settings
from audio_processing import DEFAULT_BROADBAND_CAP_DB
from denoise_archive import CANONICAL, ORIGINALS, root_marker
from flutter_plan_store import DEFAULT_DENOISED_ROOT, planning_directory, tape_directory
from shared_tape import DEFAULT_PLANNING_ROOT, DEFAULT_DEFLUTTERED_ROOT, DEFAULT_EVIDENCE_ROOT
from quindar_retime import DEFAULT_RETIMED_ROOT
from quindar_survey import DEFAULT_ANALYSIS_ROOT
from speech_normalization import DEFAULT_NORMALIZED_ROOT, Settings as SpeechSettings
from crosstalk_stage import DEFAULT_CROSSTALK_ROOT, DEFAULT_EVIDENCE_ROOT as DEFAULT_CROSSTALK_EVIDENCE_ROOT

STAGES = [
    ('01', 'Concatenate', '01_concatenate.py', 'Verify fragment order and join channels'),
    ('03', 'Denoise', '03_denoise.py',
     f'Remove tones and residual hum, then reduce broadband noise by up to {DEFAULT_BROADBAND_CAP_DB} dB'),
    ('04', 'Crosstalk', '04_crosstalk.py', 'Subtract evidenced weaker repeats; preserve gain and source timing'),
    ('05a', 'Plan deflutter', '05a_plan_deflutter.py', 'Compile one tape map from saved tone evidence on all channels'),
    ('05b', 'Deflutter', '05b_deflutter.py', 'Apply the shared tape map to stage-04 audio at its existing gain'),
    ('06', 'Retime', '06_retime.py', 'Apply a smoothed Quindar anchor curve to defluttered audio'),
    ('07', 'Normalize', '07_normalize.py', 'Reach the speech target with one constant gain; bypass noise-only channels'),
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
    tapes.add_argument('--all-tapes', action='store_true', help='Process all tapes for stage 03, 04, 05a, 05b, 06, or 07')
    parser.add_argument('--exclude-tape', action='append', default=[], metavar='TAPE',
                        help='Skip a tape in an all-tapes batch; may be repeated')
    channels = parser.add_mutually_exclusive_group()
    channels.add_argument('--channel', type=int, help='Whole-channel mode; defaults to shared deflutter from completed stage-04 audio')
    channels.add_argument('--all-channels', action='store_true', help='Process all available numbered channels, including CH01')
    parser.add_argument('--denoised-root', type=Path, default=DEFAULT_DENOISED_ROOT)
    parser.add_argument('--crosstalk-root', type=Path, default=DEFAULT_CROSSTALK_ROOT)
    parser.add_argument('--crosstalk-evidence-root', type=Path, default=DEFAULT_CROSSTALK_EVIDENCE_ROOT)
    parser.add_argument('--skip-crosstalk', action='store_true', help='Explicitly use v2 denoised audio for a stage-05b comparison')
    parser.add_argument('--denoise-version', choices=['v1', 'v2'], default='v1')
    parser.add_argument('--denoise-workers', type=int,
                        help='V1 workers per channel (default 14) or v2 concurrent channel workers (default 3)')
    parser.add_argument('--denoise-tapes', type=int, default=3, help='Maximum active tapes for v2')
    parser.add_argument('--defluttered-root', type=Path, default=DEFAULT_DEFLUTTERED_ROOT)
    parser.add_argument('--retimed-root', type=Path, default=DEFAULT_RETIMED_ROOT)
    parser.add_argument('--normalized-root', type=Path, default=DEFAULT_NORMALIZED_ROOT)
    parser.add_argument('--normalize-from', choices=['defluttered', 'retimed'], default='defluttered',
                        help='Stage 07 input when no preceding audio stage is selected')
    parser.add_argument('--speech-target-dbfs', type=float, default=-23., help='Stage 07 active speech RMS target; not LUFS')
    parser.add_argument('--speech-max-boost-db', type=float, help='Stage 07 optional boost limit; disabled by default')
    parser.add_argument('--speech-max-noise-dbfs', type=float, help='Stage 07 optional noise ceiling; disabled by default')
    parser.add_argument('--quindar-analysis-root', type=Path, default=DEFAULT_ANALYSIS_ROOT)
    parser.add_argument('--quindar-plan', type=Path, help='Stage 06 frozen shared tape plan')
    parser.add_argument('--experimental-single-channel', action='store_true', help='Allow the original per-file Quindar assessment workflow')
    parser.add_argument('--retime-candidates', type=Path, help='Stage 06: full candidate inventory for the exact defluttered input')
    parser.add_argument('--retime-smoothing-seconds', type=float, help='Stage 06: smoothing kernel sigma; default chooses by block validation')
    parser.add_argument('--min-age', type=float, default=60)
    parser.add_argument('--flutter-planning-root', '--planning-root', type=Path,
                        default=DEFAULT_PLANNING_ROOT, help='Shared tape maps')
    parser.add_argument('--flutter-evidence-root', type=Path, default=DEFAULT_EVIDENCE_ROOT,
                        help='Completed all-channel tone survey/planning bundles to reuse')
    parser.add_argument('--flutter-plan-stop-on-error', action='store_true',
                        help='Stop starting tapes after a planning failure; running tapes finish')
    parser.add_argument('--flutter-plan', type=Path, help='Stage 05b shared tape plan override')
    parser.add_argument('--flutter-plan-workers', type=int, default=4,
                        help='Concurrent tape planners for batch stage 05a (default: 4; 1 is serial)')
    parser.add_argument('--flutter-plan-start', type=float, default=0, help='Stage 05a bounded audit start; default whole tape')
    parser.add_argument('--flutter-plan-duration', type=float, help='Stage 05a optional bounded audit duration')
    parser.add_argument('--skip-deflutter', action='store_true', help='Skip stage 05b correction')
    parser.add_argument('--target-dbfs', type=float, default=DEFAULT_TARGET_DBFS, help='Stage 06 sample-peak target (default: -3 dBFS); 05b retains v2 gain')
    parser.add_argument('--max-boost-db', type=float, default=DEFAULT_MAX_BOOST_DB, help='Stage 06 maximum boost (default: 12 dB)')
    parser.add_argument('--plain', action='store_true', help='Print sequential logs without a live dashboard')
    args = parser.parse_args(argv)
    if args.denoise_workers is None:
        args.denoise_workers = 3 if args.denoise_version == 'v2' else 14
    # Explicit tape selection without excerpt input is also a whole-tape denoise request.
    args.batch = args.all_tapes or args.all_channels or (
        args.tape is not None and args.channel is None and args.input is None
        and (args.stages is None or args.stages.strip() in {'3', '03'}))
    args.tape = args.tape or 'A15_T510073'
    if args.exclude_tape and (not args.all_tapes or any(
            Path(tape).name != tape or tape in {'.', '..'} for tape in args.exclude_tape)):
        parser.error('--exclude-tape requires --all-tapes and simple tape folder names')
    if args.denoise_workers < 0:
        parser.error('--denoise-workers must be nonnegative')
    if args.flutter_plan_workers < 1:
        parser.error('--flutter-plan-workers must be positive')
    try:
        requested = args.stages or ('03' if args.batch else
                                    '05b' if args.channel is not None else '03')
        requested = requested.lower().replace('05c', '06')  # old retime command alias
        selected = {value.strip() if value.strip() in {'05a','05b'} else f'{int(value):02d}' for value in requested.split(',')}
    except ValueError:
        parser.error('--stages must contain 01,03,04,05a,05b,06,07')
    if not selected or not selected <= {s[0] for s in STAGES}:
        parser.error('--stages must contain 01,03,04,05a,05b,06,07')
    if args.batch and selected not in ({'03'}, {'04'}, {'05a'}, {'05b'}, {'06'}, {'07'}):
        parser.error('Batch mode supports one stage at a time; use --stages 03, 04, 05a, 05b, 06, or 07')
    args.batch_crosstalk = args.batch and selected == {'04'}
    if args.skip_crosstalk and ('04' in selected or '05b' not in selected):
        parser.error('--skip-crosstalk requires stage 05b without stage 04')
    if '04' in selected and not args.batch and args.channel is None:
        parser.error('Stage 04 requires --channel, --all-channels, or --all-tapes')
    if '04' in selected and selected & {'06', '07'} and ('05b' not in selected or args.skip_deflutter):
        parser.error('Run stage 05b between stage 04 and retiming/normalization')
    if args.batch_crosstalk and args.input:
        parser.error('Batch 04 uses completed v2 channels under --denoised-root; omit --input')
    args.batch_normalize = args.batch and selected == {'07'}
    if args.batch_normalize and args.input:
        parser.error('Batch 07 uses --normalize-from and stage roots; omit --input')
    args.batch_denoise = args.batch and selected == {'03'}
    if args.denoise_version == 'v2':
        if (selected != {'03'} or not (args.all_tapes or args.channel is not None)
                or args.concatenated.absolute() != ORIGINALS or args.denoised_root.absolute() != CANONICAL
                or args.exclude_tape or args.denoise_workers < 1 or args.denoise_tapes < 1
                or (args.all_tapes and args.channel is not None)):
            parser.error('V2 requires stage 03 alone, the frozen roots, and all tapes or one tape/channel')
    args.batch_flutter = args.batch and bool(selected & {'05a', '05b'})
    if args.flutter_plan_stop_on_error and not (args.batch and selected == {'05a'}):
        parser.error('--flutter-plan-stop-on-error requires batch stage 05a')
    args.batch_retime = args.batch and selected == {'06'}
    if args.batch_retime:
        if args.input or args.quindar_plan or args.retime_candidates or args.retime_smoothing_seconds or args.experimental_single_channel:
            parser.error('Batch 06 uses one saved plan per tape; omit file-specific overrides')
    if args.skip_deflutter and {'05b', '06'} <= selected:
        parser.error('Cannot retime a skipped deflutter stage; run 06 alone on existing defluttered audio')
    if '05b' in selected and not args.skip_deflutter and not args.batch and args.channel is None:
        parser.error('Stage 05b requires --channel, --all-channels, or --all-tapes; use render_channel_review.py for listening pilots')
    if Path(args.tape).name != args.tape or args.tape in {'.', '..'}:
        parser.error('--tape must be a folder name')
    if args.batch_denoise:
        if args.input or args.flutter_plan or args.retime_candidates:
            parser.error('Batch denoising uses original tapes under --concatenated; omit excerpt/deflutter inputs')
    if args.batch_flutter:
        if args.input or args.flutter_plan:
            parser.error('Batch flutter stages use original tapes and one stored plan per tape; omit excerpt/plan overrides')
        if '05b' in selected and (args.flutter_plan_start != 0 or args.flutter_plan_duration is not None):
            parser.error('Batch 05b requires whole-tape plans; omit bounded planning arguments')
    if args.channel is not None:
        if not 1 <= args.channel <= 30:
            parser.error('--channel must be 1-30')
        if args.input:
            parser.error('Whole-channel mode uses stored stage folders; use --concatenated for original input')
        if args.flutter_plan_start != 0 or args.flutter_plan_duration is not None:
            parser.error('Whole-channel mode requires a whole-tape plan; omit bounded planning arguments')
    if args.action == 'run':
        if '07' in selected and not args.batch and args.channel is None and not selected & {'05b', '06'}:
            if args.input is None or not args.input.exists():
                parser.error('Standalone 07 requires --input or a tape/channel selection')
        if not args.batch and args.channel is None and selected & {'03', '05b'} and (args.input is None or not args.input.is_dir()):
            parser.error('Audio stages require --input pointing to a speech excerpt folder')
        if not args.batch_retime and args.channel is None and '06' in selected and '05b' not in selected and (args.input is None or not args.input.exists()):
            parser.error('Standalone 06 requires --input pointing to defluttered audio')
        if args.input and (args.input.resolve() == args.output.resolve() or args.input.resolve() in args.output.resolve().parents):
            parser.error('--output must be outside --input')
    try:
        validate_settings(args.target_dbfs, args.max_boost_db)
        SpeechSettings(target_speech_dbfs=args.speech_target_dbfs,
                       max_boost_db=args.speech_max_boost_db,
                       max_noise_dbfs=args.speech_max_noise_dbfs).validate()
    except ValueError as exc:
        parser.error(str(exc))
    return args, selected


def canonical_tapes(args):
    if args.all_tapes:
        if not args.concatenated.is_dir():
            raise ValueError(f'Canonical tape root does not exist: {args.concatenated}')
        folders = sorted(p for p in args.concatenated.glob('A15_T*')
                         if p.is_dir() and p.name not in args.exclude_tape)
    else:
        folders = [args.concatenated / args.tape]
    if not folders:
        raise ValueError('No canonical tape folders found under --concatenated')
    return folders


def make_denoise_batch(args):
    """Inventory originals once and queue independent, reusable whole-channel jobs."""
    from flutter_planning import inventory
    from channel_review import completed_denoised

    jobs = []
    for folder in canonical_tapes(args):
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
            status = 'skipped existing' if completed_denoised(row, output, 0, row['frames'], version='v1') else 'pending'
            jobs.append(dict(number='03', name=f"{inv['tape']} CH{channel:02d}",
                tape=inv['tape'], channel=channel, script='03_denoise.py',
                description='Denoise whole channel',
                command=[sys.executable, '-u', str(source_path('03_denoise.py')),
                         '--input', str(folder), '--output', str(output),
                         '--channel', str(channel), '--workers', str(args.denoise_workers)],
                log=f"03_{inv['tape']}_CH{channel:02d}.log",
                status=status, elapsed=0.0, returncode=0 if status == 'skipped existing' else None))
    if not jobs:
        raise ValueError('No available channels match the selection')
    return jobs


def make_flutter_batch(args, selected, run_dir):
    """Compile each tape once, or apply its saved shared map to numbered channels."""
    from flutter_planning import inventory

    jobs = []
    for folder in canonical_tapes(args):
        tape_args = argparse.Namespace(**vars(args))
        tape_args.tape = folder.name
        if selected == {'05a'}:
            channels = [None]
        else:
            try:
                inv = inventory(folder)
            except (ValueError, OSError, RuntimeError) as error:
                number = '04' if selected == {'04'} else '05b'
                script = '04_crosstalk.py' if number == '04' else '05b_deflutter.py'
                jobs.append(dict(number=number, name=f'{folder.name} inventory', tape=folder.name,
                                 channel=None, script=script, description=str(error),
                                 command=[], status='failed', elapsed=0., returncode=2, error=str(error)))
                continue
            channels = [r['channel'] for r in inv['channels']
                        if args.channel is None or r['channel'] == args.channel]
        for channel in channels:
            tape_args.channel = channel
            for job in make_tape_jobs(tape_args, selected, run_dir):
                if job['number'] not in selected:
                    continue
                suffix = '' if channel is None else f' CH{channel:02d}'
                job.update(name=folder.name+suffix, tape=folder.name, channel=channel,
                           log=f'{job["number"]}_{folder.name}{suffix.replace(" ", "_")}.log')
                jobs.append(job)
    if not jobs:
        raise ValueError('No available numbered channels match the selection')
    return jobs


def make_jobs(args, selected, run_dir):
    if args.batch_normalize:
        root = args.retimed_root if args.normalize_from == 'retimed' else args.defluttered_root
        folders = sorted(root.glob('A15_T*')) if args.all_tapes else [root/args.tape]
        jobs = []
        for folder in folders:
            if folder.name in args.exclude_tape:
                continue
            sources = sorted(folder.glob('CH[0-9][0-9]')) if args.channel is None else [folder/f'CH{args.channel:02d}']
            for source in sources:
                channel = int(source.name[2:])
                options = argparse.Namespace(**vars(args))
                options.channel, options.tape = channel, folder.name
                job = next(j for j in make_tape_jobs(options, selected, run_dir) if j['number'] == '07')
                job.update(name=f'{folder.name} CH{channel:02d}', tape=folder.name, channel=channel,
                           log=f'07_{folder.name}_CH{channel:02d}.log')
                jobs.append(job)
        if not jobs:
            raise ValueError('No channels are available for speech normalization')
        return jobs
    if args.batch_retime:
        folders = sorted(args.defluttered_root.glob('A15_T*')) if args.all_tapes else [args.defluttered_root/args.tape]
        folders = [p for p in folders if p.is_dir() and p.name not in args.exclude_tape]
        if not folders:
            raise ValueError('No defluttered tapes are available')
        jobs = []
        for folder in folders:
            if not list(folder.glob('CH*/deflutter.wav')):
                continue
            command = [sys.executable, '-u', str(source_path('06_retime.py')), '--tape', folder.name,
                       '--defluttered-root', str(args.defluttered_root), '--retimed-root', str(args.retimed_root),
                       '--quindar-analysis-root', str(args.quindar_analysis_root),
                       '--target-dbfs', str(args.target_dbfs), '--max-boost-db', str(args.max_boost_db)]
            command += ['--channel', str(args.channel)] if args.channel else ['--all-channels']
            jobs.append(dict(number='06', name=folder.name, script='06_retime.py',
                             description='Apply one frozen tape curve to available defluttered channels', command=command,
                             status='pending', elapsed=0., returncode=None, log=f'06_{folder.name}.log'))
        if not jobs:
            raise ValueError('No defluttered channels are available')
        return jobs
    if args.batch_denoise:
        return make_denoise_batch(args)
    if args.batch_flutter or args.batch_crosstalk:
        return make_flutter_batch(args, selected, run_dir)
    return make_tape_jobs(args, selected, run_dir)


def make_tape_jobs(args, selected, run_dir):
    current = args.input
    jobs = []
    for number, name, script, description in STAGES:
        status = 'pending' if number in selected else 'not selected'
        if number == '05b' and number in selected and args.skip_deflutter:
            status = 'skipped explicitly'
        command = [sys.executable, '-u', str(source_path(script))]
        output = run_dir / f'{number}_{name.lower().replace(" ", "_").replace("/", "_")}'
        if number == '01':
            command += ['--input', str(args.delivery), '--output', str(args.concatenated), '--min-age', str(args.min_age), '--strict']
        elif number == '05a':
            output = planning_directory(args.tape, args.flutter_planning_root,
                                        args.flutter_plan_start, args.flutter_plan_duration)
            command += ['--input', str(args.concatenated / args.tape), '--output', str(output),
                        '--start', str(args.flutter_plan_start),
                        '--evidence-root', str(args.flutter_evidence_root)]
            if args.flutter_plan_duration is not None:
                command += ['--duration', str(args.flutter_plan_duration)]
        elif number == '03' and args.channel is not None:
            output = tape_directory(args.tape,args.denoised_root)/f'CH{args.channel:02d}'
            name, description = 'Denoise channel', 'Cache tone/hum removal and 3 dB broadband denoising'
            command = [sys.executable,'-u',str(source_path(script)),'--input',str(args.concatenated/args.tape),
                       '--output',str(output),'--channel',str(args.channel),'--workers',str(args.denoise_workers)]
            if number in selected:
                from flutter_planning import inventory
                from channel_review import completed_denoised

                inv = inventory(args.concatenated/args.tape)
                row = next((r for r in inv['channels'] if r['channel'] == args.channel), None)
                if row is None:
                    raise ValueError('Requested channel is unavailable')
                if completed_denoised(row, output, 0, row['frames'], version='v1'):
                    status = 'skipped existing'
        elif number == '04' and args.channel is not None:
            output = args.crosstalk_root/args.tape/f'CH{args.channel:02d}'
            command += ['--tape', args.tape, '--channel', str(args.channel), '--output', str(output),
                        '--concatenated', str(args.concatenated), '--denoised-root', str(args.denoised_root),
                        '--evidence-root', str(args.crosstalk_evidence_root)]
        elif number == '05b' and args.channel is not None:
            output = tape_directory(args.tape,args.defluttered_root)/f'CH{args.channel:02d}'
            plan = args.flutter_plan or planning_directory(args.tape,args.flutter_planning_root)/'plan.json'
            root = args.denoised_root if args.skip_crosstalk else args.crosstalk_root
            source = tape_directory(args.tape,root)/f'CH{args.channel:02d}'
            command += ['--tape',args.tape,'--channel',str(args.channel),'--output',str(output),
                        '--flutter-plan',str(plan),
                        '--denoised-input' if args.skip_crosstalk else '--crosstalk-input',str(source)]
        elif number == '06':
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
        elif number == '07':
            if args.channel is not None:
                root = args.retimed_root if '06' in selected or args.normalize_from == 'retimed' else args.defluttered_root
                output = args.normalized_root/args.tape/f'CH{args.channel:02d}'
                command += ['--tape', args.tape, '--channel', str(args.channel),
                            '--input-root', str(root), '--output', str(output)]
            else:
                command += ['--input', str(current or '<defluttered-or-retimed-audio>'), '--output', str(output)]
            command += ['--target-speech-dbfs', str(args.speech_target_dbfs)]
            if args.speech_max_boost_db is not None:
                command += ['--max-boost-db', str(args.speech_max_boost_db)]
            if args.speech_max_noise_dbfs is not None:
                command += ['--max-noise-dbfs', str(args.speech_max_noise_dbfs)]
        elif number == '03':
            command += ['--input', str(current or '<speech-excerpt-folder>'), '--output', str(output)]
            if number in selected and status != 'skipped explicitly':
                current = output
        if number == '06':
            command += ['--target-dbfs',str(args.target_dbfs),'--max-boost-db',str(args.max_boost_db)]
        if number in selected and number in {'05b', '06', '07'} and status != 'skipped explicitly':
            current = output
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
        completed = {'done', 'failed', 'blocked', 'cancelled', 'skipped existing', 'skipped explicitly'}
        planning = all(j['number'] == '05a' for j in jobs)
        counted = jobs if planning else [j for j in jobs if j['channel'] is not None]
        finished = sum(j['status'] in completed for j in counted)
        total = len(counted)
        failed = sum(j['status'] in {'failed', 'blocked'} for j in counted)
        invalid = len(jobs)-total
        skipped = sum(j['status'] == 'skipped existing' for j in jobs)
        unit = 'tapes' if planning else 'channels'
        table.caption = f'{finished}/{total} {unit} finished; {skipped} skipped existing; {failed} failed; {invalid} invalid tapes'
        visible = ([j for j in jobs if j['status'] in completed][-5:]
                   + [j for j in jobs if j['status'] == 'running']
                   + [j for j in jobs if j['status'] in {'pending', 'not run'}][:5])
    for job in visible:
        style = {'running': 'cyan', 'done': 'green', 'failed': 'red', 'blocked': 'yellow'}.get(job['status'], 'dim')
        table.add_row(f'{job["number"]} {job["name"]}', Text(job['script'] + '\n' + job['description']),
                      Text(job['status'], style=style), f'{job["elapsed"]:.1f}s')
    return Group(Panel(table, title='Apollo 15 audio pipeline'),
                 Panel(Text('\n'.join(tail) or 'Waiting to start'), title='Current script output'),
                 Text(f'Run: {location}'))


def execute(job, jobs, tail, run_dir, live, console, stop_event=None, report_queue=None):
    if stop_event is not None and stop_event.is_set():
        return 130
    started = time.monotonic()
    job['status'] = 'running'
    messages = queue.Queue()
    env = {**os.environ, 'PYTHONIOENCODING': 'utf-8'}
    if job['number'] == '05a':
        # Each tape is already a separate process; prevent nested BLAS pools
        # from multiplying the configured concurrency.
        env.update(OPENBLAS_NUM_THREADS='1', OMP_NUM_THREADS='1', MKL_NUM_THREADS='1')
    process = subprocess.Popen(job['command'], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               text=True, encoding='utf-8', errors='replace',
                               creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == 'nt' else 0,
                               env=env)

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
                if stop_event is not None and stop_event.is_set():
                    job['status'] = 'cancelled'
                    job['returncode'] = 130
                    return 130
                try:
                    line = messages.get(timeout=0.1)
                    if line is None:
                        break
                    log.write(line + '\n')
                    log.flush()
                    if report_queue is not None:
                        report_queue.put(line)
                    else:
                        tail.append(line)
                        if live is None:
                            console.print(Text(line))
                except queue.Empty:
                    pass
                if live:
                    live.update(dashboard(jobs, tail, run_dir))
        job['returncode'] = process.wait()
        job['status'] = 'done' if process.returncode == 0 else ('blocked' if job['number'] in {'05b', '06'} else 'failed')
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


def execute_planning_batch(jobs, tail, run_dir, live, console, workers, result, stop_on_error=False):
    """Run independent tape processes, committing each result as it finishes."""
    stop = threading.Event()
    reports = queue.Queue()
    pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix='tape-planner')
    pending = iter(j for j in jobs if j['status'] == 'pending')
    running = {}

    def start_next():
        job = next(pending, None)
        if job is None:
            return False
        console.print(Text(f'{job["number"]} {job["name"]}: {job["description"]}'))
        running[pool.submit(execute, job, jobs, tail, run_dir, None, console, stop, reports)] = job
        return True

    try:
        for _ in range(workers):
            if not start_next():
                break
        while running:
            done, _ = wait(running, timeout=.1, return_when=FIRST_COMPLETED)
            while True:
                try:
                    line = reports.get_nowait()
                except queue.Empty:
                    break
                tail.append(line)
                if live is None:
                    console.print(Text(line))
            if live:
                live.update(dashboard(jobs, tail, run_dir))
            for future in done:
                job = running.pop(future)
                try:
                    code = future.result()
                except Exception as error:
                    job.update(status='failed', returncode=1, error=str(error))
                    console.print(Text(f'{job["name"]}: {error}'))
                    code = 1
                if code:
                    result = result or code
                (run_dir/'run.json').write_text(json.dumps(dict(returncode=result, stages=jobs), indent=2), encoding='utf-8')
            if not (stop_on_error and result):
                for _ in done:
                    start_next()
    except BaseException:
        stop.set()
        raise
    finally:
        pool.shutdown(wait=True, cancel_futures=True)
    return result


def main(argv=None):
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    args, selected = parse_args(argv)
    console = Console()
    if args.denoise_version == 'v2':
        command = script_command('denoise_v2_rollout.py', '--version', 'v2',
                   '--mode', 'dry-run' if args.action == 'plan' else 'resume',
                   '--workers', str(args.denoise_workers), '--tapes', str(args.denoise_tapes))
        if args.channel is not None and args.action == 'run':
            command += ['--tape', args.tape, '--channel', str(args.channel)]
        return subprocess.call(command)
    run_dir = args.output.resolve() / (datetime.now().strftime('%Y%m%d_%H%M%S') + '_' + uuid.uuid4().hex[:8])
    try:
        if '03' in selected and (args.batch_denoise or args.channel is not None):
            # Cache inspection must never queue a legacy write against v2.
            if root_marker(args.denoised_root.resolve()):
                raise ValueError('The denoise root is v2; use --denoise-version v2')
            if args.denoised_root.resolve().name.lower() == 'denoised_v1':
                raise ValueError('The archived denoised_v1 tree is read-only')
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
        console.print('Commands only; no outputs written.' if args.batch else
                      'Commands only. Whole-channel order: 03 denoise, 04 crosstalk, 05a plan on originals, 05b deflutter, 06 Quindar retime, 07 speech normalization.')
        return 2 if invalid else 0
    run_dir.mkdir(parents=True)
    live = None if args.plain or not console.is_terminal else Live(console=console, refresh_per_second=5)
    result = 2 if invalid else 0
    try:
        if live:
            live.start()
        if args.batch_flutter and selected == {'05a'} and args.flutter_plan_workers > 1:
            result = execute_planning_batch(jobs, tail, run_dir, live, console,
                                            args.flutter_plan_workers, result, args.flutter_plan_stop_on_error)
        else:
            for job in jobs:
                if job['status'] != 'pending':
                    continue
                tail.clear()
                console.print(Text(f'{job["number"]} {job["name"]}: {job["description"]}'))
                try:
                    code = execute(job, jobs, tail, run_dir, live, console)
                except Exception as error:
                    if not args.batch:
                        raise
                    job.update(status='failed', returncode=1, error=str(error))
                    console.print(Text(f'{job["name"]}: {error}'))
                    code = 1
                if code:
                    result = result or code
                # Persist each completed job so an overnight interruption leaves a record.
                (run_dir / 'run.json').write_text(json.dumps(dict(returncode=result, stages=jobs), indent=2), encoding='utf-8')
                if code and (not args.batch or (selected == {'05a'} and args.flutter_plan_stop_on_error)):
                    break
    except KeyboardInterrupt:
        for interrupted in jobs:
            if interrupted['status'] == 'running':
                interrupted['status'] = 'cancelled'
        result = 130
        if '05a' in selected:
            console.print(Text('Shared map compilation stopped. Completed maps remain immutable; '
                               'an incomplete destination needs a new output directory. No tone analysis was started.'))
    except Exception as error:
        for failed in jobs:
            if failed['status'] == 'running':
                failed['status'] = 'failed'
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
        if args.batch:
            for failed in jobs:
                if failed['status'] in {'failed', 'blocked'}:
                    detail = failed.get('error') or str(run_dir/failed['log'])
                    console.print(Text(f'Failed: {failed["name"]}: {detail}'))
    return result


if __name__ == '__main__':
    sys.exit(main())
