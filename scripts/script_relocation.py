"""Adapt hash-bound stage sources to the scripts/30-track/ folder move.

The stage folders 01-08 moved from scripts/ to scripts/30-track/ without any
byte changes, so every recorded implementation hash still matches. A few
sources located the repository from their own folder depth, and saved stage-04
evidence records the former absolute script paths. This module corrects both
when the modules load. It does not alter saved files.

On 2026-11-26 the 30-track stages after deflutter were renumbered (06 transcribe,
07 retime, 08 normalize, 09 IRIG), a few modules changed owner, and the
space-to-ground sources moved under scripts/sg-audio/. ``RENAMED``
is the old-to-new record of those moves (paths relative to scripts/); every moved
file is byte-identical, so recorded implementation hashes still match.
"""
import json as _json
from pathlib import Path, PurePosixPath
import sys

SCRIPTS = Path(__file__).resolve().parent
REPO = SCRIPTS.parent
STAGES = SCRIPTS/'30-track'
MOVED = ('01_ingest', '02_alignment', '03_denoise', '04_crosstalk', '05_deflutter',
         '06_retime', '07_normalize', '08_irig')
# Old location -> new location, relative to scripts/. A trailing '/' maps a whole folder.
RENAMED = {
    '30-track/05_deflutter/carrier_survey.py': '30-track/09_irig/carrier_survey.py',
    '30-track/05_deflutter/irig_clock.py': '30-track/09_irig/irig_clock.py',
    '30-track/05_deflutter/flutter_policy.py': '30-track/09_irig/flutter_policy.py',
    'sg_transcript/md_common.py': '00_common/md_common.py',
    'sg_transcript/md_ai.py': '00_common/md_ai.py',
    '30-track/06_retime/06a_transcribe.py': '30-track/06_transcribe/06_transcribe.py',
    '30-track/06_retime/mission_sources.py': '30-track/06_transcribe/mission_sources.py',
    '30-track/06_retime/06b_retime.py': '30-track/07_retime/07b_retime.py',
    '30-track/06_retime/06c_sg_retime.py': '30-track/07_retime/07a_sg_retime.py',
    '30-track/06_retime/': '30-track/07_retime/',
    '30-track/07_normalize/07_normalize.py': '30-track/08_normalize/08_normalize.py',
    '30-track/07_normalize/': '30-track/08_normalize/',
    '30-track/08_irig/': '30-track/09_irig/',
    # Space-to-ground pipeline under one parent folder.
    'sg-audio/sg_sources.py': 'sg-audio/01_sg-internetarchive/sg_sources.py',
    'sg-audio/sg_transcribe.py': 'sg-audio/01_sg-internetarchive/sg_transcribe.py',
    'sg-audio/sg_drift_fit.py': 'sg-audio/03_sg-internetarchive-respeed/sg_drift_fit.py',
    'sg-audio/sg_respeed.py': 'sg-audio/03_sg-internetarchive-respeed/sg_respeed.py',
    'sg_nara/': 'sg-audio/01_sg-nara/',
    'sg_transcript/': 'sg-audio/02_sg-transcript/',
}


def renamed(relative):
    """The current location (relative to scripts/) of a recorded relative script path."""
    relative = PurePosixPath(relative).as_posix()
    if relative in RENAMED:
        return RENAMED[relative]
    for old, new in RENAMED.items():
        if old.endswith('/') and relative.startswith(old):
            return new+relative[len(old):]
    return relative


def current_script(path):
    """Map a recorded pre-move script location to the same file at its current location."""
    if not isinstance(path, str):
        return path
    candidate = Path(path)
    if not candidate.is_absolute() or not candidate.is_relative_to(SCRIPTS):
        return path
    parts = candidate.relative_to(SCRIPTS).parts
    if len(parts) < 2:
        return path
    recorded = '/'.join(parts)
    relative = renamed('30-track/'+recorded if parts[0] in MOVED else recorded)
    if relative == recorded:
        return path  # never moved: keep the recorded string exactly
    moved = SCRIPTS/relative
    return str(moved) if moved.is_file() else path


def _remap(value):
    if isinstance(value, dict):
        return {key: _remap(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_remap(item) for item in value]
    return current_script(value)


class _RelocatedJson:
    """Present saved script paths at their current location to one module."""
    def __getattr__(self, name):
        return getattr(_json, name)

    @staticmethod
    def loads(*args, **kwargs):
        return _remap(_json.loads(*args, **kwargs))

    @staticmethod
    def load(*args, **kwargs):
        return _remap(_json.load(*args, **kwargs))


def configure(module):
    name = module.__name__
    if name == 'crosstalk_stage':
        # Saved catalog bindings name the former repeat_catalog.py location. The
        # frozen-compiler substitution compares that path with this module's folder.
        module.json = _RelocatedJson()
    elif name == 'repeat_qualification':
        # Used only as HERE.parents[1] to find REPO/research/crosstalk_probe.py.
        module.HERE = REPO/'scripts'/module.HERE.name
    elif name == 'flutter_qualification':
        corpus = REPO/'reports'/'flutter_census_20261115'
        defaults = list(module.regression.__defaults__)
        defaults[0] = corpus
        module.regression.__defaults__ = tuple(defaults)
        module.DEFAULT_CORPUS = corpus
    elif name == 'flutter_detection':
        def windows_to_posix(path, connection):
            mapped = module.map_path(path, [(module.DATA.as_posix(), connection['data']),
                                            (REPO.as_posix(), connection['repo']),
                                            ('F:/', '/mnt/f')])
            return PurePosixPath(str(mapped).replace('\\', '/'))

        def local_command(session):
            return [sys.executable, '-u', str(SCRIPTS/'run.py'), '05_detect_flutter.py', 'worker',
                    '--run-dir', str(session.run_dir), '--cluster-dir', str(session.cluster),
                    '--host', 'windows']

        module.windows_to_posix = windows_to_posix
        module.local_command = local_command
