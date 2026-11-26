"""Shared file fingerprint and the historical carrier-survey root.

The v5 planner thresholds, policy digests and per-channel approval machinery
were removed with that planner; saved v5 evidence is read by ``shared_tape``.
"""
import hashlib
from pathlib import Path

DEFAULT_SURVEY_ROOT = Path('D:/Apollo_15_30-Track/carrier_survey_v2')


def digest(path):
    value = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(4*1024*1024), b''):
            value.update(block)
    return value.hexdigest()
