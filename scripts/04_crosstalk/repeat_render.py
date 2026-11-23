"""Publish archive stage-04 products after the census, using one verified SSD tape per batch."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
import ctypes
from ctypes import wintypes
import json
from pathlib import Path
import uuid

from denoise_archive import CANONICAL, ORIGINALS, atomic_json, sha256
from flutter_planning import inventory
from flutter_plan_store import planning_lock
from crosstalk_stage import prepared_crosstalk, render_channel
from repeat_archive import SSD, stage_tape


@contextmanager
def read_lease(path):
    """Prevent writes, replacements and deletions while the verified tape is in use."""
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                       wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
    create.restype = wintypes.HANDLE
    close = kernel.CloseHandle
    close.argtypes = (wintypes.HANDLE,)
    handle = create(str(path), 0x80000000, 1, None, 3, 0x80, None)
    if handle == ctypes.c_void_p(-1).value:
        raise OSError(ctypes.get_last_error(), f'Cannot hold verified WAV for reading: {path}')
    try:
        yield
    finally:
        close(handle)


def render_archive(base, tapes, workers):
    output_root = CANONICAL.parent/'crosstalk_removed_archive'/base.name
    totals = []
    for tape in tapes:
        report = base/tape
        source_record = json.loads((report/'sources.json').read_bytes())
        rows = source_record['channels']
        inv = inventory(ORIGINALS/tape)
        canonical = {r['channel']:r for r in inv['channels']}
        if any(entry['row'] != canonical[entry['row']['channel']] for entry in rows):
            raise ValueError(f'Canonical inventory changed for {tape}')
        scratch = SSD/base.name/tape
        with planning_lock(scratch), ExitStack() as stack:
            verified = {}
            pending = [e for e in rows if not (output_root/tape/Path(e['name']).stem).exists()]
            if pending:
                for entry in rows:
                    src = Path(entry['source'])
                    stack.enter_context(read_lease(src))
                    # A resumed staged copy is reusable only after the current
                    # original bytes have also been checked under this lease.
                    if (scratch/entry['name']).exists() and sha256(src) != entry['audio']['sha256']:
                        raise ValueError(f'Changed denoised WAV {src}')
                stage_tape(tape, scratch, report)
                for entry in rows:
                    path = scratch/entry['name']
                    stack.enter_context(read_lease(path))
                    digest = sha256(path)
                    if digest != entry['audio']['sha256']:
                        raise ValueError(f'Staged WAV changed {path}')
                    verified[Path(entry['source']).resolve()] = digest, path

            def lookup(path, digest):
                checked, staged = verified[path]
                if checked != digest:
                    raise ValueError(f'Changed denoised manifest for {path}')
                return staged

            def publish(entry):
                row = entry['row']
                ch = row['channel']
                stem = f'CH{ch:02d}'
                final = output_root/tape/stem
                catalog = report/'catalog'/stem
                if final.exists():
                    path, saved = prepared_crosstalk(row, final)
                    if sha256(path) != saved['audio']['sha256']:
                        raise ValueError(f'Changed published result {path}')
                else:
                    attempt = final.parent/'.attempts'/(stem+'-'+uuid.uuid4().hex)
                    saved = render_channel(inv, ch, CANONICAL, attempt, catalog, progress=lambda _:None,
                                           verified_audio=lookup)
                    if not saved['applied_seconds'] and (saved['audio']['decoded_float32_sha256']
                            != entry['audio']['decoded_float32_sha256']):
                        raise ValueError(f'Bypass sample hash differs for {tape} {stem}')
                    # Only the destination-local evidence moves with publication.
                    evidence = [b for b in saved['input_bindings'] if Path(b['path']) == attempt/'evidence.json']
                    if len(evidence) != 1 or sha256(attempt/'evidence.json') != evidence[0]['sha256']:
                        raise ValueError('Invalid stage-04 publication evidence')
                    evidence[0]['path'] = str(final/'evidence.json')
                    atomic_json(attempt/'crosstalk.json', saved)
                    attempt.rename(final)
                    _, saved = prepared_crosstalk(row, final)
                if (saved['sources'][str(ch)]['audio_sha256'] != entry['audio']['sha256']
                        or not any(Path(b['path']) == catalog/'reviews.json' for b in saved['input_bindings'])):
                    raise ValueError(f'Wrong archive catalog or audio binding {final}')
                print(f'Published {tape} {stem} {saved["applied_seconds"]:.3f}s', flush=True)
                return dict(tape=tape, channel=ch, frames=row['frames'], applied_seconds=saved['applied_seconds'],
                            eligible_seconds=saved['eligible_seconds'], direction=saved['direction'],
                            applied_intervals=sum(e['status']=='applied' for e in saved['events']),
                            sha256=saved['audio']['sha256'])

            with ThreadPoolExecutor(max_workers=workers) as pool:
                totals.extend(pool.map(publish, rows))
            stack.close()
            if pending:
                for entry in rows:
                    (scratch/entry['name']).unlink()
                (scratch/'sources.json').unlink()
                scratch.rmdir()
        atomic_json(base/'render_progress.json', dict(output_root=str(output_root), channels=totals))
    atomic_json(base/'complete.json', dict(status='complete', tapes=len(tapes), channels=len(totals),
                applied_seconds=sum(r['applied_seconds'] for r in totals),
                applied_channels=sum(r['applied_seconds']>0 for r in totals),
                applied_intervals=sum(r['applied_intervals'] for r in totals), output_root=str(output_root),
                results=totals))
