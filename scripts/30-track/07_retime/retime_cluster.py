"""Stage 07b across Proprius and Genus: one command, one tape queue (docs/23, "Archive run").

``07b_retime.py --all-tapes --all-channels`` on Proprius renders every tape. It starts a Genus
worker over SSH and runs a worker itself. Both take tapes from one queue on the archive disk
(``<retimed root>/.queue/``), so the faster host simply renders more tapes:

* ``<tape>.claim``: created atomically (``O_EXCL``, exclusive over SMB from both hosts) by the
  worker rendering the tape. Its mtime is refreshed every ``HEARTBEAT_S``. A claim older than
  ``STALE_S`` belongs to a dead worker and may be taken over.
* ``<tape>.done``: every channel of the tape is complete. A done tape is never claimed again.
* ``<tape>.failed``: the tape was blocked (no plan, no anchors, a channel failure), with the
  reason. The coordinator clears these at start, so a new run retries them.

Each worker renders its tape with its own local scratch (``--scratch``; defaults: F: on Windows,
the SSD on Genus). A worker's tapes share one render pool (``staged_io.run_stream``): it claims
the next tape once the current one has no channel left to stage, so workers do not drain at each
tape boundary; a host briefly holds two claims. Every archive copy holds ``staged_io.DiskLock``,
so the archive disk sees one sequential stream across both hosts.

Ctrl-C on the coordinator stops both hosts at once: this host's render workers are killed, the SSH
session to Genus is closed and the Genus worker stops on its SIGHUP the same way. Each worker
releases the claims of its unfinished tapes, so a rerun can start straight away. A rerun resumes:
completed channels are skipped from their records.
"""
from __future__ import annotations

import json
import os
import shlex
import socket
import subprocess
import threading
import time
from pathlib import Path

HEARTBEAT_S = 60.0
STALE_S = 300.0   # live workers refresh their claim every HEARTBEAT_S
GENUS = dict(host='bfeist@192.168.0.4', repo='/mnt/f/_repos/Apollo_15',
             python='/home/bfeist/.local/share/apollo15-venv/bin/python',
             scratch='/home/bfeist/.local/share/apollo15-retime', workers=12)
LOCAL_SCRATCH = Path('F:/Apollo_15_30-Track/work/retime') if os.name == 'nt' else Path(GENUS['scratch'])
# options passed through to the Genus worker unchanged (paths are translated to /mnt/<drive>)
PASS_FLAGS = ('no_row_shift', 'allow_quindar_plan', 'no_irig')
PASS_PATHS = ('retimed_root', 'defluttered_root', 'quindar_analysis_root', 'sg_fit')


def add_arguments(p):
    g = p.add_argument_group('two-host rendering (--all-tapes --all-channels)')
    g.add_argument('--tapes', help='comma-separated tapes to render (default: every tape with 05b channels)')
    g.add_argument('--hosts', choices=('auto', 'local', 'genus'), default='auto',
                   help='auto: this host plus Genus when it answers over SSH; local: this host only; genus: Genus only')
    g.add_argument('--genus-host', default=GENUS['host'])
    g.add_argument('--genus-workers', type=int, default=GENUS['workers'])
    g.add_argument('--genus-scratch', default=GENUS['scratch'])
    g.add_argument('--queue-worker', action='store_true', help=argparse_hidden())
    g.add_argument('--plain', action='store_true',
                   help='print log lines instead of the live dashboard (the log file is written either way)')


def argparse_hidden():
    import argparse
    return argparse.SUPPRESS


def linux_path(path):
    """D:/x or D:\\x -> /mnt/d/x (the Genus mounts of the Proprius shares)."""
    s = str(path).replace('\\', '/')
    if len(s) >= 2 and s[1] == ':':
        return f'/mnt/{s[0].lower()}{s[2:]}'
    return s


class TapeQueue:
    def __init__(self, root: Path):
        self.root = Path(root) / '.queue'
        self.root.mkdir(parents=True, exist_ok=True)
        self.me = f'{socket.gethostname()}:{os.getpid()}'

    def _path(self, tape, kind):
        return self.root / f'{tape}.{kind}'

    def state(self, tape):
        for kind in ('done', 'failed'):
            if self._path(tape, kind).is_file():
                return kind
        claim = self._path(tape, 'claim')
        if claim.is_file():
            try:
                return 'stale' if time.time() - claim.stat().st_mtime > STALE_S else 'claimed'
            except FileNotFoundError:
                return 'open'
        return 'open'

    def claim(self, tape):
        if self.state(tape) in ('done', 'failed', 'claimed'):
            return False
        path = self._path(tape, 'claim')
        if self.state(tape) == 'stale':
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            return False
        os.write(fd, json.dumps(dict(worker=self.me, time=time.time())).encode())
        os.close(fd)
        return True

    def heartbeat(self, tape, stop: threading.Event):
        path = self._path(tape, 'claim')
        while not stop.wait(HEARTBEAT_S):
            try:
                os.utime(path)
            except FileNotFoundError:
                return

    def release(self, tape):
        """Drop this worker's claim of an unfinished tape (after an interruption)."""
        path = self._path(tape, 'claim')
        try:
            if json.loads(path.read_text(encoding='utf-8')).get('worker') == self.me:
                path.unlink()
        except (FileNotFoundError, ValueError):
            pass

    def finish(self, tape, failures):
        kind = 'failed' if failures else 'done'
        tmp = self._path(tape, kind + '.partial')
        tmp.write_text(json.dumps(dict(worker=self.me, time=time.time(), failures=failures), indent=1), encoding='utf-8')
        tmp.replace(self._path(tape, kind))
        try:
            self._path(tape, 'claim').unlink()
        except FileNotFoundError:
            pass

    def reset_failed(self):
        for f in self.root.glob('*.failed'):
            f.unlink()

    def summary(self, tapes):
        out = {}
        for t in tapes:
            k = self.state(t)
            rec = None
            if k in ('done', 'failed'):
                rec = json.loads(self._path(t, k).read_text(encoding='utf-8'))
            out[t] = (k, rec)
        return out


class HostStatus:
    """One host's live state for the dashboard: ``<retimed root>/.queue/hosts/<label>.json``.

    Rewritten every ``STATUS_S`` (2 s) by a daemon thread (and at each phase change, at most once a
    second): the channels this host holds (phase, since, input bytes, render progress read from
    the worker's local progress file), the bytes it has published since it started, and its state."""
    STATUS_S = 2.0

    def __init__(self, queue: TapeQueue, label, workers):
        self.path = queue.root / 'hosts' / f'{label}.json'
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.info = dict(label=label, host=socket.gethostname(), pid=os.getpid(), workers=workers,
                         started=time.time(), state='running', done_bytes=0, done_channels=0, failed_channels=0)
        self.jobs = {}
        self.last_write = 0.0
        self.stop = threading.Event()
        threading.Thread(target=self._loop, daemon=True).start()

    def update(self, key, name, phase, **info):
        with self.lock:
            job = self.jobs.setdefault((key, name), dict(tape=key, channel=name, bytes=0, started=time.time()))
            if job.get('phase') != phase:
                job['since'] = time.time()
            job.update(info, phase=phase)
        if time.time() - self.last_write >= 1.0:
            self.write()

    def drop(self, key, name, done_bytes):
        with self.lock:
            self.jobs.pop((key, name), None)
            if done_bytes is None:
                self.info['failed_channels'] += 1
            else:
                self.info['done_bytes'] += done_bytes
                self.info['done_channels'] += 1
        self.write()

    def write(self, state=None):
        import staged_io
        with self.lock:
            if state:
                self.info['state'] = state
            jobs = []
            for job in self.jobs.values():
                job = dict(job)
                if job.get('progress'):
                    p = staged_io.read_progress(job.pop('progress'))
                    job.update(percent=p.get('percent'), text=p.get('text'), progress_time=p.get('time'))
                jobs.append(job)
            data = dict(self.info, updated=time.time(), jobs=jobs)
            self.last_write = time.time()
        try:
            tmp = self.path.with_name(self.path.name + f'.{os.getpid()}.tmp')
            tmp.write_text(json.dumps(data, indent=1), encoding='utf-8')
            os.replace(tmp, self.path)
        except OSError:
            pass  # a display aid only

    def _loop(self):
        while not self.stop.wait(self.STATUS_S):
            self.write()

    def close(self, state):
        self.stop.set()
        self.write(state)


def tapes_of(args):
    if args.tapes:
        return [t.strip() for t in args.tapes.split(',') if t.strip()]
    root = Path(args.defluttered_root)
    return sorted(p.name for p in root.glob('A15_T*') if any(p.glob('CH*/deflutter.wav')))


def worker(args, prepare_tape, label, lock_root, log=print, **stream):
    """Render queued tapes until none is open. Returns the number of tapes this worker finished.

    ``prepare_tape(tape, args, log)`` -> (plan, jobs). The tapes share one render pool
    (``staged_io.run_stream``): the next tape is claimed as soon as the current one has no channel
    left to stage, so its channels start while the last channels of the previous tape render.
    A tape whose plan or jobs cannot be prepared is marked failed and the next one is claimed."""
    import staged_io
    queue = TapeQueue(args.retimed_root)
    args.scratch = args.scratch or LOCAL_SCRATCH
    tapes = iter(tapes_of(args))
    beats, began, finished = {}, {}, []
    tag = lambda m: log(f'[{label}] {m}')  # noqa: E731
    tag(f'worker started: {args.workers} render workers, scratch {args.scratch}, queue {queue.root}')
    status = HostStatus(queue, label, args.workers)

    def finish(tape, failures):
        beats.pop(tape).set()
        queue.finish(tape, failures)
        finished.append(tape)
        tag(f'{tape}: {"FAILED " + "; ".join(failures) if failures else "done"} '
            f'({(time.monotonic() - began.pop(tape)) / 60:.0f} min)')

    def next_batch():
        for tape in tapes:
            if not queue.claim(tape):
                continue
            stop = threading.Event()
            threading.Thread(target=queue.heartbeat, args=(tape, stop), daemon=True).start()
            beats[tape], began[tape] = stop, time.monotonic()
            tag(f'{tape}: claimed')
            try:
                plan, jobs = prepare_tape(tape, args, log=tag)
            except Exception as error:  # noqa: BLE001 - a blocked tape must not stop the queue
                finish(tape, [str(error)])
                continue
            return tape, jobs, plan, Path(args.scratch) / tape
        return None

    state = 'stopped'
    try:
        staged_io.run_stream(next_batch, args.workers, lock_root, finish, log=tag, status=status, **stream)
        state = 'finished'
    finally:
        for tape, stop in beats.items():  # interrupted: release the unfinished tapes for the next run
            stop.set()
            queue.release(tape)
            tag(f'{tape}: released (not finished)')
        status.jobs.clear()
        status.close(state)
    return len(finished)


def genus_command(args):
    parts = [GENUS['python'], '-u', 'scripts/run.py', '07b_retime.py', '--all-tapes', '--all-channels', '--queue-worker',
             '--scratch', args.genus_scratch, '--workers', str(args.genus_workers)]
    if args.tapes:
        parts += ['--tapes', args.tapes]
    for name in PASS_FLAGS:
        if getattr(args, name, False):
            parts.append('--' + name.replace('_', '-'))
    for name in PASS_PATHS:
        value = getattr(args, name, None)
        if value is not None:
            parts += ['--' + name.replace('_', '-'), linux_path(value)]
    return f'cd {shlex.quote(GENUS["repo"])} && exec ' + ' '.join(shlex.quote(x) for x in parts)


def genus_reachable(host):
    try:
        r = subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8', host, 'true'],
                           capture_output=True, timeout=20)
        return r.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def start_genus(args, log):
    cmd = ['ssh', '-tt', '-o', 'BatchMode=yes', '-o', 'ServerAliveInterval=30', '-o', 'ServerAliveCountMax=6',
           args.genus_host, genus_command(args)]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                            text=True, encoding='utf-8', errors='replace', bufsize=1)

    def pump():
        for line in proc.stdout:
            line = line.rstrip('\r\n')
            if line:
                log(line if line.startswith('[genus]') else f'[genus] {line}')
    t = threading.Thread(target=pump, daemon=True)
    t.start()
    return proc, t


def stop_genus(proc, host, log):
    """Close the SSH session (the Genus worker stops on SIGHUP); kill any Genus worker left over."""
    proc.terminate()
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
    try:
        subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8', host,
                        # [.] and [r]: the pattern must not match this shell's own command line
                        'pkill -TERM -f "07b_retime[.]py .*--queue-worke[r]" && sleep 20; '
                        'pkill -KILL -f "07b_retime[.]py .*--queue-worke[r]"; true'],
                       capture_output=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as error:
        log(f'07b: could not confirm Genus stopped ({error}); check with ssh {host} pgrep -af 07b_retime')
    log('07b: Genus stopped')


def run(args, prepare_tape, lock_root):
    """Coordinator (or, with --queue-worker, one worker). Returns a process exit code."""
    import sys
    from collections import deque
    from contextlib import nullcontext
    lock = threading.Lock()
    recent = deque(maxlen=200)
    log_file = None
    live_view = not args.queue_worker and not getattr(args, 'plain', False) and sys.stdout.isatty()
    showing = dict(live=live_view)  # log lines go to the dashboard while it is shown
    if not args.queue_worker:
        logs = Path(args.retimed_root) / '.logs'
        logs.mkdir(parents=True, exist_ok=True)
        log_file = (logs / f'07b-{socket.gethostname().lower()}-{time.strftime("%Y%m%d-%H%M%S")}.log').open(
            'a', encoding='utf-8')

    def log(message):
        with lock:
            if log_file:
                log_file.write(f'{time.strftime("%H:%M:%S")} {message}\n')
                log_file.flush()
            if showing['live']:
                recent.append(f'{time.strftime("%H:%M:%S")} {message}'[:220])
            else:
                print(message, flush=True)

    if args.queue_worker:
        import staged_io
        staged_io.stop_on_signals()  # the coordinator closing the SSH session stops this worker
        try:
            worker(args, prepare_tape, 'genus' if os.name != 'nt' else 'proprius', lock_root, log)
        except KeyboardInterrupt:
            log('worker stopped')
            return 130
        return 0
    queue = TapeQueue(args.retimed_root)
    queue.reset_failed()
    tapes = tapes_of(args)
    use_genus = args.hosts == 'genus' or (args.hosts == 'auto' and genus_reachable(args.genus_host))
    log(f'07b: {len(tapes)} tapes; hosts: {"this host" if args.hosts != "genus" else ""}'
        f'{" + " if use_genus and args.hosts != "genus" else ""}{"Genus" if use_genus else ""}; queue {queue.root}')
    if args.hosts == 'auto' and not use_genus:
        log('07b: Genus did not answer over SSH; rendering on this host only')
    if log_file:
        log(f'07b: log file {log_file.name}')
    board = view = None
    if live_view:
        from rich.live import Live
        import retime_dashboard
        board = retime_dashboard.Dashboard(args.defluttered_root, args.retimed_root, tapes, log_lines=recent)
        view = Live(get_renderable=board.render, refresh_per_second=0.5)
    genus = start_genus(args, log) if use_genus else None
    try:
        with view or nullcontext():
            try:
                if args.hosts != 'genus':
                    worker(args, prepare_tape, socket.gethostname().lower(), lock_root, log)
                if genus:
                    while genus[0].poll() is None:  # a blocking wait cannot be interrupted on Windows
                        time.sleep(1.0)
                    genus[1].join(timeout=10)
            except KeyboardInterrupt:
                if board:
                    board.stopping = True
                log('07b: interrupted; stopping both hosts')
                if genus:
                    stop_genus(genus[0], args.genus_host, log)
                log('07b: stopped. Rerun the same command to resume (completed channels are kept).')
                if view:
                    view.refresh()
                return 130
    finally:
        if live_view:
            showing['live'] = False
            print('\n'.join(list(recent)[-15:]), flush=True)
        if log_file:
            print(f'Log: {log_file.name}', flush=True)
    status = queue.summary(tapes)
    done = [t for t, (k, _) in status.items() if k == 'done']
    failed = {t: r['failures'] for t, (k, r) in status.items() if k == 'failed'}
    left = [t for t, (k, _) in status.items() if k not in ('done', 'failed')]
    log(f'07b: {len(done)}/{len(tapes)} tapes done' + (f'; failed: {len(failed)}' if failed else '')
        + (f'; not finished: {", ".join(left)}' if left else ''))
    for t, f in failed.items():
        log(f'  {t}: ' + '; '.join(f))
    import transcript_times
    for t in done:  # needs the scraped mission transcripts, which are on this host (.local is not in Git)
        transcript_times.tape_check_safe(t, args.retimed_root, progress=log)
    return 0 if not failed and not left else 2
