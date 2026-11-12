"""Metadata-bound verification cache around the unchanged frozen coordinator.

Only startup may reuse a successful check. Publication and explicit full checks
always call the original verifier. Cache files live in the control session, not
alongside immutable audio or its evidence.
"""
from contextlib import contextmanager
from pathlib import Path
import stat
import threading
import time

import denoise_archive as archive
import denoise_v2_cluster as cluster
import denoise_v2_production as production


class CompletedVerification:
    def __init__(self, config, verify):
        self.verify_original = verify
        self.folder = cluster.session_path(config)
        self.path = self.folder/'verification_cache.json'
        self.progress_path = self.folder/'startup.json'
        self.identity = cluster.digest_json(dict(schema=1, config=config, code={
            str(Path(module).name): cluster.sha256(module)
            for module in (__file__, archive.__file__, production.__file__)}))
        try:
            saved = cluster.read_json(self.path)
            entries = saved['entries'] if saved['identity'] == self.identity else {}
            self.entries = entries if isinstance(entries, dict) else {}
        except (OSError, ValueError, KeyError, TypeError):
            self.entries = {}
        self.lock = threading.Lock()
        self.startup = True
        self.checked = self.cached = self.verified = 0
        self.dirty = False
        self.last_save = self.last_report = 0
        self.started = time.time()
        self.report('Preparing verification', force=True)

    @staticmethod
    def file_stamp(path):
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ValueError(f'Unexpected verification file: {path}')
        return [str(path), info.st_size, info.st_mtime_ns, info.st_ctime_ns,
                info.st_dev, info.st_ino]

    def snapshot(self, folder, job, freeze):
        folder = cluster.exact_path(folder)
        source = cluster.exact_path(job['source']['path'])
        manifest = folder/'denoised.json'
        before = self.file_stamp(manifest)
        record = cluster.read_json(manifest)
        files = [before, self.file_stamp(source),
                 self.file_stamp(archive.denoised_audio_path(folder, record)),
                 self.file_stamp(folder/'cleanup.jsonl')]
        if self.file_stamp(manifest) != before:
            raise ValueError(f'Manifest changed during verification: {manifest}')
        return record, dict(job=cluster.digest_json(dict(job=job, freeze=freeze)),
                            files=files, marker=archive.root_marker(folder))

    def save(self, force=False):
        # Caller holds the lock, including during the publisher's fresh checks.
        if not self.dirty or not force and time.monotonic()-self.last_save < 5:
            return
        try:
            cluster.atomic_json(self.path, dict(identity=self.identity, entries=self.entries))
        except OSError as exc:
            print(f'Verification cache not saved; will recheck next launch: {exc}', flush=True)
        else:
            self.dirty = False
        self.last_save = time.monotonic()

    def report(self, phase, key='', force=False):
        if not force and time.monotonic()-self.last_report < 1:
            return
        state = dict(started=self.started, updated=time.time(), phase=phase, key=key,
                     checked=self.checked, cached=self.cached, verified=self.verified,
                     elapsed_seconds=time.time()-self.started)
        try:
            cluster.atomic_json(self.progress_path, state)
        except OSError:
            pass  # Progress must not prevent work or weaken the actual checks.
        print(f'{phase}: {self.checked} completed channels checked '
              f'({self.cached} cached, {self.verified} verified)'
              + (f'; {key}' if key else ''), flush=True)
        self.last_report = time.monotonic()

    def verify(self, folder, job, freeze, *, full=False):
        key = cluster.job_key(job).as_posix()
        if Path(folder).absolute() != (cluster.CANONICAL/cluster.job_key(job)).absolute():
            return self.verify_original(folder, job, freeze, full=full)
        with self.lock:
            if self.startup:
                self.report('Checking completed channel', key)
            try:
                record, before = self.snapshot(folder, job, freeze)
            except (OSError, ValueError, KeyError, TypeError):
                record, before = None, None
            hit = (self.startup and not full and before is not None
                   and self.entries.get(key) == before)
            if not hit:
                if key in self.entries:
                    del self.entries[key]
                    self.dirty = True
                # Failure propagates; a changed or broken result is never accepted.
                record = self.verify_original(folder, job, freeze, full=full)
                try:
                    after_record, after = self.snapshot(folder, job, freeze)
                except (OSError, ValueError, KeyError, TypeError):
                    after = None
                if before is not None and before == after and record == after_record:
                    self.entries[key] = after
                    self.dirty = True
            if self.startup:
                self.checked += 1
                self.cached += int(hit)
                self.verified += int(not hit)
                self.report('Checking completed channels', key)
            self.save()
            return record

    def finish_startup(self):
        with self.lock:
            if self.startup:
                self.startup = False
                self.save(force=True)
                self.report('Verification finished; starting workers', force=True)

    def close(self):
        with self.lock:
            self.save(force=True)


@contextmanager
def cached_verification(config):
    original_verify, original_select = cluster.verify_completed, cluster.select_job
    cache = CompletedVerification(config, original_verify)

    def select(*args, **kwargs):
        # The first scheduling call follows the entire frozen startup scan.
        cache.finish_startup()
        return original_select(*args, **kwargs)

    cluster.verify_completed, cluster.select_job = cache.verify, select
    try:
        yield cache
    finally:
        cluster.verify_completed, cluster.select_job = original_verify, original_select
        cache.close()
