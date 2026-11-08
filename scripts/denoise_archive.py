"""Version and relocation rules shared by archive writers and readers."""
from contextlib import contextmanager, nullcontext
import hashlib
import json
import os
from pathlib import Path
import stat

from flutter_plan_store import planning_lock

BASE = Path('D:/Apollo_15_30-Track')
ORIGINALS = BASE/'orig_concatenated'
CANONICAL = BASE/'denoised'
ARCHIVE = BASE/'denoised_v1'
CONTROL = BASE/'.denoise_v2_rollout'
MARKER = '.denoise_root.json'
TRANSACTION = 'migration.json'
SOURCE_KEYS = ('path', 'bytes', 'mtime_ns', 'frames', 'sample_rate', 'channel')


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def sync(stream):
    stream.flush()
    os.fsync(stream.fileno())


def atomic_json(path, value):
    pending = path.with_suffix(path.suffix+'.partial')
    with pending.open('w', encoding='utf-8') as stream:
        json.dump(value, stream, indent=2, allow_nan=False)
        sync(stream)
    pending.replace(path)


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


def exact_path(path):
    """Reject redirects, including junctions and symlinks in ancestors."""
    path = Path(path).absolute()
    if path.resolve() != path:
        raise ValueError(f'Redirected archive path: {path}')
    for entry in (path, *path.parents):
        try:
            info = entry.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ValueError(f'Reparse point in archive path: {entry}')
    return path


def root_marker(folder):
    for path in (folder, *folder.parents):
        marker = path/MARKER
        if marker.exists():
            return read_json(marker)
    return None


def require_closed_files(root, files):
    """On Windows, probe for open reader/writer handles before rotating a tree."""
    if os.name != 'nt':
        return
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                       wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
    create.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    invalid = ctypes.c_void_p(-1).value
    for name in files:
        path = root/name
        handle = create(str(path), 0x80000000, 0, None, 3, 0x80, None)
        if handle == invalid:
            raise OSError(ctypes.get_last_error(), f'Cannot exclusively open legacy file before cutover: {path}')
        kernel.CloseHandle(handle)


def cleanup_version(manifest):
    if manifest.get('schema') == 2:
        if manifest.get('cleanup_version') != 'v2' or manifest.get('action') not in {
                'denoise_v2', 'copy_original_timing'}:
            raise ValueError('Unknown schema-2 cleanup version or action')
        return 'v2'
    if manifest.get('schema') not in (None, 1) or manifest.get('cleanup_version', 'v1') != 'v1':
        raise ValueError('Unknown denoise manifest version')
    return 'v1'


def archive_lock(control=CONTROL):
    return planning_lock(exact_path(control)/'archive')


@contextmanager
def legacy_write(*paths):
    paths = [exact_path(p) for p in paths]
    protected = any(p == CANONICAL or CANONICAL in p.parents for p in paths)
    with (archive_lock() if protected else nullcontext()):
        for path in paths:
            if (any(root != path and path in root.parents for root in (CANONICAL, ARCHIVE, CONTROL, ORIGINALS))
                    or any(p.name.lower() in {'denoised_v1', '.denoise_v2_rollout', 'orig_concatenated'}
                        or p.name.lower().startswith('denoised_quarantine_')
                    for p in (path, *path.parents))
                    or root_marker(path) is not None
                    or (protected and (CONTROL/TRANSACTION).exists()
                        and read_json(CONTROL/TRANSACTION)['status'] != 'rolled_back')):
                raise ValueError(f'Legacy cleanup cannot write to {path}; use stage 03 v2 for the new root')
        yield


def historical_path(path, version='v1'):
    """Translate a known version binding. Callers still verify its saved identity."""
    path = Path(path).absolute()
    if version != 'v1' or CANONICAL not in path.parents and path != CANONICAL:
        return path
    record_path = CONTROL/TRANSACTION
    if not record_path.is_file():
        if (CANONICAL/MARKER).exists():
            raise ValueError('Missing v1 relocation record for the versioned root')
        return path
    record = read_json(record_path)
    if (record.get('from') != str(CANONICAL) or record.get('to') != str(ARCHIVE)
            or record.get('version') != 'v1'):
        raise ValueError('Invalid denoise relocation record')
    if record['status'] == 'rolled_back':
        return path
    if ARCHIVE.is_dir():
        return exact_path(ARCHIVE/path.relative_to(CANONICAL))
    if (CANONICAL/MARKER).exists() or record['status'] == 'complete':
        raise ValueError('Relocated v1 root is unavailable')
    return path


def bound_path(item):
    """Resolve saved review evidence by version and content, including old bindings."""
    path = Path(item['path'])
    version = item.get('cleanup_version')
    if version is None and path.name in {'denoised.wav', 'denoised.json', 'cleanup.jsonl'}:
        version = 'v1'
    return historical_path(path, version) if version else path


def validate_binding(folder, saved, row, offset, frames, version=None):
    """Validate denoise identity and audio header without decoding a whole channel."""
    import soundfile as sf

    actual = cleanup_version(saved)
    marker = root_marker(folder)
    if ((version is not None and actual != version)
            or (marker and (marker.get('cleanup_version') != actual
                            or marker.get('run_id') != saved.get('run_id')))):
        raise ValueError(f'Unexpected cleanup version or run: {folder}')
    signature = saved['signature']
    identity = {k: row[k] for k in SOURCE_KEYS}
    if (saved['status'] != 'complete' or signature['source'] != identity
            or signature['offset_frames'] != offset or signature['frames'] != frames):
        raise ValueError(f'Incomplete, unaligned or stale denoise source: {folder}')
    path = folder/'denoised.wav'
    info = sf.info(path)
    if (info.frames, info.samplerate, info.channels) != (frames, row['sample_rate'], 1):
        raise ValueError(f'Denoise audio header mismatch: {path}')
    if actual == 'v2':
        audio, source = saved['audio'], saved['source']
        current = path.stat()
        original = Path(row['path']).stat()
        expected_action = 'copy_original_timing' if row.get('role') == 'timing_only' else 'denoise_v2'
        if (saved['action'] != expected_action or any(source.get(k) != v for k, v in identity.items())
                or saved['source_frame_interval'] != [0, row['frames']]
                or saved['canonical_start_offset_frames'] != round(row.get('start_seconds', 0)*row['sample_rate'])
                or (original.st_size, original.st_mtime_ns) != (row['bytes'], row['mtime_ns'])
                or (audio['bytes'], audio['mtime_ns']) != (current.st_size, current.st_mtime_ns)
                or (audio['format'], audio['subtype']) != (info.format, info.subtype)
                or (audio['frames'], audio['sample_rate']) != (frames, row['sample_rate'])
                or (saved['action'] == 'denoise_v2' and info.subtype != 'FLOAT')):
            raise ValueError(f'V2 output identity mismatch: {path}')
    return path
