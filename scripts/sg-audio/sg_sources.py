"""Space-to-ground (SG) reels as transcription sources.

The SG reels are continuous space-to-ground recordings: air-to-ground speech
that corresponds to the TEC, with PAO announcements mixed in as interruptions.
Each reel is a source on its own reel clock (elapsed seconds from its first
sample). All channels are mixed with equal weight, as in the 572-AAA pilot.
Earlier code and the pilot called these "PAO reels".

``550-AAG`` in the same folder is the Apollo 11 landing and is excluded.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from audio_sources import PROCESSING_ROOT, Source, wav_layout

# Master copies on the network share, and the local working copy that is transcribed.
MASTER_ROOT = Path('N:/Projects/Apollo/Mission Data/Apollo 15/Audio/Internet Archive')
SG_ROOT = Path(os.environ.get('A15_SG_ROOT', 'F:/_repos/Apollo_15_data/raw/audio/Internet Archive'))
DATA_ROOT = PROCESSING_ROOT / 'sg'
TRANSCRIPT_ROOT = DATA_ROOT / 'timing_transcripts'
EVIDENCE_ROOT = DATA_ROOT / 'evidence' / 'timing_transcription'
# The 30-track store already holds checksums of these reels; reuse them instead of rehashing 128 GB.
LEGACY_EVIDENCE_ROOT = Path('D:/Apollo_15_30-Track/stage_evidence/timing_transcription')
NOT_APOLLO_15 = {'550-AAG'}


def sg_sources(root, names=None, hashes=None, errors=None):
    out = []
    for wav in sorted(Path(root).glob('*.wav')):
        reel = wav.stem
        if reel in NOT_APOLLO_15:
            continue
        if names and reel not in names and 'all' not in names:
            continue
        try:
            layout = wav_layout(wav)
            stat = wav.stat()
            checksum = hashes.get(wav) if hashes else ''
            out.append(Source(
                key=f'sg/{reel}', collection='sg', relpath=wav.name, path=str(wav.resolve()),
                sample_rate=layout.rate, frames=layout.frames, source_channels=layout.channels,
                mix='single_channel' if layout.channels == 1 else 'equal_weight_mean',
                sample_format=layout.description, time_domain='sg_reel_elapsed_seconds',
                content_sha256=checksum or '', hash_origin='computed' if checksum else 'pending',
                size=stat.st_size, mtime_ns=stat.st_mtime_ns, label=f'SG {reel}'))
        except (OSError, ValueError) as error:
            if errors is None:
                raise
            errors.append(dict(key=f'sg/{reel}', error=str(error)))
    return out




def _locked_by_writer(path: Path) -> bool:
    """True while another process holds the file open (e.g. a copy still writing it)."""
    if sys.platform != 'win32':
        return False
    import ctypes
    from ctypes import wintypes
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel32.CreateFileW.restype = wintypes.HANDLE
    GENERIC_READ, OPEN_EXISTING, INVALID = 0x80000000, 3, wintypes.HANDLE(-1).value
    handle = kernel32.CreateFileW(str(path), GENERIC_READ, 0, None, OPEN_EXISTING, 0, None)  # no sharing
    if handle == INVALID:
        return True
    kernel32.CloseHandle(handle)
    return False


def copy_status(local_root=SG_ROOT, master_root=MASTER_ROOT) -> dict[str, str]:
    """Reel -> 'complete' | 'copying' | 'missing' for every Apollo 15 master reel.

    A local reel is complete when its size equals the master's, its modification time
    matches the master's (copy tools set it when they finish), and no other process
    still has it open.
    """
    out = {}
    for master in sorted(Path(master_root).glob('*.wav')):
        reel = master.stem
        if reel in NOT_APOLLO_15:
            continue
        local = Path(local_root) / master.name
        if not local.is_file():
            out[reel] = 'missing'
            continue
        m, l = master.stat(), local.stat()
        same = l.st_size == m.st_size and abs(l.st_mtime - m.st_mtime) <= 2
        out[reel] = 'complete' if same and not _locked_by_writer(local) else 'copying'
    return out
