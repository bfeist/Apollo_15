#!/usr/bin/env python3
"""Archive complete concatenated tapes, one ZIP per tape (standard library only).

    uv run scripts/01a_zip_originals.py
    uv run scripts/01a_zip_originals.py --dry-run

Existing ZIPs are skipped. Completeness requires stage 01's shared time-range
marker AND matching outputs for every channel in the current delivery. Keep the
delivery available for verification; rerun stage 01 after downloads finish.
"""

import argparse
import math
import os
from pathlib import Path
import re
import runpy
import tempfile
import time
import wave
import zipfile


CONCAT = runpy.run_path(str(Path(__file__).with_name('01_concatenate.py')))


class DeferredTape(ValueError):
    """The tape is not ready to archive."""


def snapshot(paths, root):
    result = []
    for path in paths:
        stat = path.stat()
        result.append((path.relative_to(root).as_posix(), stat.st_size, stat.st_mtime_ns))
    return sorted(result)


def complete_tape(tape, delivery, min_age=60, range_tolerance=2):
    """Return archive members and a cheap, repeatable source/output snapshot."""
    if not delivery.is_dir():
        raise DeferredTape(f'delivery folder missing: {delivery}')
    members = sorted(path for path in tape.rglob('*') if path.is_file())
    if any(path.name.lower().endswith('.partial') for path in members):
        raise DeferredTape('concatenation has partial files')
    markers = [path for path in members if path.name.startswith('.timerange-')]
    if len(markers) != 1 or markers[0].parent != tape:
        raise DeferredTape('requires exactly one stage-01 shared time-range marker')
    groups = CONCAT['group_fragment_files'](delivery)
    if not groups:
        raise DeferredTape('no delivery WAVs found')
    sources = sorted(path for fragments in groups.values() for _, path in fragments)
    before_source = snapshot(sources, delivery)
    before_output = snapshot(members, tape)
    cutoff = time.time_ns() - min_age * 1_000_000_000
    if any(mtime > cutoff for _, _, mtime in before_source + before_output):
        raise DeferredTape('files were recently modified')
    expected = []
    ranges = []
    for base, fragments in groups.items():
        numbers = [number for number, _ in fragments]
        if numbers != list(range(len(numbers))):
            raise DeferredTape(f'missing or duplicate fragments: {base}')
        files = [path for _, path in fragments]
        time_range = CONCAT['fragment_time_range'](files)
        if time_range is None:
            raise DeferredTape(f'no fragment time range: {base}')
        output = tape / f'{base}_{time_range}.wav'
        if not CONCAT['output_matches_inputs'](files, output):
            raise DeferredTape(f'missing, truncated or outdated concatenation: {output.name}')
        expected.append(output)
        ranges.append(time_range)
    shared_range = CONCAT['tape_time_range'](ranges, range_tolerance)
    if shared_range is None or markers[0].name != f'.timerange-{shared_range}':
        raise DeferredTape('shared time-range marker does not match delivery')
    expected.append(markers[0])
    if set(members) != set(expected):
        raise DeferredTape('unexpected files (possibly obsolete concatenations) in tape folder')
    # Detect changes while inspecting headers, including newly arriving fragments.
    current_groups = CONCAT['group_fragment_files'](delivery)
    current_sources = sorted(path for fragments in current_groups.values() for _, path in fragments)
    current_members = sorted(path for path in tape.rglob('*') if path.is_file())
    if (snapshot(current_sources, delivery) != before_source
            or snapshot(current_members, tape) != before_output):
        raise DeferredTape('files changed during verification')
    return sorted(expected), (before_source, before_output)


def archive_tape(tape, delivery, output, dry_run=False, min_age=60,
                 range_tolerance=2, compression_level=6):
    """Return created, skipped, deferred, or failed; never replace an existing ZIP."""
    archive = output / f'{tape.name}.zip'
    if archive.exists():
        print(f'Skipping (exists): {archive}', flush=True)
        return 'skipped'
    temporary = None
    try:
        members, signature = complete_tape(tape, delivery, min_age, range_tolerance)
        size = sum(path.stat().st_size for path in members)
        if dry_run:
            print(f'Would archive {tape.name}: {len(members) - 1} WAVs, '
                  f'{size / 1024**3:.2f} GiB -> {archive}', flush=True)
            return 'ready'
        output.mkdir(parents=True, exist_ok=True)
        handle, name = tempfile.mkstemp(prefix=f'.{tape.name}-', suffix='.zip.partial', dir=output)
        temporary = Path(name)
        os.close(handle)
        print(f'Archiving {tape.name}: {len(members) - 1} WAVs, '
              f'{size / 1024**3:.2f} GiB -> {archive}', flush=True)
        with zipfile.ZipFile(temporary, 'w', compression=zipfile.ZIP_DEFLATED,
                             compresslevel=compression_level, allowZip64=True) as zipped:
            for path in members:
                zipped.write(path, arcname=f'{tape.name}/{path.relative_to(tape).as_posix()}')
        _, after = complete_tape(tape, delivery, min_age, range_tolerance)
        if signature != after:
            raise DeferredTape('files changed during archiving; rerun after concatenation finishes')
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
    except (ValueError, EOFError, wave.Error) as error:
        print(f'Deferred {tape.name}: {error}', flush=True)
        return 'deferred'
    except OSError as error:
        print(f'Failed {tape.name}: {error}', flush=True)
        return 'failed'
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, default=Path('D:/Apollo_15_30-Track/orig_concatenated'))
    parser.add_argument('--delivery', type=Path, default=Path('D:/Apollo_15_30-Track/orig_delivery'),
                        help='Stage-01 source fragments, required to verify every channel')
    parser.add_argument('--output', type=Path,
                        default=Path('O:/30-track-audio/Apollo_15_30-Track/originals'))
    parser.add_argument('--tape', help='Process only this tape folder, e.g. A15_T510073')
    parser.add_argument('--dry-run', action='store_true', help='Preview without writing files')
    parser.add_argument('--min-age', type=float, default=60,
                        help='Defer files modified within this many seconds (default: 60)')
    parser.add_argument('--range-tolerance', type=float, default=2,
                        help='Stage-01 endpoint tolerance in seconds (default: 2)')
    parser.add_argument('--compression-level', type=int, choices=range(10), default=6,
                        help='ZIP compression level: 0 fastest, 9 smallest (default: 6)')
    args = parser.parse_args()
    for flag in ('min_age', 'range_tolerance'):
        value = getattr(args, flag)
        if not math.isfinite(value) or value < 0:
            parser.error(f'--{flag.replace("_", "-")} must be finite and nonnegative')
    for folder in (args.input, args.delivery):
        if not folder.is_dir():
            parser.error(f'Folder does not exist: {folder}')
    for folder in (args.input, args.delivery):
        if args.output.resolve() == folder.resolve() or folder.resolve() in args.output.resolve().parents:
            parser.error('Output must be outside the concatenated and delivery folders')
    if args.tape and not re.fullmatch(r'A15_T\d+', args.tape):
        parser.error('--tape must be a tape folder name such as A15_T510073')
    tapes = sorted(path for path in args.input.iterdir()
                   if path.is_dir() and re.fullmatch(r'A15_T\d+', path.name)
                   and (not args.tape or path.name == args.tape))
    if not tapes:
        parser.error('No matching tape folders found')
    print(f'Found {len(tapes)} tape folder(s). Output: {args.output}', flush=True)
    counts = dict(created=0, skipped=0, deferred=0, failed=0, ready=0)
    for tape in tapes:
        status = archive_tape(tape, args.delivery / tape.name, args.output,
                              args.dry_run, args.min_age, args.range_tolerance,
                              args.compression_level)
        counts[status] += 1
    print('Finished scan. ' + ', '.join(f'{key}: {value}' for key, value in counts.items()))
    return 1 if counts['failed'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
