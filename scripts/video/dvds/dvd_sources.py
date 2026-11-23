"""DVD audio streams (Spacecraft Films MP4s) as transcription sources.

Each audio stream of an MP4 is its own source. Time is decoded stream elapsed
seconds counted from the first decoded sample. The stream presentation origin
is retained so program PTS can be restored.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from audio_sources import PROCESSING_ROOT, Source

DVD_ROOT = Path('D:/! Media Files/A15/Spacecraft Films')
DATA_ROOT = PROCESSING_ROOT / 'dvd'
TRANSCRIPT_ROOT = DATA_ROOT / 'timing_transcripts'
EVIDENCE_ROOT = DATA_ROOT / 'evidence' / 'timing_transcription'
LEGACY_EVIDENCE_ROOT = Path('D:/Apollo_15_30-Track/stage_evidence/timing_transcription')


def dvd_label(name):
    """``Apollo 15 - Disc 04 - Title 02 - Part 01.mp4`` -> ``D04-T02-P01``."""
    parts = []
    for word, letter in (('Disc', 'D'), ('Title', 'T'), ('Part', 'P'), ('Angle', 'A')):
        match = re.search(rf'{word} (\d+)', name)
        if match:
            parts.append(f'{letter}{int(match.group(1)):02d}')
    if not parts:
        raise ValueError(f'Unrecognized DVD file name: {name}')
    return '-'.join(parts)


def probe_streams(path):
    data = json.loads(subprocess.check_output([
        'ffprobe', '-v', 'error', '-select_streams', 'a', '-show_entries',
        'stream=index,codec_name,sample_rate,channels,start_time,duration,duration_ts,time_base:stream_tags=language,title',
        '-of', 'json', str(path)], stderr=subprocess.PIPE))
    return data.get('streams', [])


def dvd_sources(root, patterns=None, hashes=None, errors=None):
    out = []
    for media in sorted(Path(root).glob('*.mp4')):
        try:
            label = dvd_label(media.name)
        except ValueError as error:
            if errors is not None:
                errors.append(dict(key=f'dvd/{media.name}', error=str(error)))
            continue
        if patterns and 'all' not in patterns and not any(
                re.search(p, label) or re.search(p, media.name) for p in patterns):
            continue
        try:
            streams = probe_streams(media)
            if not streams:
                if errors is not None:
                    errors.append(dict(key=f'dvd/{label}', error='no audio stream', skipped=True))
                continue
            stat = media.stat()
            checksum = hashes.get(media) if hashes else ''
            for position, stream in enumerate(streams):
                rate = int(stream['sample_rate'])
                duration = float(stream.get('duration') or 0)
                # Exact decoded length is established by decoding; ffprobe's duration
                # is the container's declaration and is refined in ``measure_decoded``.
                frames = round(duration * rate)
                out.append(Source(
                    key=f'dvd/{label}/a{position}', collection='dvd', relpath=media.name,
                    path=str(media.resolve()), sample_rate=rate, frames=frames,
                    source_channels=int(stream['channels']),
                    mix='single_channel' if int(stream['channels']) == 1 else 'equal_weight_mean',
                    stream=position, container='ffmpeg', sample_format=stream.get('codec_name', ''),
                    time_domain='decoded_stream_elapsed_seconds; program PTS = pts_origin + elapsed',
                    content_sha256=checksum or '', hash_origin='computed' if checksum else 'pending',
                    size=stat.st_size, mtime_ns=stat.st_mtime_ns,
                    pts_origin_s=float(stream.get('start_time') or 0), label=f'DVD {label} a:{position}'))
        except (OSError, ValueError, subprocess.CalledProcessError) as error:
            if errors is None:
                raise
            errors.append(dict(key=f'dvd/{label}', error=str(error)))
    return out


def measure_decoded(source: Source):
    """Count decoded samples of a compressed stream once; the declared length can differ."""
    process = subprocess.Popen(['ffmpeg', '-v', 'error', '-nostdin', '-i', source.path, '-map', f'0:a:{source.stream}',
                                '-f', 'f32le', '-acodec', 'pcm_f32le', 'pipe:1'],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    count = 0
    while chunk := process.stdout.read(1 << 22):
        count += len(chunk)
    if process.wait() != 0:
        raise ValueError(process.stderr.read().decode(errors='replace'))
    return count // (4 * source.source_channels)



def dvd_inventory(root, patterns, hash_cache=None, measure=True, errors=None):
    found = dvd_sources(root, patterns, hash_cache, errors)
    if measure:
        for source in found:
            cached = hash_cache.values.get(f'decoded|{source.content_sha256}|{source.stream}') if hash_cache else None
            if cached is None:
                cached = measure_decoded(source)
                if hash_cache is not None and source.content_sha256:
                    hash_cache.values[f'decoded|{source.content_sha256}|{source.stream}'] = cached
                    hash_cache.changed = True
            source.frames = int(cached)
    return found
