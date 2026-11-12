"""Windows master console with measured rates and ETAs for both denoise hosts.

Presentation is separate from the frozen cluster worker identity, so console
changes do not invalidate Windows or Genus render checkpoints.
"""

# Resolve shared modules from the stage folders.
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))
from _bootstrap import install as _install
_install()

import argparse
from collections import deque
from contextlib import nullcontext
from datetime import datetime
from zoneinfo import ZoneInfo
import json
from pathlib import Path
import signal
import subprocess
import sys
import time

from rich.console import Console, Group
from rich.live import Live
from rich.table import Table
from rich.text import Text

import denoise_v2_cluster as cluster
from denoise_v2_launcher import GENUS_CPUS, cpu_list
from denoise_v2_accelerated import CHECKPOINT_EXIT
from denoise_v2_worker_runtime import resilient_json_writes


def duration(seconds):
    if seconds is None:
        return 'warming up'
    if seconds <= 0:
        return 'finishing'
    minutes = max(1, round(seconds/60))
    days, minutes = divmod(minutes, 1440)
    hours, minutes = divmod(minutes, 60)
    return f'~{days}d {hours}h' if days else f'~{hours}h {minutes:02d}m'


def update_age(seconds):
    hours, remainder = divmod(max(0, int(seconds)), 3600)
    minutes, seconds = divmod(remainder, 60)
    return f'{hours:02d}:{minutes:02d}:{seconds:02d} ago'


def finish_time(seconds):
    if seconds is None or seconds <= 0:
        return ''
    try:
        from zoneinfo import ZoneInfo
        return datetime.fromtimestamp(time.time()+seconds, ZoneInfo('America/New_York')).strftime('%b %d %I:%M %p %Z')
    except (KeyError, ValueError):
        # Relative ETAs remain valid on Windows installations without tzdata.
        return ''


class Rates:
    """Five-minute progress deltas; never count preexisting checkpoint audio."""
    def __init__(self, window=300, warmup=45):
        self.window, self.warmup = window, warmup
        self.samples = {}
        self.starts = {}

    def observe(self, statuses, now):
        for key, state in statuses.items():
            if 'cursor' not in state or not state.get('host'):
                continue
            cursor = state['cursor']
            samples = self.samples.setdefault(key, deque())
            if samples and cursor < samples[-1][1]:
                samples.clear()
                self.starts.pop(key, None)
            if key not in self.starts:
                self.starts[key] = (now, cursor)
            if not samples or now > samples[-1][0]:
                samples.append((now, cursor))
            while len(samples) > 2 and samples[1][0] < now-self.window:
                samples.popleft()

    def rate(self, key, sample_rate):
        points = self.samples.get(key, ())
        if len(points) < 2:
            return None
        elapsed = points[-1][0]-points[0][0]
        if elapsed < self.warmup:
            return None
        if points[-1][1] <= self.starts[key][1]:
            return None  # No newly committed audio yet, even if startup took a minute.
        return max(0, points[-1][1]-points[0][1])/sample_rate/elapsed


def metrics(jobs, statuses, rates):
    tapes, workers, hosts = {}, {}, {}
    remaining, total, complete = 0., 0., 0
    for key, job in jobs.items():
        state = statuses.get(key, {})
        done = state.get('status') == 'complete'
        rate_hz = job['source']['sample_rate']
        frames = job['source']['frames']
        cursor = frames if done else min(frames, state.get('cursor', 0))
        seconds = (frames-cursor)/rate_hz
        total += frames/rate_hz
        remaining += seconds
        complete += done
        tape = tapes.setdefault(job['tape'], dict(remaining_seconds=0., source_seconds=0.,
            complete=0, channels=0, active=0, rate=0., measured=0, hosts=set()))
        tape['remaining_seconds'] += seconds
        tape['source_seconds'] += frames/rate_hz
        tape['complete'] += done
        tape['channels'] += 1
        if state.get('status') != 'running':
            continue
        host = state['host']
        throughput = rates.rate(key, rate_hz)
        eta = seconds/throughput if throughput and seconds > 0 else (0 if seconds == 0 else None)
        workers[key] = dict(host=host, rate=throughput, eta_seconds=eta)
        tape['active'] += 1
        tape['hosts'].add(host)
        machine = hosts.setdefault(host, dict(rate=0., active=0, measured=0, tapes=set()))
        machine['active'] += 1
        machine['tapes'].add(job['tape'])
        if throughput is not None:
            tape['measured'] += 1
            machine['measured'] += 1
            tape['rate'] += throughput
            machine['rate'] += throughput
    combined = sum(value['rate'] for value in hosts.values())
    measured = sum(value['measured'] for value in hosts.values())
    active = sum(value['active'] for value in hosts.values())
    # An overall estimate based on only one warmed worker would be misleading.
    ready = active > 0 and measured == active and combined > 0
    for tape in tapes.values():
        tape['hosts'] = sorted(tape['hosts'])
        tape['eta_seconds'] = (tape['remaining_seconds']/tape['rate']
            if tape['rate'] > 0 and tape['measured'] == tape['active'] else None)
    for host in hosts.values():
        host['tapes'] = sorted(host['tapes'])
    return dict(rate=combined if ready else None, eta_seconds=remaining/combined if ready else None,
                remaining_seconds=remaining, source_seconds=total, complete=complete,
                channels=len(jobs), tapes=tapes, workers=workers, hosts=hosts)


def rate_text(value):
    return f'{value:.1f} audio h/h' if value is not None else 'warming up'


def worker_ssh_descendants(processes, coordinator_pid):
    descendants = {coordinator_pid}
    while True:
        added = {p['ProcessId'] for p in processes if p['ParentProcessId'] in descendants}
        if added <= descendants:
            break
        descendants.update(added)
    return [p for p in processes if p['ProcessId'] in descendants
            and p['Name'].lower() == 'ssh.exe'
            and any(script in (p.get('CommandLine') or '') for script in
                    ('denoise_v2_cluster.py', 'denoise_v2_accelerated.py'))
            and ' worker ' in (p.get('CommandLine') or '')]


def release_stopped_connections(config, coordinator_pid):
    """Never infer remote exit from silence or an SSH error; ask Genus first."""
    try:
        remote = subprocess.run(['ssh', '-n', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=5',
            config['ssh'], "pgrep -af '[d]enoise_v2_(cluster|accelerated).py worker'"],
            capture_output=True, text=True, timeout=10)
        if remote.returncode != 1 or remote.stdout.strip() or remote.stderr.strip():
            return 0
        snapshot = subprocess.run(['powershell', '-NoProfile', '-Command',
            'Get-CimInstance Win32_Process | Select-Object ProcessId,ParentProcessId,Name,CommandLine | ConvertTo-Json -Compress'],
            capture_output=True, text=True, timeout=10, check=True)
        processes = json.loads(snapshot.stdout)
        stale = worker_ssh_descendants(processes, coordinator_pid)
        for process in stale:
            # Recheck parent and command immediately before terminating this client.
            pid, parent = int(process['ProcessId']), int(process['ParentProcessId'])
            script = (f'$entry = Get-CimInstance Win32_Process -Filter "ProcessId = {pid}"; '
                f'if ($entry.ParentProcessId -eq {parent} -and $entry.Name -eq "ssh.exe" '
                '-and $entry.CommandLine -match "denoise_v2_(cluster|accelerated).py.*worker") '
                '{ Stop-Process -Id $entry.ProcessId -ErrorAction Stop }')
            subprocess.run(['powershell', '-NoProfile', '-Command', script],
                           capture_output=True, timeout=10, check=True)
        return len(stale)
    except (OSError, ValueError, subprocess.SubprocessError):
        return 0  # Unknown remote state: leave workers and their connections alone.


def render(status, progress, limits, stopping=False, all_tapes=False):
    complete, total = progress['complete'], progress['channels']
    tape_complete = sum(t['complete'] == t['channels'] for t in progress['tapes'].values())
    eta = ('stopping' if stopping else 'complete' if complete == total else duration(progress['eta_seconds']))
    finish = '' if stopping else finish_time(progress['eta_seconds'])
    headline = Text(f'Apollo 15  |  {complete}/{total} channels  |  '
                     f'{tape_complete}/{len(progress["tapes"])} tapes complete', style='bold')
    overall = Text(f'Combined rate: {rate_text(progress["rate"])}  |  Archive ETA: {eta}'
                   +(f'  ({finish})' if finish else '')
                   +f'  |  {progress["remaining_seconds"]/3600:,.0f} audio hours remaining')
    hosts = Table('Machine', 'Workers', 'Active tapes', 'Measured rate')
    for name, limit in limits.items():
        host = progress['hosts'].get(name, dict(active=0, measured=0, rate=0., tapes=[]))
        hosts.add_row(name, f'{host["active"]}/{limit}',
            ', '.join(t.replace('A15_', '') for t in host['tapes']) or '—',
            rate_text(host['rate'] if host['active'] and host['active'] == host['measured'] else None))
    tapes = Table('Tape', 'Channels done', 'Workers', 'Audio remaining', 'Measured rate', 'Tape ETA')
    for name, tape in progress['tapes'].items():
        if not all_tapes and not tape['active']:
            continue
        tape_eta = ('complete' if tape['complete'] == tape['channels'] else
                    'queued' if not tape['active'] else duration(tape['eta_seconds']))
        tapes.add_row(name.replace('A15_', ''), f'{tape["complete"]}/{tape["channels"]}', str(tape['active']),
            f'{tape["remaining_seconds"]/3600:.1f} h',
            rate_text(tape['rate'] if tape['measured'] and tape['measured'] == tape['active'] else None), tape_eta)
    workers = Table('Machine', 'Tape / channel', 'Progress', 'Rate', 'Channel ETA', 'State', 'Update')
    for key, worker in sorted(progress['workers'].items(), key=lambda item: (item[1]['host'], item[0])):
        state = status['jobs'][key]
        cursor, frames = state.get('cursor', 0), state.get('frames', 0)
        workers.add_row(worker['host'], key.replace('A15_', ''), f'{100*cursor/frames:.1f}%' if frames else 'starting',
            rate_text(worker['rate']), duration(worker['eta_seconds']), state.get('phase', 'starting'),
            update_age(time.time()-state.get('updated', time.time())))
    footer = ('Stopping both machines at checkpoints; leave this console open.' if stopping else
              'Ctrl+C stops both machines. Rates use up to 5 minutes of progress; ETAs are approximate.')
    if stopping and not progress['workers']:
        footer = 'All workers stopped. Run the same command to resume.'
    return Group(headline, overall, hosts, tapes, workers, Text(footer))


def scheduled_windows_workers(now=None):
    """User's Eastern schedule, including daylight-saving transitions."""
    eastern = ZoneInfo('America/New_York')
    now = datetime.now(eastern) if now is None else now.astimezone(eastern)
    return 12 if 1 <= now.hour < 9 else 6


def report_worker_failures(console, folder, status):
    """A coordinator stop caused by a worker is a failure, not a user pause."""
    for key, state in status.get('jobs', {}).items():
        if state.get('status') == 'interrupted' and state.get('exit_code') is None:
            console.print(f'Worker exit unconfirmed: {key} ({state.get("host", "unknown host")}); '
                          'resume to verify checkpoint.', markup=False)
    failures = [(key, state) for key, state in status.get('jobs', {}).items()
                if state.get('status') == 'failed' or
                (state.get('status') == 'interrupted' and state.get('exit_code') not in (None, CHECKPOINT_EXIT))]
    for key, state in failures:
        console.print(f'Worker failed: {key} ({state.get("host", "unknown host")}), '
                      f'exit {state.get("exit_code", "unknown")}', markup=False)
        if state.get('message'):
            console.print(state['message'], markup=False)
        log = folder/'logs'/(key.replace('/', '_')+'.log')
        console.print(f'Log: {log}', markup=False)
        try:
            with log.open('rb') as stream:
                stream.seek(0, 2)
                stream.seek(max(0, stream.tell()-4096))
                tail = stream.read().decode('utf-8', errors='replace').strip()
            if tail:
                console.print(tail, markup=False)
        except OSError:
            pass  # The failed job and log location must still be visible.
    return bool(failures)


def startup_text(folder, launched):
    """Ignore progress from an earlier console session."""
    try:
        state = cluster.read_json(folder/'startup.json')
        if state['started'] < launched:
            return None
        elapsed = max(0, int(time.time()-state['started']))
        return Text(f'{state["phase"]}' + (f': {state["key"]}' if state['key'] else '')
                    + f'\n{state["checked"]} completed channels checked '
                    + f'({state["cached"]} cached, {state["verified"]} verified)'
                    + f' | Elapsed: {elapsed//60}m {elapsed%60:02d}s')
    except (OSError, ValueError, KeyError, TypeError):
        return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--local-workers', type=int,
                        help='Fixed Windows limit, 0–8; omit for 6 daytime / 12 from 1–9am Eastern')
    parser.add_argument('--genus-workers', type=int, default=6)
    parser.add_argument('--genus-cpus', type=cpu_list, default=GENUS_CPUS,
                        help='Genus CPU affinity (default: balanced six cores); none disables affinity')
    parser.add_argument('--backend', choices=['reference', 'numba'], default='numba',
                        help='Qualified denoise implementation (default: numba)')
    parser.add_argument('--tapes', type=int, default=3)
    parser.add_argument('--all-tapes', action='store_true', help='Also show queued and completed tape rows')
    parser.add_argument('--plain', action='store_true')
    parser.add_argument('--run-seconds', type=float, help='Bounded validation trial')
    args = parser.parse_args()
    if sys.platform != 'win32':
        parser.error('Run this console on Windows; it manages Genus through SSH')
    if args.local_workers is not None and not 0 <= args.local_workers <= 8:
        parser.error('Fixed Windows limits are 0–8; omit --local-workers for the overnight 12-worker schedule')
    args.auto_workers = args.local_workers is None
    args.user_cancelled = False
    try:
        while not args.user_cancelled:
            if args.auto_workers:
                args.local_workers = scheduled_windows_workers()
            result, restart = run_session(args)
            if args.user_cancelled or not restart:
                return result
    except KeyboardInterrupt:
        return 130
    return 130


def run_session(args):
    config = cluster.read_json(cluster.CONTROL/cluster.read_json(cluster.CANONICAL/cluster.MARKER)['run_id']/'cluster/config.json')
    folder = cluster.session_path(config)
    jobs = {cluster.job_key(job).as_posix():job for job in cluster.read_json(folder/'jobs.json')}
    command = [sys.executable, '-u', str(Path(__file__).with_name('denoise_v2_launcher.py')),
        '--config', str(folder/'config.json'), '--plain', '--genus-cpus', args.genus_cpus,
        '--backend', args.backend,
        '--local-workers', str(args.local_workers), '--genus-workers', str(args.genus_workers), '--tapes', str(args.tapes)]
    if args.run_seconds:
        command += ['--run-seconds', str(args.run_seconds)]
    console, rates = Console(), Rates()
    stopping, rescheduling, child = False, False, None
    def request_stop(*_):
        nonlocal stopping
        stopping = True
        args.user_cancelled = True
        if child is not None:
            (folder/'stop_requested').touch()
    original_handler = signal.signal(signal.SIGINT, request_stop)
    # One console prevents a second wrapper from signalling somebody else's coordinator.
    try:
        with resilient_json_writes(), cluster.planning_lock(folder/'console'), \
             (folder/'console.log').open('a', encoding='utf-8') as log:
            if args.user_cancelled:
                return 130, False
            launched = time.time()
            child = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
            limits = dict(windows=args.local_workers, genus=args.genus_workers)
            view = (Live(Text('Verifying completed channels and resuming checkpoints...'),
                         console=console, refresh_per_second=1) if not args.plain else nullcontext())
            with view as live:
                last_plain, last_shutdown_check = 0, 0
                while child.poll() is None:
                    if (args.auto_workers and not args.run_seconds and not stopping and not rescheduling
                            and scheduled_windows_workers() != args.local_workers):
                        rescheduling = True
                        console.print(f'Checkpointing to switch Windows to {scheduled_windows_workers()} workers (Eastern schedule).')
                    if stopping or rescheduling:
                        # Reassert in case the coordinator was still in startup and cleared the flag.
                        (folder/'stop_requested').touch()
                    try:
                        status = cluster.read_json(folder/'status.json')
                    except (OSError, ValueError):
                        status = {}
                    if status.get('updated', 0) >= launched:
                        stopping = stopping or (bool(status.get('stop_reason')) and not rescheduling)
                        pausing = stopping or rescheduling
                        if pausing and time.monotonic()-last_shutdown_check >= 30:
                            last_shutdown_check = time.monotonic()
                            released = release_stopped_connections(config, child.pid)
                            if released:
                                console.print(f'Genus has stopped; released {released} leftover SSH connections.')
                        for key, state in status['jobs'].items():
                            if state.get('phase') in ('uploading', 'verifying / publishing'):
                                state['cursor'] = jobs[key]['source']['frames']
                        rates.observe(status['jobs'], time.time())
                        progress = metrics(jobs, status['jobs'], rates)
                        if pausing:
                            progress['eta_seconds'] = None
                        cluster.atomic_json(folder/'progress.json', dict(updated=time.time(), stopping=pausing,
                                            rescheduling=rescheduling, **progress))
                        if live is not None:
                            live.update(render(status, progress, limits, pausing, args.all_tapes))
                        if args.plain and time.time()-last_plain >= 10:
                            console.print(f'{progress["complete"]}/{progress["channels"]} complete; '
                                f'{rate_text(progress["rate"])}; archive ETA {"stopping" if pausing else duration(progress["eta_seconds"])}; '
                                + ' '.join(f'{h}={progress["hosts"].get(h,{}).get("active",0)}/{n}' for h,n in limits.items()))
                            last_plain = time.time()
                    else:
                        startup = startup_text(folder, launched)
                        if startup is not None:
                            if live is not None:
                                live.update(startup)
                            if args.plain and time.time()-last_plain >= 10:
                                console.print(startup)
                                last_plain = time.time()
                    time.sleep(1)
                # Leave a truthful final display/snapshot rather than the last seven
                # workers that happened to be visible while they were winding down.
                status = cluster.read_json(folder/'status.json')
                if status.get('updated', 0) >= launched:
                    progress = metrics(jobs, status['jobs'], rates)
                    cluster.atomic_json(folder/'progress.json', dict(updated=time.time(), stopping=stopping or rescheduling,
                                        rescheduling=rescheduling, **progress))
                    if live is not None:
                        live.update(render(status, progress, limits, stopping or rescheduling, args.all_tapes))
            failed = any(state.get('status') == 'failed' for state in status.get('jobs', {}).values())
            unsafe_stop = any(state.get('status') == 'interrupted' and state.get('exit_code') != CHECKPOINT_EXIT
                              for state in status.get('jobs', {}).values())
            restart = (rescheduling and not stopping and not failed and not unsafe_stop and child.returncode == 1
                       and status.get('updated', 0) >= launched and bool(status.get('stop_reason')))
            if restart:
                console.print('Checkpoints saved. Resuming with the scheduled Windows limit.')
                return (0, True) if not stopping and not args.user_cancelled else (130, False)
            if child.returncode and not stopping:
                console.print((folder/'console.log').read_text(encoding='utf-8')[-4000:])
            worker_failed = report_worker_failures(console, folder, status)
            console.print('Workers failed; see the errors above. Run the same command after resolving them.' if worker_failed else
                          'Stopped. Run the same command to resume.' if stopping and child.returncode in (0, 1, 130) else
                          'Archive finished.' if child.returncode == 0 else 'Coordinator failed; see console.log.')
            return child.returncode, False
    finally:
        if child is not None and child.poll() is None:
            request_stop()
            console.print('Waiting for Windows and Genus workers to checkpoint...')
            while child.poll() is None:
                (folder/'stop_requested').touch()
                release_stopped_connections(config, child.pid)
                time.sleep(1)
        signal.signal(signal.SIGINT, original_handler)


if __name__ == '__main__':
    raise SystemExit(main())
