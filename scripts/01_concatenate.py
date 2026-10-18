#!/usr/bin/env python3
"""
WAV Fragment Concatenator

Concatenates split WAV files in Apollo tape folders with output organized
under D:/Apollo_15_30-Track/orig_concatenated, grouped by tape. Reads channel subfolders
recursively from D:/Apollo_15_30-Track/orig_delivery. Rerun after downloading finishes;
outputs with the expected WAV size are skipped; others are rebuilt.

Run from the repository root with uv (no third-party dependencies):
    uv run scripts/01_concatenate.py
Preview without writing output:
    uv run scripts/01_concatenate.py --dry-run
Rerun the first command after downloads finish to include all fragments.

Format: A15_T510073_HR2U_CH2_FD_OPS_62-14-36-00_62-15-06-00-0.wav
Groups by tape/head/channel; output names retain the combined start/end range.
A .timerange-<start>_<end> marker records the shared range in each tape folder.
"""

import argparse
import math
import os
import time
import re
from pathlib import Path
from typing import Dict, List, Tuple
import wave


# Match the complete range before looking for an optional fragment index.
# Otherwise a timestamp's last field can be mistaken for the index.
FRAGMENT_NAME = re.compile(
    r"^(?P<base>.+)_(?P<start>\d+-\d+-\d+-\d+)_"
    r"(?P<end>\d+-\d+-\d+-\d+)(?:-(?P<number>\d+))?\.wav$",
    re.IGNORECASE,
)


def extract_base_name(filename: str) -> str:
    """Return the channel identity, excluding fragment timestamps and index."""
    match = FRAGMENT_NAME.fullmatch(filename)
    return match['base'] if match else Path(filename).stem


def group_fragment_files(folder_path: Path) -> Dict[str, List[Tuple[int, Path]]]:
    """Group by tape/head/channel, ordered by the delivery fragment index."""
    groups: Dict[str, List[Tuple[int, Path]]] = {}
    for wav_file in sorted(folder_path.rglob('*')):
        if not wav_file.is_file() or wav_file.suffix.lower() != '.wav':
            continue
        match = FRAGMENT_NAME.fullmatch(wav_file.name)
        number = int(match['number'] or 0) if match else 0
        groups.setdefault(extract_base_name(wav_file.name), []).append((number, wav_file))
    for fragments in groups.values():
        fragments.sort(key=lambda item: item[0])
    return groups


def timestamp_seconds(value):
    day, hour, minute, second = map(int, value.split('-'))
    return ((day * 24 + hour) * 60 + minute) * 60 + second


def tape_time_range(ranges, tolerance):
    """Use the outer range when channel starts and ends agree within tolerance."""
    if not ranges or any(value is None for value in ranges):
        return None
    starts, ends = zip(*(value.split('_') for value in ranges))
    first = min(starts, key=timestamp_seconds)
    last = max(ends, key=timestamp_seconds)
    start_spread = timestamp_seconds(max(starts, key=timestamp_seconds)) - timestamp_seconds(first)
    end_spread = timestamp_seconds(last) - timestamp_seconds(min(ends, key=timestamp_seconds))
    if max(start_spread, end_spread) > tolerance:
        return None
    return f'{first}_{last}'


def fragment_time_range(files: List[Path]):
    """Validate chronological, adjoining ranges and return the combined range."""
    matches = [FRAGMENT_NAME.fullmatch(path.name) for path in files]
    if not any(matches):
        return None
    if not all(matches):
        raise ValueError('Mixed timestamped and untimestamped fragments')
    previous_end = None
    for match in matches:
        start = timestamp_seconds(match['start'])
        end = timestamp_seconds(match['end'])
        if end <= start or (previous_end is not None and start != previous_end):
            raise ValueError('Fragment time ranges are reversed, overlapping, or have a gap')
        previous_end = end
    return f"{matches[0]['start']}_{matches[-1]['end']}"


def concatenate_wav_files(input_files: List[Path], output_file: Path) -> None:
    """
    Concatenate multiple WAV files into a single output file.
    
    All input files must have the same audio parameters (channels, sample rate, etc.)
    """
    if not input_files:
        raise ValueError("No input files provided")
    
    # Open first file to get parameters
    with wave.open(str(input_files[0]), 'rb') as first_wav:
        params = first_wav.getparams()
        n_channels = params.nchannels
        sample_width = params.sampwidth
        framerate = params.framerate
    
    expected_bytes = sum(
        _audio_size(path) for path in input_files
    )
    if expected_bytes > 0xFFFFFFFF - 36:
        raise ValueError("Combined audio exceeds the standard WAV 4 GiB limit")

    # Create output file with the same parameters
    with wave.open(str(output_file), 'wb') as out_wav:
        out_wav.setnchannels(n_channels)
        out_wav.setsampwidth(sample_width)
        out_wav.setframerate(framerate)
        
        # Concatenate all input files
        for input_file in input_files:
            with wave.open(str(input_file), 'rb') as in_wav:
                # Verify parameters match
                in_params = in_wav.getparams()
                if (in_params.nchannels != n_channels or
                    in_params.sampwidth != sample_width or
                    in_params.framerate != framerate):
                    raise ValueError(
                        f"Audio parameters mismatch in {input_file.name}: "
                        f"Expected ({n_channels}, {sample_width}, {framerate}), "
                        f"Got ({in_params.nchannels}, {in_params.sampwidth}, {in_params.framerate})"
                    )
                
                # Copy audio frames
                remaining = in_wav.getnframes()
                frame_size = n_channels * sample_width
                while remaining:
                    frames = in_wav.readframes(min(remaining, 65536))
                    if not frames or len(frames) % frame_size:
                        raise ValueError(f"Incomplete WAV (still downloading?): {input_file}")
                    out_wav.writeframesraw(frames)
                    remaining -= len(frames) // frame_size


def _audio_size(path: Path) -> int:
    with wave.open(str(path), 'rb') as wav:
        return wav.getnframes() * wav.getnchannels() * wav.getsampwidth()


def source_signature(files):
    return [dict(name=p.name, size=p.stat().st_size, mtime_ns=p.stat().st_mtime_ns)
            for p in files]


def output_matches_inputs(input_files: List[Path], output_file: Path) -> bool:
    """Check headers and lengths without reading all audio or writing metadata."""
    audio_format = None
    total_frames = 0
    for path in input_files:
        with wave.open(str(path), 'rb') as wav:
            current_format = (wav.getnchannels(), wav.getsampwidth(), wav.getframerate())
            if audio_format is not None and current_format != audio_format:
                raise ValueError(f"Audio parameters mismatch in {path.name}")
            audio_format = current_format
            frames = wav.getnframes()
            # Seeking to the last declared frame checks for truncation cheaply,
            # even when source WAVs have extra metadata chunks in their headers.
            if frames:
                wav.setpos(frames - 1)
                if len(wav.readframes(1)) != current_format[0] * current_format[1]:
                    raise ValueError(f"Incomplete WAV (still downloading?): {path}")
            total_frames += frames
    channels, sample_width, rate = audio_format
    # Python's wave writer produces a 44-byte PCM header.
    expected_size = 44 + total_frames * channels * sample_width
    if not output_file.is_file() or output_file.stat().st_size != expected_size:
        return False
    try:
        with wave.open(str(output_file), 'rb') as wav:
            return (wav.getnchannels(), wav.getsampwidth(), wav.getframerate(),
                    wav.getnframes()) == (channels, sample_width, rate, total_frames)
    except (OSError, EOFError, wave.Error):
        return False


def process_tape_folder(input_folder: Path, output_folder: Path,
                        dry_run=False, min_age=60, strict=False,
                        range_tolerance=2.0) -> int:
    """Skip matching output sizes; rebuild other channels without tracking files."""
    if not math.isfinite(range_tolerance) or range_tolerance < 0:
        raise ValueError('Range tolerance must be finite and nonnegative')
    files_created = 0
    deferred = 0
    completed_ranges = []
    for base_name, fragments in group_fragment_files(input_folder).items():
        fragment_files = [path for _, path in fragments]
        output_file = output_folder / f"{base_name}.wav"
        temporary = output_file.with_suffix('.wav.partial')
        try:
            numbers = [number for number, _ in fragments]
            if numbers != list(range(len(numbers))):
                deferred += 1
                print(f"  Deferred (missing or duplicate fragments): {base_name}")
                continue
            time_range = fragment_time_range(fragment_files)
            output_stem = f"{base_name}_{time_range}" if time_range else base_name
            output_file = output_folder / f"{output_stem}.wav"
            temporary = output_file.with_suffix('.wav.partial')
            signature = source_signature(fragment_files)
            if any(time.time_ns() - item['mtime_ns'] < min_age * 1_000_000_000
                   for item in signature):
                deferred += 1
                print(f"  Deferred (recently modified): {base_name}")
                continue
            if output_matches_inputs(fragment_files, output_file):
                completed_ranges.append(time_range)
                print(f"  Skipping (expected size and audio format): {output_file.name}")
                continue
            if dry_run:
                print(f"  Would concatenate {len(fragments)} fragments: {output_file}")
                continue
            output_folder.mkdir(parents=True, exist_ok=True)
            print(f"  Concatenating {len(fragments)} fragments: {output_file.name}", flush=True)
            concatenate_wav_files(fragment_files, temporary)
            current_files = [p for _, p in group_fragment_files(input_folder).get(base_name, [])]
            if current_files != fragment_files or source_signature(fragment_files) != signature:
                raise ValueError("Source changed during concatenation; rerun after downloading")
            os.replace(temporary, output_file)
            completed_ranges.append(time_range)
            files_created += 1
            print(f"    Created: {output_file}")
        except (OSError, ValueError, EOFError, wave.Error) as error:
            deferred += 1
            print(f"    Deferred/error for {base_name}: {error}")
        finally:
            if not dry_run:
                temporary.unlink(missing_ok=True)
    if not dry_run and output_folder.exists():
        # Allow small endpoint differences without changing any channel's audio.
        shared_range = tape_time_range(completed_ranges, range_tolerance) if not deferred else None
        marker = output_folder / f".timerange-{shared_range}" if shared_range else None
        if marker is not None:
            marker.touch(exist_ok=True)
        for old_marker in output_folder.glob('.timerange-*'):
            if old_marker != marker and old_marker.is_file():
                old_marker.unlink()
    if strict and deferred:
        raise RuntimeError(f"{deferred} channel(s) deferred or failed; pipeline cannot continue")
    return files_created


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, default=Path('D:/Apollo_15_30-Track/orig_delivery'))
    parser.add_argument('--output', type=Path, default=Path('D:/Apollo_15_30-Track/orig_concatenated'))
    parser.add_argument('--dry-run', action='store_true', help='Preview without writing files')
    parser.add_argument('--min-age', type=float, default=60,
                        help='Defer fragments modified within this many seconds (default: 60)')
    parser.add_argument('--strict', action='store_true', help='Fail the pipeline if any channels are deferred')
    parser.add_argument('--range-tolerance', type=float, default=2.0,
                        help='Allowed spread in channel start/end times for the tape marker, in seconds (default: 2)')
    args = parser.parse_args()
    if not args.input.is_dir():
        parser.error(f"Input folder does not exist: {args.input}")
    if args.min_age < 0:
        parser.error('--min-age must be nonnegative')
    if not math.isfinite(args.range_tolerance) or args.range_tolerance < 0:
        parser.error('--range-tolerance must be finite and nonnegative')
    if args.output.resolve() == args.input.resolve() or args.input.resolve() in args.output.resolve().parents:
        parser.error('Output must be outside the input folder')
    tape_folders = sorted(p for p in args.input.iterdir()
                          if p.is_dir() and re.fullmatch(r'A15_T\d+', p.name))
    if args.strict and not tape_folders:
        parser.error("No tape folders found")
    print(f"Found {len(tape_folders)} tape folder(s)")
    print(f"Output: {args.output}")
    total = 0
    for folder in tape_folders:
        print(f"Processing: {folder.name}")
        total += process_tape_folder(folder, args.output / folder.name,
                                     args.dry_run, args.min_age, args.strict, args.range_tolerance)
    print(f"Finished scan. Created/rebuilt {total} concatenated file(s).")
    print('Rerun after downloads finish to include newly arrived fragments.')


if __name__ == '__main__':
    main()
