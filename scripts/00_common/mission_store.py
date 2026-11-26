"""Atomic, resumable storage for transcript chunks and run ledgers.

Layout under the transcript root::

    <settings_id>/settings.json
    <settings_id>/sources/<collection>/<...key>/<identity_id>/source.json
    <settings_id>/sources/<collection>/<...key>/<identity_id>/chunks/c00012/{chunk.json,segments.jsonl.gz}

A chunk directory appears only by renaming a complete temporary directory. Aligning
an older chunk replaces it the same way, after moving the old directory aside.
Reuse requires the same settings, the same source identity and the same nominal
chunk range. A new source checksum, origin or map gives a new identity directory;
earlier transcripts stay in place and are reported as superseded. A store is found
by its transcript-defining settings (``resolve_settings``), so execution-only
settings such as the batch size need not match.

Session ledgers (one per invocation) live under the evidence root and record
throughput, failures and the selected scope.
"""
from __future__ import annotations

import ctypes
import gzip
import hashlib
import json
import os
import shutil
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from audio_sources import Source

# Interim transcripts for tape timing; a later archive transcript is expected after retiming.
CHUNK_SCHEMA = 1


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f'.{path.name}.{uuid.uuid4().hex[:8]}.tmp')
    tmp.write_text(json.dumps(value, indent=1, ensure_ascii=False, allow_nan=False) + '\n', encoding='utf-8')
    os.replace(tmp, path)


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def append_jsonl(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a', encoding='utf-8') as out:
        out.write(json.dumps(value, ensure_ascii=False) + '\n')
        out.flush()
        os.fsync(out.fileno())


def read_jsonl(path):
    path = Path(path)
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding='utf-8').splitlines():
        if line.strip():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                # An interrupted append leaves at most one incomplete final line.
                continue
    return rows


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def pid_alive(pid):
    if pid <= 0:
        return False
    if os.name == 'nt':
        kernel = ctypes.windll.kernel32
        handle = kernel.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            kernel.GetExitCodeProcess(handle, ctypes.byref(code))
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


class Store:
    def __init__(self, transcript_root, evidence_root):
        # Each caller owns its roots: 30-track (stage 06a), SG reels and DVD streams use separate stores and locks.
        self.root = Path(transcript_root)
        self.evidence = Path(evidence_root)

    # ----------------------------------------------------------------- paths
    def settings_dir(self, settings_id):
        return self.root / settings_id

    def source_dir(self, settings_id, source: Source):
        return self.settings_dir(settings_id) / 'sources' / Path(*source.key.split('/')) / source.identity_id

    def chunk_dir(self, settings_id, source, index):
        return self.source_dir(settings_id, source) / 'chunks' / f'c{index:05d}'

    # ----------------------------------------------------------------- settings
    def resolve_settings(self, settings):
        """The store directory for ``settings``.

        The existing directory whose transcript-defining settings match (the newest if several),
        else the settings' own identity for a new store. Execution-only settings such as the
        batch size are ignored, so every command finds a store whatever batch size made it.
        """
        from mission_asr import transcript_fields
        own = settings.settings_id
        if (self.settings_dir(own) / 'settings.json').is_file():
            return own
        wanted = settings.transcript_description()
        found = []
        for path in (sorted(self.root.glob('asr-*/settings.json')) if self.root.is_dir() else []):
            try:
                saved = read_json(path)
            except (OSError, json.JSONDecodeError):
                continue
            if transcript_fields(saved['settings']) == wanted:
                found.append((saved.get('created', ''), saved['settings_id']))
        return max(found)[1] if found else own

    def register_settings(self, settings_id, description):
        from mission_asr import transcript_fields
        path = self.settings_dir(settings_id) / 'settings.json'
        if path.is_file():
            saved = read_json(path)
            if transcript_fields(saved['settings']) != transcript_fields(description):
                raise ValueError(f'Settings {settings_id} were saved with different parameters')
            return
        write_json(path, dict(settings_id=settings_id, settings=description, created=now()))

    def register_source(self, settings_id, source: Source):
        path = self.source_dir(settings_id, source) / 'source.json'
        record = source.to_json()
        identity = source.identity()
        if path.is_file():
            if read_json(path)['identity'] != identity:
                raise ValueError(f'Identity directory collision for {source.key}')
            return
        write_json(path, dict(identity=identity, source=record, registered=now()))

    # ----------------------------------------------------------------- chunks
    def completed(self, settings_id, source, spec_index, nominal, verify=False):
        folder = self.chunk_dir(settings_id, source, spec_index)
        receipt_path = folder / 'chunk.json'
        if not receipt_path.is_file():
            return None
        try:
            receipt = read_json(receipt_path)
        except (OSError, json.JSONDecodeError):
            return None
        if (receipt.get('status') != 'complete' or receipt.get('schema') != CHUNK_SCHEMA
                or receipt.get('settings_id') != settings_id
                or receipt.get('identity_id') != source.identity_id
                or receipt.get('nominal_frames') != list(nominal)):
            return None
        if verify:
            data = (folder / 'segments.jsonl.gz').read_bytes()
            if sha256_bytes(data) != receipt['segments_sha256']:
                raise ValueError(f'Transcript chunk changed after publication: {folder}')
        return receipt

    def _write_temporary(self, final, settings_id, source, receipt, rows):
        final.parent.mkdir(parents=True, exist_ok=True)
        tmp = final.parent / f'.tmp-{final.name}-{uuid.uuid4().hex[:8]}'
        tmp.mkdir()
        try:
            payload = gzip.compress(''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in rows).encode('utf-8'),
                                    compresslevel=6, mtime=0)
            (tmp / 'segments.jsonl.gz').write_bytes(payload)
            receipt = dict(receipt, schema=CHUNK_SCHEMA, status='complete', settings_id=settings_id,
                           identity_id=source.identity_id, source_key=source.key,
                           segments_sha256=sha256_bytes(payload), published=now())
            write_json(tmp / 'chunk.json', receipt)
            return tmp, receipt
        except BaseException:
            shutil.rmtree(tmp, ignore_errors=True)
            raise

    def publish_chunk(self, settings_id, source, index, receipt, rows):
        """Write rows and receipt to a temporary directory, then rename it into place."""
        final = self.chunk_dir(settings_id, source, index)
        tmp, receipt = self._write_temporary(final, settings_id, source, receipt, rows)
        try:
            os.rename(tmp, final)
        except OSError:
            shutil.rmtree(tmp, ignore_errors=True)
            if (final / 'chunk.json').is_file():
                # Another worker published the same deterministic chunk first.
                return read_json(final / 'chunk.json')
            raise
        return receipt

    def replace_chunk(self, settings_id, source, index, receipt, rows):
        """Replace a published chunk: write the new one aside, move the old one out, rename.

        A crash between the two renames leaves ``.old-cNNNNN-*`` and no chunk;
        ``clean_temporaries`` puts the old chunk back.
        """
        final = self.chunk_dir(settings_id, source, index)
        tmp, receipt = self._write_temporary(final, settings_id, source, receipt, rows)
        old = final.parent / f'.old-{final.name}-{uuid.uuid4().hex[:8]}'
        try:
            os.rename(final, old)
        except BaseException:
            shutil.rmtree(tmp, ignore_errors=True)
            raise
        try:
            os.rename(tmp, final)
        except BaseException:
            os.rename(old, final)
            shutil.rmtree(tmp, ignore_errors=True)
            raise
        shutil.rmtree(old, ignore_errors=True)
        return receipt

    def clean_temporaries(self, settings_id):
        """Remove unpublished temporaries and restore a chunk whose replacement was interrupted."""
        removed = 0
        base = self.settings_dir(settings_id)
        if base.is_dir():
            for old in base.rglob('.old-c*'):
                final = old.parent / old.name.split('-')[1]
                if old.is_dir() and not final.exists():
                    os.rename(old, final)
                elif old.is_dir():
                    shutil.rmtree(old, ignore_errors=True)
            for tmp in base.rglob('.tmp-c*'):
                if tmp.is_dir():
                    shutil.rmtree(tmp, ignore_errors=True)
                    removed += 1
        return removed

    def chunk_rows(self, folder):
        with gzip.open(Path(folder) / 'segments.jsonl.gz', 'rt', encoding='utf-8') as stream:
            return [json.loads(line) for line in stream if line.strip()]

    def source_chunks(self, settings_id, source):
        base = self.source_dir(settings_id, source) / 'chunks'
        out = []
        if base.is_dir():
            for folder in sorted(base.glob('c[0-9]*')):
                receipt_path = folder / 'chunk.json'
                if receipt_path.is_file():
                    receipt = read_json(receipt_path)
                    if receipt.get('status') == 'complete' and receipt.get('identity_id') == source.identity_id:
                        out.append((folder, receipt))
        return out

    def identities(self, settings_id, key):
        folder = self.settings_dir(settings_id) / 'sources' / Path(*key.split('/'))
        return sorted(p.name for p in folder.iterdir() if p.is_dir()) if folder.is_dir() else []

    # ----------------------------------------------------------------- sessions
    def new_session(self, kind, record):
        session = datetime.now().strftime('%Y%m%d_%H%M%S') + '_' + uuid.uuid4().hex[:6]
        folder = self.evidence / 'sessions' / session
        write_json(folder / 'session.json', dict(record, session=session, kind=kind, started=now(),
                                                 started_ns=time.time_ns(),
                                                 pid=os.getpid(), status='running'))
        return session, folder

    def sessions(self):
        base = self.evidence / 'sessions'
        out = []
        if base.is_dir():
            for folder in sorted(base.iterdir()):
                path = folder / 'session.json'
                if path.is_file():
                    try:
                        record = read_json(path)
                    except (OSError, json.JSONDecodeError):
                        continue
                    record['progress'] = read_jsonl(folder / 'progress.jsonl')
                    record['failures'] = read_jsonl(folder / 'failures.jsonl')
                    record['folder'] = str(folder)
                    if record.get('status') == 'running' and not pid_alive(record.get('pid', -1)):
                        record['status'] = 'stopped'
                    out.append(record)
        # Folder names have one-second resolution; order by the recorded start.
        return sorted(out, key=lambda r: (r.get('started_ns', 0), r['folder']))

    @contextmanager
    def lock(self, name='transcribe'):
        path = self.evidence / f'{name}.lock'
        path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                break
            except FileExistsError:
                try:
                    holder = json.loads(path.read_text(encoding='utf-8'))
                except (OSError, json.JSONDecodeError):
                    holder = {}
                if pid_alive(int(holder.get('pid', -1))):
                    raise RuntimeError(f'Another {name} run holds {path} (pid {holder.get("pid")})')
                path.unlink(missing_ok=True)
        else:
            raise RuntimeError(f'Cannot acquire {path}')
        with os.fdopen(fd, 'w', encoding='utf-8') as out:
            out.write(json.dumps(dict(pid=os.getpid(), started=now())))
        try:
            yield path
        finally:
            path.unlink(missing_ok=True)

    def stop_requested(self):
        return (self.evidence / 'STOP').exists()


def wait_for(path, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if Path(path).exists():
            return True
        time.sleep(0.1)
    return False
