"""Sequential whole-tape input staging for the stage 05 coordinator."""
from contextlib import ExitStack
import hashlib
import os
from pathlib import Path
import shutil
import time

from flutter_plan_store import planning_lock


class SourceStage:
    def __init__(self, root, cluster):
        self.root = Path(root)
        # The coordinator holds the corresponding cluster lock for this slot's lifetime.
        session = hashlib.sha256(Path(cluster).resolve().as_posix().encode()).hexdigest()
        self.folder = self.root/session

    def clear(self):
        if self.folder.is_dir():
            for channel in range(1, 31):
                (self.folder/f'CH{channel:02d}.wav').unlink(missing_ok=True)
            self.folder.rmdir()

    def load(self, inventory, stopping, progress=None):
        self.clear()
        self.folder.mkdir(parents=True)
        rows = inventory['channels']
        total = sum(row['bytes'] for row in rows)
        if shutil.disk_usage(self.folder).free < total:
            raise OSError(f'Insufficient free space for {total} bytes of source WAVs at {self.folder}')
        # Separate benchmark and production coordinators also take turns copying from D.
        with ExitStack() as lock:
            while not stopping():
                try:
                    lock.enter_context(planning_lock(self.root/'source-copy'))
                    break
                except ValueError:
                    time.sleep(.25)
            for index, row in enumerate(rows, 1):
                if stopping():
                    raise InterruptedError('Stopped during source staging')
                path = Path(row['path'])
                if progress:
                    progress(f'{inventory["tape"]}  ·  Staging WAV {index}/{len(rows)}  ·  CH{row["channel"]:02d}')
                target = self.folder/f'CH{row["channel"]:02d}.wav'
                digest = hashlib.sha256()
                with path.open('rb') as src, target.open('xb') as dst:
                    before = os.fstat(src.fileno())
                    if (before.st_size, before.st_mtime_ns) != (row['bytes'], row['mtime_ns']):
                        raise ValueError(f'Original changed since inventory: {path}')
                    for data in iter(lambda: src.read(4 << 20), b''):
                        dst.write(data)
                        digest.update(data)
                    dst.flush()
                    os.fsync(dst.fileno())
                after = path.stat()
                if (after.st_size, after.st_mtime_ns) != (row['bytes'], row['mtime_ns']):
                    raise ValueError(f'Original changed during staging: {path}')
                copied = hashlib.sha256()
                with target.open('rb') as check:
                    for data in iter(lambda: check.read(4 << 20), b''):
                        copied.update(data)
                if copied.digest() != digest.digest():
                    raise OSError(f'Staged WAV failed SHA-256 verification: {target}')
                os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
        return total
