"""Staged 07b rendering: one sequential archive-disk stream at a time (docs/23, "Running 07b").

The archive disk (D:, an HDD; ``/mnt/d`` on Genus) thrashes when many channels are read and
written at once. Here only the coordinating thread touches it, and only through whole-file
sequential copies:

1. copy a 05b ``deflutter.wav`` into ``<scratch>/in/CHnn/`` (``copy2``: same bytes and mtime),
   verified by size and byte SHA-256;
2. a worker process renders it with ``quindar_retime.retime(read_from=...)`` into
   ``<scratch>/out/CHnn/`` and writes the timed transcript there (small archive reads only:
   review, plan, transcript chunks);
3. the finished folder is copied to the retimed root through a ``.partial`` folder, each file
   verified by byte SHA-256 after the copy, then renamed into place; the scratch copies are removed.

At most ``workers + 1`` channels are staged at once. Every archive copy holds ``DiskLock``: a lock
file created atomically on the archive disk, so 07b runs on Proprius and on Genus share the disk
one stream at a time. A channel whose retimed folder is already complete for the same input
and settings is skipped without reading its audio.

``run_stream`` feeds one process pool from a sequence of batches (tapes). When the current tape has
no channel left to stage it asks for the next tape, so its channels fill the workers that the
last channels of the previous tape leave idle. Each tape is reported finished (``on_done``) as
soon as its own channels are published or failed. ``run_staged`` is the one-batch case.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import time
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path

LOCK_STALE_S = 900.0
CHUNK = 16 * 1024 * 1024


class DiskLock:
    """Exclusive lock file on the archive disk (works over SMB from Linux and Windows)."""

    def __init__(self, root: Path, name='.archive_io.lock', poll_s=2.0):
        self.path = Path(root) / name
        self.poll_s = poll_s

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        while True:
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, json.dumps(dict(host=socket.gethostname(), pid=os.getpid(), time=time.time())).encode())
                os.close(fd)
                return self
            except FileExistsError:
                try:
                    age = time.time() - self.path.stat().st_mtime
                    if age > LOCK_STALE_S:
                        self.path.unlink()
                        continue
                except FileNotFoundError:
                    continue
                time.sleep(self.poll_s)

    def __exit__(self, *exc):
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


def copy_verified(src: Path, dst: Path):
    """Sequential copy with byte SHA-256 of the source; the copy is re-read and must match."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    h = hashlib.sha256()
    tmp = dst.with_name(dst.name + '.partial')
    with open(src, 'rb') as a, open(tmp, 'wb') as b:
        while True:
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


def _render(args):
    source, staged, out, plan, knots = args
    from quindar_retime import retime
    from timed_transcript import write_timed_transcript
    log = []
    record = retime(source, out, shared_plan=plan, row_shift=knots, read_from=staged, progress=log.append)
    write_timed_transcript(out, Path(record['source']['path']), progress=log.append)
    return log[-3:]


def run_staged(jobs, plan, scratch: Path, workers: int, lock_root: Path, log=print):
    """Render ``jobs`` [(source, destination, knots)] through ``scratch``. Returns failure texts."""
    batches = iter([(Path(scratch).name, jobs, plan, scratch)])
    result = []
    run_stream(lambda: next(batches, None), workers, lock_root, lambda key, failures: result.extend(failures), log=log)
    return result


def run_stream(next_batch, workers: int, lock_root: Path, on_done, log=print, render=_render,
               pool_factory=ProcessPoolExecutor):
    """Render batches from ``next_batch()`` -> (key, jobs, plan, scratch) or None through one pool.

    ``jobs`` is [(source, destination, knots)]; the batch stages under its own ``scratch`` folder
    (cleared first: leftovers of an interrupted run). ``next_batch`` is called only when no channel
    is waiting to be staged and a slot is free. ``on_done(key, failures)`` is called once per batch
    when its last channel is published or failed (at once for a batch with nothing to render).
    """
    lock = DiskLock(Path(lock_root))
    workers = max(1, workers)
    pending, staged, running = [], [], {}
    remaining, failures, scratches = {}, {}, {}
    exhausted = False

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

    def renew(old):
        log('render pool broke (a worker process died); starting a new pool')
        old.shutdown(wait=False, cancel_futures=True)
        return pool_factory(max_workers=workers)

    pool = pool_factory(max_workers=workers)
    try:
        while True:
            while len(staged) + len(running) < workers + 1:
                if not pending:
                    if exhausted:
                        break
                    batch = next_batch()
                    if batch is None:
                        exhausted = True
                        break
                    add_batch(*batch)
                    continue
                key, plan, scratch, source, destination, knots = pending.pop(0)
                name = source.parent.name
                local = scratch / 'in' / name / source.name
                began = time.monotonic()
                try:
                    with lock:
                        copy_verified(source, local)
                    log(f'{key} {name}: staged in ({time.monotonic() - began:.0f} s)')
                    staged.append((key, plan, scratch, source, destination, knots, local))
                except Exception as error:  # noqa: BLE001 - block this channel, keep the others
                    fail(key, name, f'stage-in {error}')
                    settle(key)
            while staged and len(running) < workers:
                key, plan, scratch, source, destination, knots, local = staged.pop(0)
                out = scratch / 'out' / source.parent.name
                if out.exists():
                    shutil.rmtree(out)
                job = (str(source), str(local), str(out), str(plan), knots)
                try:
                    fut = pool.submit(render, job)
                except BrokenProcessPool:  # broke before its failed future was read
                    pool = renew(pool)
                    fut = pool.submit(render, job)
                running[fut] = (key, source, destination, local, out, pool)
            if not running:
                if exhausted and not pending and not staged:
                    break
                continue
            done, _ = wait(running, return_when=FIRST_COMPLETED)
            for fut in done:
                key, source, destination, local, out, owner = running.pop(fut)
                name = source.parent.name
                try:
                    for line in fut.result():
                        log(f'{key} {name}: {line}')
                    partial = destination.with_name(destination.name + '.partial')
                    if partial.exists():
                        shutil.rmtree(partial)
                    began = time.monotonic()
                    with lock:
                        for f in sorted(out.iterdir()):
                            if f.is_file():
                                copy_verified(f, partial / f.name)
                    if destination.exists() and any(destination.iterdir()):
                        raise OSError(f'{destination} appeared while rendering')
                    if destination.exists():
                        destination.rmdir()
                    partial.replace(destination)
                    log(f'{key} {name}: published ({time.monotonic() - began:.0f} s)')
                except Exception as error:  # noqa: BLE001 - report and continue with the other channels
                    fail(key, name, error)
                    if isinstance(error, BrokenProcessPool) and owner is pool:
                        pool = renew(pool)
                finally:
                    shutil.rmtree(out, ignore_errors=True)
                    try:
                        local.unlink()
                    except FileNotFoundError:
                        pass
                    settle(key)
    finally:
        pool.shutdown(wait=True, cancel_futures=True)
