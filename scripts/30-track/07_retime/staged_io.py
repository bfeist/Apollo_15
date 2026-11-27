"""Staged 07b rendering: one sequential archive-disk stream at a time (docs/23, "Running 07b").

The archive disk (D:, an HDD; ``/mnt/d`` on Genus) thrashes when many channels are read and
written at once. It is touched only by one disk thread per host, and only through whole-file
sequential copies:

1. copy a 05b ``deflutter.wav`` into ``<scratch>/in/CHnn/`` (same bytes and mtime), verified by
   byte SHA-256;
2. a worker process renders it with ``quindar_retime.retime(read_from=...)`` into
   ``<scratch>/out/CHnn/`` and writes the timed transcript there (small archive reads only:
   review, plan, transcript chunks);
3. the finished folder is copied to the retimed root through a ``.partial`` folder, each file
   verified by byte SHA-256 after the copy, then renamed into place; the scratch copies are removed.

Channels flow one at a time. A channel is staged in only when a render worker is free for it (plus
one staged spare while every worker is busy), and it starts rendering as soon as its copy is
verified, so the first workers render while later channels are still being staged. A rendered
channel is queued for publishing and its worker takes the next staged channel at once: the
coordinator never waits for the disk. The disk thread does one copy at a time: a stage-in while a
worker is idle, otherwise the publish of the oldest rendered channel.

Every archive copy holds ``DiskLock``: a lock file created atomically on the archive disk, so 07b
runs on Proprius and on Genus share the disk one stream at a time, alternating copy by copy when
both are waiting. A channel whose retimed folder is already complete for the same input and
settings is skipped without reading its audio.

``run_stream`` feeds one process pool from a sequence of batches (tapes). When the current tape has
no channel left to stage it asks for the next tape, so its channels fill the workers that the
last channels of the previous tape leave idle. Each tape is reported finished (``on_done``) as
soon as its own channels are published or failed. ``run_staged`` is the one-batch case.

Ctrl-C (or SIGTERM/SIGHUP on Linux, see ``stop_on_signals``) stops the run at once: the render
workers ignore SIGINT themselves, the coordinator polls instead of blocking (a blocking wait cannot
be interrupted on Windows), and on interruption it kills the render workers and cancels the copy
in progress instead of waiting for their channels. Nothing half-written is published; a rerun
clears the scratch and resumes.
"""
from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import shutil
import signal
import socket
import threading
import time
from concurrent.futures import FIRST_COMPLETED, Future, ProcessPoolExecutor, wait
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path

LOCK_STALE_S = 900.0
WAIT_STALE_S = 120.0  # a waiter refreshes its wait file every poll; older ones are from dead processes
CHUNK = 4 * 1024 * 1024  # the SMB rsize/wsize of the Genus mounts
POLL_S = 1.0  # the coordinator's wait for renders; short so Ctrl-C is seen at once on Windows


def _ignore_sigint():
    """Render workers leave Ctrl-C to the coordinator (it would otherwise kill them one by one and
    the broken pool would be renewed, so the run would carry on)."""
    signal.signal(signal.SIGINT, signal.SIG_IGN)


def render_pool(max_workers):
    return ProcessPoolExecutor(max_workers=max_workers, initializer=_ignore_sigint)


def stop_on_signals():
    """On Linux, SIGTERM and SIGHUP (the SSH session of a Genus worker closing) stop like Ctrl-C."""
    def interrupt(signum, frame):
        raise KeyboardInterrupt(f'signal {signum}')
    for name in ('SIGTERM', 'SIGHUP'):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), interrupt)


def kill_pool(pool):
    """Stop a pool now: cancel queued work and terminate its worker processes."""
    processes = list((getattr(pool, '_processes', None) or {}).values())
    pool.shutdown(wait=False, cancel_futures=True)
    for p in processes:
        try:
            p.terminate()
        except Exception:  # noqa: BLE001 - already gone
            pass
    for p in processes:
        p.join(timeout=10)


class Cancelled(Exception):
    """A disk copy or lock wait stopped by the run's interruption."""


class DiskLock:
    """Exclusive lock file on the archive disk (works over SMB from Linux and Windows).

    Fair between hosts: a process waiting for the lock keeps a ``<lock>.<host>-<pid>.wait`` file,
    refreshed every poll. After a release, the releasing process holds back from its next
    acquisition while another process is waiting (for a few polls), so the waiter gets the lock
    instead of the holder taking it straight back: two waiting hosts alternate copy by copy.
    ``cancel`` (an Event) stops a wait with ``Cancelled``."""

    def __init__(self, root: Path, name='.archive_io.lock', poll_s=1.0, cancel=None):
        self.path = Path(root) / name
        self.poll_s = poll_s
        self.cancel = cancel
        self.me = f'{socket.gethostname()}-{os.getpid()}'
        self.wait_path = self.path.with_name(f'{self.path.name}.{self.me}.wait')
        self._yield_until = 0.0

    def _others_waiting(self):
        now = time.time()
        found = False
        for f in self.path.parent.glob(self.path.name + '.*.wait'):
            if f.name == self.wait_path.name:
                continue
            try:
                if now - f.stat().st_mtime > WAIT_STALE_S:
                    f.unlink()
                else:
                    found = True
            except FileNotFoundError:
                pass
        return found

    def _sleep(self, seconds):
        if self.cancel is None:
            time.sleep(seconds)
        elif self.cancel.wait(seconds):
            raise Cancelled('disk lock wait cancelled')

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        waiting = False
        try:
            while True:
                if self.cancel is not None and self.cancel.is_set():
                    raise Cancelled('disk lock wait cancelled')
                if time.monotonic() < self._yield_until and self._others_waiting():
                    self._sleep(self.poll_s)
                    continue
                try:
                    fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                    os.write(fd, json.dumps(dict(host=socket.gethostname(), pid=os.getpid(), time=time.time())).encode())
                    os.close(fd)
                    return self
                except FileExistsError:
                    try:
                        if time.time() - self.path.stat().st_mtime > LOCK_STALE_S:
                            self.path.unlink()
                            continue
                    except FileNotFoundError:
                        continue
                try:  # announce (or refresh) this waiter so the holder yields to it
                    self.wait_path.write_text(self.me, encoding='utf-8')
                    waiting = True
                except OSError:
                    pass
                self._sleep(self.poll_s)
        finally:
            if waiting:
                try:
                    self.wait_path.unlink()
                except FileNotFoundError:
                    pass

    def __exit__(self, *exc):
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        # a waiter polls every poll_s (both hosts use the same interval): give it time to take the lock
        self._yield_until = time.monotonic() + 3 * self.poll_s + 1.0


def copy_verified(src: Path, dst: Path, cancel=None):
    """Sequential copy with byte SHA-256 of the source; the copy is re-read and must match.

    ``cancel`` (an Event) stops the copy with ``Cancelled``; the ``.partial`` file is removed."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    h = hashlib.sha256()
    tmp = dst.with_name(dst.name + '.partial')
    try:
        with open(src, 'rb') as a, open(tmp, 'wb') as b:
            while True:
                if cancel is not None and cancel.is_set():
                    raise Cancelled(f'copy of {src} cancelled')
                block = a.read(CHUNK)
                if not block:
                    break
                h.update(block)
                b.write(block)
        shutil.copystat(src, tmp)
        g = hashlib.sha256()
        with open(tmp, 'rb') as b:
            while True:
                block = b.read(CHUNK)
                if not block:
                    break
                g.update(block)
        if g.hexdigest() != h.hexdigest():
            raise OSError(f'copy of {src} does not verify')
        tmp.replace(dst)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    return h.hexdigest()


def complete_matching(destination: Path, source: Path):
    """True when ``destination`` holds a complete retime of this exact source (metadata only)."""
    rec_path = destination / 'retime.json'
    if not rec_path.is_file() or not (destination / 'retimed.wav').is_file():
        return False
    rec = json.loads(rec_path.read_text(encoding='utf-8'))
    st = source.stat()
    src = rec.get('source', {})
    return rec.get('status') == 'complete' and (src.get('bytes'), src.get('mtime_ns')) == (st.st_size, st.st_mtime_ns) \
        and (destination / 'timed_transcript.json').is_file()


def progress_path(out):
    """Live progress of one rendering channel: ``<scratch>/progress/CHnn.json`` (local, small)."""
    out = Path(out)
    return out.parent.parent / 'progress' / (out.name + '.json')


def note_progress(path, text):
    """Record the latest progress line of a render (``Retime 42.0% ...`` gives the percentage)."""
    data = dict(text=str(text)[:200], time=time.time())
    m = re.match(r'Retime ([\d.]+)%', str(text))
    if m:
        data['percent'] = float(m.group(1))
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + '.tmp')
        tmp.write_text(json.dumps(data), encoding='utf-8')
        os.replace(tmp, path)
    except OSError:
        pass  # progress is a display aid only


def read_progress(path):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def _render(args):
    source, staged, out, plan, knots = args
    from quindar_retime import retime
    from timed_transcript import write_timed_transcript
    log, live = [], progress_path(out)

    def progress(message):
        log.append(message)
        note_progress(live, message)
    record = retime(source, out, shared_plan=plan, row_shift=knots, read_from=staged, progress=progress)
    note_progress(live, 'Timed transcript')
    write_timed_transcript(out, Path(record['source']['path']), progress=progress)
    return log[-3:]


class _NoStatus:
    def update(self, *args, **kwargs):
        pass

    def drop(self, *args, **kwargs):
        pass


def run_staged(jobs, plan, scratch: Path, workers: int, lock_root: Path, log=print):
    """Render ``jobs`` [(source, destination, knots)] through ``scratch``. Returns failure texts."""
    batches = iter([(Path(scratch).name, jobs, plan, scratch)])
    result = []
    run_stream(lambda: next(batches, None), workers, lock_root, lambda key, failures: result.extend(failures), log=log)
    return result


class _DiskThread:
    """One daemon thread doing this host's archive copies, one at a time (``submit`` -> Future)."""

    def __init__(self):
        self.tasks = queue.Queue()
        self.thread = threading.Thread(target=self._loop, name='archive-disk', daemon=True)
        self.thread.start()

    def _loop(self):
        while True:
            item = self.tasks.get()
            if item is None:
                return
            fut, fn = item
            if not fut.set_running_or_notify_cancel():
                continue
            try:
                fut.set_result(fn())
            except BaseException as error:  # noqa: BLE001 - handed to the coordinator
                fut.set_exception(error)

    def submit(self, fn):
        fut = Future()
        self.tasks.put((fut, fn))
        return fut

    def close(self, timeout=None):
        self.tasks.put(None)
        self.thread.join(timeout)


def _rate(size, seconds):
    return f'{seconds:.0f} s, {size / 1e6 / max(seconds, 1e-3):.0f} MB/s'


def run_stream(next_batch, workers: int, lock_root: Path, on_done, log=print, render=_render,
               pool_factory=render_pool, status=None, should_stop=None):
    """Render batches from ``next_batch()`` -> (key, jobs, plan, scratch) or None through one pool.

    ``jobs`` is [(source, destination, knots)]; the batch stages under its own ``scratch`` folder
    (cleared first: leftovers of an interrupted run). ``next_batch`` is called only when no channel
    is waiting to be staged and a slot is free. ``on_done(key, failures)`` is called once per batch
    when its last channel is published or failed (at once for a batch with nothing to render).
    ``status`` (``update(key, name, phase, **info)`` / ``drop(key, name, done_bytes)``) is told each
    channel's phase for the dashboard (retime_dashboard.py). ``should_stop()`` is polled every
    ``POLL_S``; when it returns true the run stops as on Ctrl-C (``07b_retime.py --stop``).

    This thread only schedules: archive copies run on one disk thread, renders in the pool. The
    disk thread gets one channel at a time: a stage-in while a worker is idle, otherwise the oldest
    rendered channel's publish, otherwise one spare stage-in. A staged channel is submitted at once.
    """
    status = status or _NoStatus()
    cancel = threading.Event()
    lock = DiskLock(Path(lock_root), cancel=cancel)
    workers = max(1, workers)
    pending, staged, running, rendered = [], [], {}, []
    remaining, failures, scratches = {}, {}, {}
    exhausted = False
    io = None  # (future, kind, item): the copy on the disk thread

    def fail(key, name, text):
        failures[key].append(f'{name}: {text}')
        log(f'BLOCKED {key} {failures[key][-1]}')

    def settle(key):
        remaining[key] -= 1
        if remaining[key] == 0:
            del remaining[key]
            shutil.rmtree(scratches.pop(key), ignore_errors=True)
            on_done(key, failures.pop(key))

    def add_batch(key, jobs, plan, scratch):
        scratch = Path(scratch)
        shutil.rmtree(scratch, ignore_errors=True)
        remaining[key], failures[key], scratches[key] = 1, [], scratch  # 1 holds the batch open while listing
        for source, destination, knots in jobs:
            source, destination = Path(source), Path(destination)
            try:
                if complete_matching(destination, source):
                    log(f'{key} {destination.name}: complete; skipped')
                    continue
            except Exception as error:  # noqa: BLE001 - an unreadable record blocks this channel only
                fail(key, destination.name, f'record {error}')
                continue
            pending.append((key, plan, scratch, source, destination, knots))
            remaining[key] += 1
        settle(key)

    def stage_in(item):  # on the disk thread
        key, plan, scratch, source, destination, knots = item
        name = source.parent.name
        local = scratch / 'in' / name / source.name
        size = source.stat().st_size
        status.update(key, name, 'waiting for disk', bytes=size)
        with lock:
            began = time.monotonic()
            status.update(key, name, 'staging in', bytes=size)
            copy_verified(source, local, cancel)
        return local, size, time.monotonic() - began

    def publish(item):  # on the disk thread
        key, source, destination, local, out = item
        name = source.parent.name
        partial = destination.with_name(destination.name + '.partial')
        if partial.exists():
            shutil.rmtree(partial)
        files = [f for f in sorted(out.iterdir()) if f.is_file()]
        with lock:
            began = time.monotonic()
            status.update(key, name, 'publishing')
            for f in files:
                copy_verified(f, partial / f.name, cancel)
            took = time.monotonic() - began
        if destination.exists() and any(destination.iterdir()):
            raise OSError(f'{destination} appeared while rendering')
        if destination.exists():
            destination.rmdir()
        partial.replace(destination)
        return sum(f.stat().st_size for f in files), took

    def cleanup(local, out):
        shutil.rmtree(out, ignore_errors=True)
        try:
            local.unlink()
        except FileNotFoundError:
            pass

    def renew(old):
        log('render pool broke (a worker process died); starting a new pool')
        old.shutdown(wait=False, cancel_futures=True)
        return pool_factory(max_workers=workers)

    def check_stop():
        if should_stop and should_stop():
            log('stop requested')
            raise KeyboardInterrupt('stop requested')

    def submit(pool, item):
        key, plan, scratch, source, destination, knots, local = item
        name = source.parent.name
        out = scratch / 'out' / name
        if out.exists():
            shutil.rmtree(out)
        job = (str(source), str(local), str(out), str(plan), knots)
        try:
            progress_path(out).unlink()
        except FileNotFoundError:
            pass
        try:
            fut = pool.submit(render, job)
        except BrokenProcessPool:  # broke before its failed future was read
            pool = renew(pool)
            fut = pool.submit(render, job)
        status.update(key, name, 'rendering', progress=str(progress_path(out)))
        running[fut] = (key, source, destination, local, out, pool)
        return pool

    def staged_done(fut, item):
        key, plan, scratch, source, destination, knots = item
        name = source.parent.name
        try:
            local, size, took = fut.result()
        except Cancelled:
            raise
        except Exception as error:  # noqa: BLE001 - block this channel, keep the others
            fail(key, name, f'stage-in {error}')
            status.drop(key, name, None)
            settle(key)
            return
        log(f'{key} {name}: staged in ({_rate(size, took)})')
        status.update(key, name, 'staged', bytes=size)
        staged.append((key, plan, scratch, source, destination, knots, local))

    def published_done(fut, item):
        key, source, destination, local, out = item
        name = source.parent.name
        published = None
        try:
            size, took = fut.result()
            published = source.stat().st_size
            log(f'{key} {name}: published ({_rate(size, took)})')
        except Cancelled:
            raise
        except Exception as error:  # noqa: BLE001 - report and continue with the other channels
            fail(key, name, error)
        finally:
            cleanup(local, out)
            status.drop(key, name, published)
            settle(key)

    def rendered_done(fut, pool):
        key, source, destination, local, out, owner = running.pop(fut)
        name = source.parent.name
        try:
            for line in fut.result():
                log(f'{key} {name}: {line}')
        except Exception as error:  # noqa: BLE001 - report and continue with the other channels
            fail(key, name, error)
            if isinstance(error, BrokenProcessPool) and owner is pool:
                pool = renew(pool)
            cleanup(local, out)
            status.drop(key, name, None)
            settle(key)
            return pool
        try:
            local.unlink()  # the staged input is no longer needed
        except FileNotFoundError:
            pass
        status.update(key, name, 'rendered')
        rendered.append((key, source, destination, local, out))
        return pool

    disk = _DiskThread()
    pool = pool_factory(max_workers=workers)
    try:
        while True:
            check_stop()
            while staged and len(running) < workers:
                pool = submit(pool, staged.pop(0))
            staging = 1 if io and io[1] == 'stage' else 0
            # claim the next batch once this one has nothing left to stage and a slot would be free
            while not pending and not exhausted and len(running) + len(staged) + staging < workers + 1:
                batch = next_batch()
                if batch is None:
                    exhausted = True
                else:
                    add_batch(*batch)
            if io is None:
                idle = workers - len(running) - len(staged)
                if pending and (idle > 0 or (not rendered and not staged)):
                    item = pending.pop(0)
                    io = (disk.submit(lambda item=item: stage_in(item)), 'stage', item)
                elif rendered:
                    item = rendered.pop(0)
                    io = (disk.submit(lambda item=item: publish(item)), 'publish', item)
            if not running and io is None and not staged:
                if exhausted and not pending and not rendered:
                    break
                continue
            done, _ = wait(list(running) + ([io[0]] if io else []), timeout=POLL_S, return_when=FIRST_COMPLETED)
            if io and io[0] in done:
                fut, kind, item = io
                io = None
                (staged_done if kind == 'stage' else published_done)(fut, item)
            for fut in done:
                if fut in running:
                    pool = rendered_done(fut, pool)
    except BaseException:  # Ctrl-C or a signal: stop now, do not wait for the running channels
        cancel.set()
        kill_pool(pool)
        disk.close(timeout=30)  # the copy in progress stops at its next block and releases the lock
        raise
    disk.close()
    pool.shutdown(wait=True, cancel_futures=True)
