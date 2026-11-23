"""Stage-05b 30-track channels as transcription sources (stage 06a).

``tape``: completed stage-05b WAVs. The adjacent ``review.json`` supplies the
published audio checksum and the channel's canonical origin, which already
includes the stage-02 capture placement. Local frame ``n`` lies at canonical
post-05b tape second ``start_seconds + n / rate``. Add the origin once.

The generic source, reader and checksum code is in ``00_common/audio_sources.py``
and is re-exported here. Space-to-ground reels and DVD streams have their own
inventories in ``scripts/sg-audio`` and ``scripts/video/dvds``.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
from dataclasses import dataclass, field, asdict
from pathlib import Path

import numpy as np

from audio_sources import (HashCache, Reader, Source, WavLayout, canonical_json, decode_pcm, digest,  # noqa: F401
                           file_sha256, gpu_description, wav_layout)

DATA_ROOT = Path('D:/Apollo_15_30-Track' if os.name == 'nt' else '/mnt/d/Apollo_15_30-Track')
DEFAULT_ROOTS = {'tape': DATA_ROOT}
TRANSCRIPT_ROOT = DATA_ROOT / 'timing_transcripts'
EVIDENCE_ROOT = DATA_ROOT / 'stage_evidence' / 'timing_transcription'
DEFLUTTERED = 'defluttered_shared'
ORIGINALS = 'orig_concatenated'
COLLECTIONS = ('tape',)


def tape_sources(root, tapes=None, channels=None, errors=None):
    """Completed stage-05b channels with their review-bound origin and checksum."""
    base = Path(root) / DEFLUTTERED
    folders = sorted(p for p in base.glob('A15_T*') if p.is_dir())
    if tapes:
        folders = [p for p in folders if p.name in set(tapes)]
    out = []
    for folder in folders:
        for channel_dir in sorted(folder.glob('CH[0-9][0-9]')):
            channel = int(channel_dir.name[2:])
            if channels and channel not in channels:
                continue
            review_path, wav = channel_dir / 'review.json', channel_dir / 'deflutter.wav'
            try:
                raw = review_path.read_bytes()
                review = json.loads(raw)
                receipt = review['files']['deflutter.wav']
                if (review.get('status') != 'complete' or review.get('tape') != folder.name
                        or review.get('channel') != channel or not wav.is_file()):
                    raise ValueError('incomplete or mismatched stage-05b review')
                layout = wav_layout(wav)
                if (layout.frames, layout.rate) != (receipt['frames'], receipt['sample_rate']) or layout.channels != 1:
                    raise ValueError('WAV header disagrees with its review receipt')
                stat = wav.stat()
                source_record = review.get('source', {})
                out.append(Source(
                    key=f'tape/{folder.name}/CH{channel:02d}', collection='tape',
                    relpath=f'{DEFLUTTERED}/{folder.name}/{channel_dir.name}/deflutter.wav', path=str(wav.resolve()),
                    sample_rate=layout.rate, frames=layout.frames, source_channels=1, mix='single_channel',
                    sample_format=layout.description, canonical_origin_s=float(review['start_seconds']),
                    time_domain='stage_05b_local_elapsed_seconds; canonical = origin + local',
                    content_sha256=receipt['sha256'], hash_origin='stage_05b_review', size=stat.st_size,
                    mtime_ns=stat.st_mtime_ns, tape=folder.name, channel=channel,
                    review=dict(relpath=f'{DEFLUTTERED}/{folder.name}/{channel_dir.name}/review.json',
                                sha256=hashlib.sha256(raw).hexdigest(), map_sha256=review.get('map_sha256'),
                                plan_sha256=review.get('plan_sha256'),
                                acquisition_start_seconds=source_record.get('acquisition_start_seconds',
                                                                            source_record.get('start_seconds')),
                                source_filename=Path(source_record.get('path', '')).name,
                                crosstalk_removed=review.get('crosstalk_removed')),
                    label=f'{folder.name} CH{channel:02d}'))
            except (OSError, ValueError, KeyError) as error:
                if errors is None:
                    raise
                errors.append(dict(key=f'tape/{folder.name}/{channel_dir.name}', error=str(error)))
    return out


def original_archive_hours(root):
    """Channel-hours in the original 30-track archive, from WAV headers only."""
    base = Path(root) / ORIGINALS
    total, files, tapes = 0.0, 0, set()
    for wav in base.glob('A15_T*/*.wav'):
        try:
            layout = wav_layout(wav)
        except (OSError, ValueError):
            continue
        total += layout.frames / layout.rate / 3600
        files += 1
        tapes.add(wav.parent.name)
    return dict(channel_hours=total, files=files, tapes=len(tapes))


def inventory(roots=None, *, tapes=None, all_tapes=False, channels=None, errors=None, **_):
    roots = {**DEFAULT_ROOTS, **{k: Path(v) for k, v in (roots or {}).items() if v}}
    if tapes or all_tapes:
        return tape_sources(roots['tape'], None if all_tapes else tapes, channels, errors)
    return []
