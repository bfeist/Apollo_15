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
the SSD on Genus). Every archive copy holds ``staged_io.DiskLock``, so the archive disk sees one
sequential stream across both hosts. Interrupting the coordinator stops Genus too (``ssh -tt``).
A rerun resumes: completed channels are skipped from their records.
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


def tapes_of(args):
    if args.tapes:
        return [t.strip() for t in args.tapes.split(',') if t.strip()]
    root = Path(args.defluttered_root)
    return sorted(p.name for p in root.glob('A15_T*') if any(p.glob('CH*/deflutter.wav')))


def worker(args, render_tape, label, log=print):
    """Render queued tapes until none is open. Returns the number of tapes this worker rendered."""
    queue = TapeQueue(args.retimed_root)
    args.scratch = args.scratch or LOCAL_SCRATCH
    done = 0
    log(f'[{label}] worker started: {args.workers} render workers, scratch {args.scratch}, queue {queue.root}')
    for tape in tapes_of(args):
        if not queue.claim(tape):
            continue
        stop = threading.Event()
        beat = threading.Thread(target=queue.heartbeat, args=(tape, stop), daemon=True)
        beat.start()
        began = time.monotonic()
        log(f'[{label}] {tape}: claimed')
        try:
            failures = render_tape(tape, args, log=lambda m: log(f'[{label}] {m}'))
        except Exception as error:  # noqa: BLE001 - a blocked tape must not stop the queue
            failures = [str(error)]
        stop.set()
        queue.finish(tape, failures)
        done += 1
        log(f'[{label}] {tape}: {"FAILED " + "; ".join(failures) if failures else "done"} '
            f'({(time.monotonic() - began) / 60:.0f} min)')
    return done


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


def run(args, render_tape):
    """Coordinator (or, with --queue-worker, one worker). Returns a process exit code."""
    lock = threading.Lock()

    def log(message):
        with lock:
            print(message, flush=True)

    if args.queue_worker:
        worker(args, render_tape, 'genus' if os.name != 'nt' else 'proprius', log)
        return 0
    queue = TapeQueue(args.retimed_root)
    queue.reset_failed()
    tapes = tapes_of(args)
    use_genus = args.hosts == 'genus' or (args.hosts == 'auto' and genus_reachable(args.genus_host))
    log(f'07b: {len(tapes)} tapes; hosts: {"this host" if args.hosts != "genus" else ""}'
        f'{" + " if use_genus and args.hosts != "genus" else ""}{"Genus" if use_genus else ""}; queue {queue.root}')
    if args.hosts == 'auto' and not use_genus:
        log('07b: Genus did not answer over SSH; rendering on this host only')
    genus = start_genus(args, log) if use_genus else None
    try:
        if args.hosts != 'genus':
            worker(args, render_tape, socket.gethostname().lower(), log)
        if genus:
            genus[0].wait()
            genus[1].join(timeout=10)
    except KeyboardInterrupt:
        if genus:
            genus[0].terminate()
        raise
    status = queue.summary(tapes)
    done = [t for t, (k, _) in status.items() if k == 'done']
    failed = {t: r['failures'] for t, (k, r) in status.items() if k == 'failed'}
    left = [t for t, (k, _) in status.items() if k not in ('done', 'failed')]
    log(f'07b: {len(done)}/{len(tapes)} tapes done' + (f'; failed: {len(failed)}' if failed else '')
        + (f'; not finished: {", ".join(left)}' if left else ''))
    for t, f in failed.items():
        log(f'  {t}: ' + '; '.join(f))
    return 0 if not failed and not left else 2
