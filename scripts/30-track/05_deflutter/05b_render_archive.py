"""Render the qualified archive on Proprius and Genus using rolling F: scratch."""
import argparse
import hashlib
import inspect
from contextlib import ExitStack
import json
import os
from pathlib import Path, PureWindowsPath
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading

for variable in ('OPENBLAS_NUM_THREADS', 'OMP_NUM_THREADS', 'MKL_NUM_THREADS', 'NUMEXPR_NUM_THREADS'):
    os.environ[variable] = '1'
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _bootstrap import GENUS_SSH
import numpy as np
import soundfile as sf
from denoise_archive import atomic_json, sha256
from flutter_plan_store import planning_lock
from shared_render import render_audio, render_shared_channel
from shared_tape import DEFAULT_DEFLUTTERED_ROOT, load_tape_plan, verify_bindings
from render_cluster_monitor import RenderMonitor
from render_queue import run_queue

DATA = Path('D:/Apollo_15_30-Track')
SSD = Path('F:/Apollo_15_30-Track')
RELEASE = DATA/'crosstalk_qualification/repeat-v2/complete.json'
QUALIFIED = Path('census-0a7f22d8282355c1/analysis-1d46441629e82576')
PLANS = DATA/'deflutter_planning_qualified'/QUALIFIED
WORK = SSD/'deflutter_render_work/repeat-v2'/QUALIFIED
GENUS_PYTHON = '/home/bfeist/.local/share/apollo15-venv/bin/python'
HERE = Path(__file__).resolve()


def read(path):
    return json.loads(path.read_bytes())


def mounted(path):
    p = PureWindowsPath(path)
    if p.drive.lower() != 'f:' or not p.is_absolute() or '..' in p.parts:
        raise ValueError(f'Expected a shared F: path: {p}')
    return Path('/mnt/f', *p.parts[1:])


def copy_checked(src, dst, digest):
    """Expose only a verified copy. Interrupted transfers retry in the same slot."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        if sha256(dst) != digest:
            raise ValueError(f'Existing file differs from its published hash: {dst}')
        return
    partial = dst.with_suffix(dst.suffix+'.partial')
    shutil.copyfile(src, partial)
    if sha256(partial) != digest:
        raise ValueError(f'Copy verification failed: {partial}')
    partial.rename(dst)


def implementation():
    return {n: sha256(HERE.with_name(n)) for n in (HERE.name, 'shared_render.py', 'flutter.py')}


def worker(request, expected):
    if sha256(request) != expected:
        raise ValueError('Job changed after dispatch')
    job = read(request)
    if job['implementation'] != implementation():
        raise ValueError('Worker implementation differs from coordinator')
    path = Path if os.name == 'nt' else mounted
    source, tape_map, final = (path(job[k]) for k in ('source', 'map', 'result'))
    final.parent.mkdir(parents=True, exist_ok=True)
    with ExitStack() as stack:
        attempt = Path(stack.enter_context(tempfile.TemporaryDirectory(dir=final.parent)))
        scratch = attempt
        if os.name != 'nt':
            # Read each large input once over SMB. Random seeks and verification stay on Genus.
            native = Path.home()/'.local/share/apollo15-deflutter'
            native.mkdir(parents=True, exist_ok=True)
            scratch = Path(stack.enter_context(tempfile.TemporaryDirectory(dir=native)))
            print('Copying to Genus SSD', flush=True)
            shutil.copyfile(source, scratch/'input.wav')
            shutil.copyfile(tape_map, scratch/'map.npy')
            source, tape_map = scratch/'input.wav', scratch/'map.npy'
        output = scratch/'channel'
        data = np.load(tape_map, mmap_mode='r', allow_pickle=False)
        try:
            report = render_audio(job['summary'], source, tape_map, data, output, 5.,
                                  lambda message: print(message, flush=True), lambda: None)
        finally:
            del data
        if scratch != attempt:
            print('Returning verified WAV to F:', flush=True)
            copy_checked(output/'deflutter.wav', attempt/'channel/deflutter.wav',
                         report['audio']['deflutter']['sha256'])
            shutil.copyfile(output/'review.json', attempt/'channel/review.json')
        (attempt/'channel').rename(final)


def valid_result(folder, summary):
    report = read(folder/'review.json')
    expected = summary
    # The verified historical renderer used the identical render_audio code;
    # its only coordinate input was the filename start. Reuse only on that clock.
    if ('acquisition_start_seconds' not in summary['source'] and
            hashlib.sha256(inspect.getsource(render_audio).encode()).hexdigest() ==
            '4eda2e2714dc324b692f622d0478667b641ec0771f0bcfec5d3dd8b542a50bd7' and
            report.get('implementation_sha256') == '96937827b4421bf9dd50784dd21f457a22fdd1835cb4c2ad48aa4b350add3095'):
        expected = dict(summary, implementation_sha256=report['implementation_sha256'])
    if (report.get('status') != 'complete' or any(report.get(k) != v for k, v in expected.items()
                                                 if k != 'status')):
        raise ValueError(f'Output belongs to different audio, map, or implementation: {folder}')
    wav = folder/'deflutter.wav'
    info = sf.info(wav)
    if ((info.frames, info.samplerate, info.channels, info.subtype) !=
            (summary['frames'], summary['sample_rate'], 1, 'FLOAT')
            or sha256(wav) != report['audio']['deflutter']['sha256']):
        raise ValueError(f'Invalid completed WAV: {wav}')
    return report


def inventories(selected=None, release_path=None):
    release = read(release_path or RELEASE)
    if release['status'] != 'complete':
        raise ValueError('Stage 04 release is incomplete')
    sources = Path(release['output_root'])
    manifest = read(DATA/'deflutter_analysis'/QUALIFIED/'input_manifest.json')
    excluded = set(manifest['excluded'])
    tapes = {}
    for row in release['results']:
        tape, ch = row['tape'], row['channel']
        if tape in excluded or selected and tape not in selected:
            continue
        channels = tapes.setdefault(tape, {})
        if ch in channels or not 1 <= ch <= 30:
            raise ValueError(f'Invalid release channel {tape} CH{ch}')
        channels[ch] = row
    if selected and set(tapes) != set(selected):
        raise ValueError('Requested tapes are absent from the stage-04 release or excluded')
    if set(tapes) != ({p.parent.name for p in PLANS.glob('*/plan.json')} if not selected else
                      {t for t in selected if (PLANS/t/'plan.json').is_file()}):
        raise ValueError('Qualified tape plans and stage-04 release disagree')
    return sources, tapes, excluded


def prepare(tape, rows, sources, work, destination, monitor, *, excerpt=None, stop=None):
    """The single transfer thread owns all D: reads, including provenance checks."""
    slot = work/tape
    monitor.transfer(f'D: reading plan and evidence for {tape}')
    loaded = load_tape_plan(PLANS/tape/'plan.json')
    plan_path, plan, _ = loaded
    if not plan['qualified'] or not plan['full_tape'] or plan['inventory']['tape'] != tape:
        raise ValueError(f'Expected a qualified whole-tape plan for {tape}')
    channels = {r['channel'] for r in plan['inventory']['channels']}
    if excerpt is None and channels != set(rows):
        raise ValueError(f'Plan channels and completed stage-04 release disagree on {tape}')
    jobs = []
    map_staged = False
    code = implementation()
    for ch, release_row in sorted(rows.items()):
        if stop is not None and stop.is_set():
            break
        name = f'CH{ch:02d}'
        final = destination/tape/name
        folder = sources/tape/name
        # A previously published destination is validated below using the same expected summary.
        summary = render_shared_channel(plan_path, ch, folder, slot/'preflight'/name,
                    loaded_plan=loaded, start=excerpt[0] if excerpt else None,
                    duration=excerpt[1] if excerpt else None, dry_run=True)
        if (summary['source_audio_sha256'] != release_row['sha256'] or
                summary['source']['frames'] != release_row['frames']):
            raise ValueError(f'Stage 04 release binding changed for {tape} {name}')
        wav = slot/'inputs'/(name+'.wav')
        # Reuse the already staged first tape without another D: read.
        seed = SSD/'crosstalk_removed_archive'/sources.name/tape/name/'crosstalk.wav'
        if seed.is_file():
            wav = seed
        result = slot/'results'/name
        summary['resampler_sha256'] = code['flutter.py']
        job = dict(summary=summary, source=str(wav), map=str(slot/'tape_map.npy'),
                   result=str(result), implementation=code)
        request = slot/'jobs'/(name+'.json')
        monitor.transfer(f'{tape} {name} · verifying completed work or staging input')
        if final.exists():
            valid_result(final, summary)
            monitor.finished((tape,ch), reused=True, published=True)
        elif result.exists():
            valid_result(result, summary)
            monitor.finished((tape,ch), reused=True)
        else:
            if not map_staged:
                copy_checked(plan_path.parent/plan['map']['file'], slot/'tape_map.npy', plan['map']['sha256'])
                map_staged = True
            if wav == seed:
                if sha256(wav) != release_row['sha256']:
                    raise ValueError(f'Changed staged input: {wav}')
            else:
                copy_checked(folder/'crosstalk.wav', wav, release_row['sha256'])
        request.parent.mkdir(parents=True, exist_ok=True)
        atomic_json(request, job)
        jobs.append((ch, job, request, final))
    monitor.transfer(f'D: idle · {tape} ready on F:')
    return jobs


def dispatch(host, request, report):
    if host == 'Proprius':
        argv = [sys.executable, '-u', str(HERE), 'worker', str(request), sha256(request)]
    else:
        remote = [GENUS_PYTHON, '-u', mounted(HERE).as_posix(), 'worker',
                  mounted(request).as_posix(), sha256(request)]
        argv = ['ssh', '-T', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
                '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=3',
                GENUS_SSH, 'exec '+shlex.join(remote)]
    # The interactive coordinator drains jobs on Ctrl+C. Its workers and SSH
    # clients must finish their verified writes before the console exits.
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == 'nt' else 0
    with subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, creationflags=flags,
                          stdin=subprocess.DEVNULL, text=True, errors='replace') as child:
        tail = []
        for line in child.stdout:
            message = line.strip()
            report(message)
            tail = (tail+[message])[-12:]
        if child.wait():
            raise RuntimeError(f'{host} failed on {request}: '+'\n'.join(tail))


def render_job(slot, tape, channel, monitor):
    ch, request = channel
    key = (tape, ch)
    monitor.show_worker(slot, key, 'starting')
    try:
        dispatch(slot[0], request, lambda msg: monitor.show_worker(slot, key, msg))
        monitor.finished(key, host=slot[0])
    finally:
        monitor.show_worker(slot)


def publish(tape, jobs, work, monitor):
    reference = jobs[0][1]['summary']
    canonical_map = Path(reference['shared_map'])
    if (sha256(canonical_map) != reference['map_sha256'] or
            sha256(canonical_map.parent/'plan.json') != reference['plan_sha256']):
        raise ValueError(f'Canonical shared map or plan changed during rendering: {tape}')
    for ch, job, _, final in jobs:
        result = Path(job['result'])
        if final.exists():
            if (tape,ch) not in monitor.published:
                valid_result(final, job['summary'])
                monitor.finished((tape,ch), published=True)
        else:
            monitor.transfer(f'F: → D: {tape} CH{ch:02d} · copying and verifying')
            summary = job['summary']
            verify_bindings(summary['input_bindings'])
            report = valid_result(result, summary)
            final.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(dir=final.parent, prefix='.publish-') as attempt:
                channel = Path(attempt)/'channel'
                copy_checked(result/'deflutter.wav', channel/'deflutter.wav', report['audio']['deflutter']['sha256'])
                atomic_json(channel/'review.json', report)
                channel.rename(final)
            monitor.finished((tape,ch), published=True)
    # Only discard this runner's scratch after every channel is verified on D:.
    slot = (work/tape).resolve()
    if slot.parent != work.resolve() or not tape.startswith('A15_T'):
        raise ValueError(f'Unexpected scratch path: {slot}')
    locked = []
    for ch, _, request, _ in jobs:
        name = f'CH{ch:02d}'
        for path in (slot/'inputs'/(name+'.wav'), slot/'results'/name/'deflutter.wav',
                     slot/'results'/name/'review.json', request):
            discard_scratch(path, locked)
    discard_scratch(slot/'tape_map.npy', locked)
    for path in locked:
        print(f'Published {tape}; Windows still holds scratch file {path}', file=sys.stderr, flush=True)
    monitor.transfer(f'D: idle · verified {tape}')


def discard_scratch(path, locked):
    try:
        path.unlink(missing_ok=True)
    except PermissionError as error:
        if getattr(error, 'winerror', None) not in (32, 33):
            raise
        locked.append(path)


def coordinate(sources, tapes, work, destination, monitor, stop):
    def stage(tape):
        jobs = prepare(tape, tapes[tape], sources, work, destination, monitor, stop=stop)
        pending = [(ch, request) for ch, _, request, _ in jobs if (tape, ch) not in monitor.rendered]
        return jobs, pending

    ordered = sorted(tapes)
    # Resume a prefetched tape first so workers can compute while the disk lane
    # verifies earlier checkpoints. prepare() still validates every input.
    for tape in ordered:
        slot = work/tape
        if (slot/'tape_map.npy').is_file() and any(
                not (slot/'results'/wav.stem/'review.json').is_file()
                for wav in (slot/'inputs').glob('CH??.wav')):
            ordered = [tape, *(name for name in ordered if name != tape)]
            break
    run_queue(ordered, monitor.slots, stage,
              lambda slot, tape, channel: render_job(slot, tape, channel, monitor),
              lambda tape, jobs: publish(tape, jobs, work, monitor), stop)


def main():
    global PLANS
    if sys.argv[1:2] == ['worker']:
        worker(Path(sys.argv[2]), sys.argv[3])
        return 0
    if not sys.stdout.isatty():
        sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('run', 'check'))
    parser.add_argument('--local-workers', type=int, default=10)
    parser.add_argument('--genus-workers', type=int, default=10)
    parser.add_argument('--tape', action='append', help='Restrict the release to these tapes')
    parser.add_argument('--planning-root', type=Path, default=PLANS)
    parser.add_argument('--output-root', type=Path)
    from flutter_alignment import ROOT
    parser.add_argument('--release', type=Path, default=RELEASE, help='Verified stage-04 release manifest')
    parser.add_argument('--alignment-root', type=Path, default=ROOT)
    args = parser.parse_args()
    if not (0 <= args.local_workers <= 16 and 0 <= args.genus_workers <= 16
            and args.local_workers+args.genus_workers):
        parser.error('Choose 0–16 workers per host with at least one active worker')
    PLANS = args.planning_root.resolve()
    sources, tapes, excluded = inventories(args.tape,args.release)
    from tape_coordinates import verify_coordinates
    for tape in tapes:
        verify_coordinates(read(PLANS/tape/'plan.json')['inventory'], args.alignment_root)
    historical = PLANS == (DATA/'deflutter_planning_qualified'/QUALIFIED).resolve()
    if not historical and args.output_root is None:
        parser.error('Choose a new --output-root for the rebuilt plans')
    destination = (args.output_root or DEFAULT_DEFLUTTERED_ROOT).resolve()
    if not historical and destination == DEFAULT_DEFLUTTERED_ROOT.resolve():
        parser.error('New plans require a separate archive destination')
    work = WORK if historical else WORK.parent/('aligned-'+hashlib.sha256(str(PLANS).encode()).hexdigest()[:16])
    sizes = {(t,c): row['frames'] for t, rows in tapes.items() for c,row in rows.items()}
    print(f'{len(tapes)} qualified tapes, {len(sizes)} channels. Census exclusions: {sorted(excluded)}', flush=True)
    monitor = RenderMonitor(sizes, args.local_workers, args.genus_workers)
    if args.command == 'check':
        # Exercise both native environments on the same real excerpt, outside the archive.
        tape = next(iter(sorted(tapes)))
        ch = min(tapes[tape])
        work.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=work.parent) as tmp:
            base = Path(tmp)
            jobs = prepare(tape, {ch:tapes[tape][ch]}, sources, base, base/'published',
                           monitor, excerpt=(600.,30.))
            _, job, request, _ = jobs[0]
            samples = []
            for host, count in [('Proprius',args.local_workers), ('Genus',args.genus_workers)]:
                if count:
                    result = base/host/'channel'
                    atomic_json(request, dict(job, result=str(result)))
                    dispatch(host, request, lambda msg: print(host, msg, flush=True))
                    valid_result(result, job['summary'])
                    samples.append(sf.read(result/'deflutter.wav', dtype='float32')[0])
            if len(samples) == 2:
                peak = float(np.max(np.abs(samples[0]-samples[1])))
                print(f'Cross-host peak difference {peak:g}')
                if peak > 2**-22:
                    raise ValueError('Worker audio differs')
        print('Stage 05b worker check passed.')
        return 0
    from rich.live import Live
    import signal
    stop = threading.Event()
    def interrupt(*_):
        stop.set()
    signal.signal(signal.SIGINT, interrupt)
    with planning_lock(work), planning_lock(destination), Live(monitor, refresh_per_second=2):
        coordinate(sources, tapes, work, destination, monitor, stop)
    print(f'{len(monitor.published)}/{len(sizes)} verified channels on D:.')
    if stop.is_set():
        return 130
    return 0 if monitor.published == set(sizes) else 2


if __name__ == '__main__':
    raise SystemExit(main())
