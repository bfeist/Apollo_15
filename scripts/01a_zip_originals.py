#!/usr/bin/env python3
"""Archive concatenated tape folders, one ZIP per tape (standard library only).

    uv run scripts/01a_zip_originals.py
    uv run scripts/01a_zip_originals.py --dry-run

All files in each input folder are included recursively, without concatenation
validation or delivery dependencies. Existing ZIPs are skipped.
"""

import argparse
import os
from pathlib import Path
import tempfile
import zipfile


def archive_tape(tape, output, dry_run=False, compression_level=6):
    """Return created, skipped, ready, or failed; never replace an existing ZIP."""
    archive = output / f'{tape.name}.zip'
    if archive.exists():
        print(f'Skipping (exists): {archive}', flush=True)
        return 'skipped'
    temporary = None
    try:
        members = sorted(path for path in tape.rglob('*') if path.is_file())
        size = sum(path.stat().st_size for path in members)
        if dry_run:
            print(f'Would archive {tape.name}: {len(members)} files, '
                  f'{size / 1024**3:.2f} GiB -> {archive}', flush=True)
            return 'ready'
        output.mkdir(parents=True, exist_ok=True)
        handle, name = tempfile.mkstemp(prefix=f'.{tape.name}-', suffix='.zip.partial', dir=output)
        temporary = Path(name)
        os.close(handle)
        print(f'Archiving {tape.name}: {len(members)} files, '
              f'{size / 1024**3:.2f} GiB -> {archive}', flush=True)
        with zipfile.ZipFile(temporary, 'w', compression=zipfile.ZIP_DEFLATED,
                             compresslevel=compression_level, allowZip64=True) as zipped:
            for path in members:
                zipped.write(path, arcname=f'{tape.name}/{path.relative_to(tape).as_posix()}')
        try:
            # Windows rename is atomic and fails if the destination exists.
            # On POSIX, a hard link gives the same no-overwrite publication.
            if os.name == 'nt':
                os.rename(temporary, archive)
            else:
                os.link(temporary, archive)
        except FileExistsError:
            print(f'Skipping (created by another run): {archive}', flush=True)
            return 'skipped'
        print(f'Created: {archive}', flush=True)
        return 'created'
    except OSError as error:
        print(f'Failed {tape.name}: {error}', flush=True)
        return 'failed'
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, default=Path('D:/Apollo_15_30-Track/orig_concatenated'))
    parser.add_argument('--output', type=Path,
                        default=Path('O:/30-track-audio/Apollo_15_30-Track/originals'))
    parser.add_argument('--tape', help='Process only this tape folder, e.g. A15_T510073')
    parser.add_argument('--dry-run', action='store_true', help='Preview without writing files')
    parser.add_argument('--compression-level', type=int, choices=range(10), default=6,
                        help='ZIP compression level: 0 fastest, 9 smallest (default: 6)')
    args = parser.parse_args()
    if not args.input.is_dir():
        parser.error(f'Folder does not exist: {args.input}')
    if (args.output.resolve() == args.input.resolve()
            or args.input.resolve() in args.output.resolve().parents):
        parser.error('Output must be outside the concatenated folder')
    tapes = sorted(path for path in args.input.iterdir()
                   if path.is_dir() and (not args.tape or path.name == args.tape))
    if not tapes:
        parser.error('No matching tape folders found')
    print(f'Found {len(tapes)} tape folder(s). Output: {args.output}', flush=True)
    counts = dict(created=0, skipped=0, failed=0, ready=0)
    for tape in tapes:
        status = archive_tape(tape, args.output, args.dry_run, args.compression_level)
        counts[status] += 1
    print('Finished scan. ' + ', '.join(f'{key}: {value}' for key, value in counts.items()))
    return 1 if counts['failed'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
