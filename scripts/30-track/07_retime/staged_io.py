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
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import time
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
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
    scratch = Path(scratch)
    shutil.rmtree(scratch, ignore_errors=True)  # leftovers of an interrupted run of this tape
    lock = DiskLock(Path(lock_root))
    pending, failures = [], []
    for source, destination, knots in jobs:
        if complete_matching(destination, source):
            log(f'{destination.name}: complete; skipped')
        else:
            pending.append((Path(source), Path(destination), knots))
    staged, running = [], {}
    with ProcessPoolExecutor(max_workers=max(1, workers)) as pool:
        while pending or staged or running:
            while pending and len(staged) + len(running) < workers + 1:
                source, destination, knots = pending.pop(0)
                local = scratch / 'in' / source.parent.name / source.name
                began = time.monotonic()
                try:
                    with lock:
                        copy_verified(source, local)
                    log(f'{source.parent.name}: staged in ({time.monotonic() - began:.0f} s)')
                    staged.append((source, destination, knots, local))
                except OSError as error:
                    failures.append(f'{source.parent.name}: stage-in {error}')
                    log(f'BLOCKED {failures[-1]}')
            while staged and len(running) < workers:
                source, destination, knots, local = staged.pop(0)
                out = scratch / 'out' / source.parent.name
                if out.exists():
                    shutil.rmtree(out)
                fut = pool.submit(_render, (str(source), str(local), str(out), str(plan), knots))
                running[fut] = (source, destination, local, out)
            if not running:
                continue
            done, _ = wait(running, return_when=FIRST_COMPLETED)
            for fut in done:
                source, destination, local, out = running.pop(fut)
                try:
                    for line in fut.result():
                        log(f'{source.parent.name}: {line}')
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
                    log(f'{source.parent.name}: published ({time.monotonic() - began:.0f} s)')
                except Exception as error:  # noqa: BLE001 - report and continue with the other channels
                    failures.append(f'{source.parent.name}: {error}')
                    log(f'BLOCKED {failures[-1]}')
                finally:
                    shutil.rmtree(out, ignore_errors=True)
                    try:
                        local.unlink()
                    except FileNotFoundError:
                        pass
    return failures
