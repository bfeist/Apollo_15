"""Stable per-tape storage shared by planning and saved-map rendering."""
from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import re

DEFAULT_PLANNING_ROOT = Path('D:/Apollo_15_30-Track/deflutter_planning_v4')
DEFAULT_DENOISED_ROOT = Path('D:/Apollo_15_30-Track/denoised')
DEFAULT_DEFLUTTERED_ROOT = Path('D:/Apollo_15_30-Track/defluttered_v4')


def is_continuous_plan(plan):
    return plan.get('schema') == 2 and plan.get('kind') == 'continuous_reference'


def iter_plan_artifacts(folder, plan):
    """Yield all bound evidence/maps, including continuous binary streams."""
    folder = Path(folder).resolve()
    for summary in plan['regions']:
        if not summary.get('detail_file'):
            continue
        path, raw = checked_artifact(folder, summary['detail_file'], summary['detail_sha256'])
        yield path, summary['detail_sha256']
        detail = json.loads(raw)
        maps = detail.get('maps', {})
        if is_continuous_plan(plan):
            for row in maps.get('streams', {}).values():
                path = checked_stream(folder, row['file'], row['sha256'])
                if path.stat().st_size != row['frames']*16 or row.get('dtype') != '<f8':
                    raise ValueError('Continuous stream size or dtype changed')
                yield path, row['sha256']
            for row in detail.get('monitor_records', []):
                path, _ = checked_artifact(folder, row['file'], row['sha256'])
                yield path, row['sha256']
        elif maps:
            path, _ = checked_artifact(folder, maps['file'], maps['sha256'])
            yield path, maps['sha256']


def tape_directory(tape, root=DEFAULT_PLANNING_ROOT):
    if not isinstance(tape, str) or not re.fullmatch(r'[A-Za-z0-9_-]+', tape):
        raise ValueError('Tape must be a simple folder name')
    return Path(root)/tape


def planning_directory(tape, root=DEFAULT_PLANNING_ROOT, start=0, duration=None):
    folder = tape_directory(tape, root)
    if not math.isfinite(start) or start < 0 or (duration is not None and
            (not math.isfinite(duration) or duration <= 0)):
        raise ValueError('Invalid planning interval')
    if start != 0 or duration is not None:
        # Audits must never occupy the canonical whole-tape plan's filename.
        suffix = 'end' if duration is None else f'{duration:g}'
        folder = folder/'audits'/f'start_{start:g}_duration_{suffix}'
    return folder


def checked_artifact(folder, name, digest):
    path = (folder/name).resolve()
    if folder.resolve() not in path.parents:
        raise ValueError('Plan artifact must be inside its plan directory')
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError(f'Saved plan artifact changed: {name}')
    return path, raw


def checked_stream(folder, name, expected):
    """Validate large binary maps without allocating tape-length byte arrays."""
    path = (Path(folder)/name).resolve()
    if Path(folder).resolve() not in path.parents:
        raise ValueError('Plan artifact must be inside its plan directory')
    value = hashlib.sha256()
    with path.open('rb') as stream:
        while block := stream.read(4*1024*1024):
            value.update(block)
    if value.hexdigest() != expected:
        raise ValueError(f'Saved plan artifact changed: {name}')
    return path


def load_saved_plan(path, require_complete=True, *, expand_regions=True):
    """Verify every artifact; optionally keep the compact index for streaming jobs."""
    path = Path(path).resolve()
    if path.is_dir():
        path = path/'plan.json'
    plan = json.loads(path.read_text(encoding='utf-8'))
    if plan.get('schema') != 1 and not is_continuous_plan(plan):
        raise ValueError('Unsupported saved-plan schema')
    if require_complete and (not plan.get('complete') or plan.get('source_validation') != 'verified'):
        raise ValueError('Saved plan is incomplete; finish stage 05a first')
    survey = plan['inventory'].get('carrier_survey')
    if survey and hashlib.sha256(Path(survey['path']).read_bytes()).hexdigest() != survey['sha256']:
        raise ValueError('Original carrier survey changed since planning')
    # Validate original source identity without redoing reference analysis.
    for row in plan['inventory']['channels']+plan['inventory'].get('excluded_files', []):
        stat = Path(row['path']).stat()
        if stat.st_size != row['bytes'] or stat.st_mtime_ns != row['mtime_ns']:
            raise ValueError(f"Original source changed since planning: {row['path']}")
    for i, summary in enumerate(plan['regions']):
        if summary['status'] != 'analyzed':
            continue
        _, raw = checked_artifact(path.parent, summary['detail_file'], summary['detail_sha256'])
        detail = json.loads(raw)
        for key in ('id', 'start_seconds', 'end_seconds', 'analysis_start', 'analysis_end', 'maps'):
            if detail.get(key) != summary.get(key):
                raise ValueError('Saved region index and evidence disagree')
        if is_continuous_plan(plan):
            for artifact in detail.get('maps', {}).get('streams', {}).values():
                stream = checked_stream(path.parent, artifact['file'], artifact['sha256'])
                if stream.stat().st_size != artifact['frames']*16 or artifact.get('dtype') != '<f8':
                    raise ValueError('Continuous stream size or dtype changed')
            for artifact in detail.get('monitor_records', []):
                checked_artifact(path.parent, artifact['file'], artifact['sha256'])
        elif detail.get('maps'):
            checked_artifact(path.parent, detail['maps']['file'], detail['maps']['sha256'])
        detail.update(detail_file=summary['detail_file'], detail_sha256=summary['detail_sha256'])
        if expand_regions:
            plan['regions'][i] = detail
    return plan


@contextmanager
def planning_lock(output):
    """One writer per destination; OS releases the lock after a crash."""
    output = Path(output).resolve()
    locks = output.parent/'.planning_locks'
    locks.mkdir(parents=True, exist_ok=True)
    path = locks/(output.name+'.lock')
    with path.open('a+b') as handle:
        if path.stat().st_size == 0:
            handle.write(b'0')
            handle.flush()
        handle.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise ValueError(f'Another planner is already using {output}') from exc
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == 'nt':
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
