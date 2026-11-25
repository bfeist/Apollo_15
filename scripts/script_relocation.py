"""Adapt hash-bound stage sources to the scripts/30-track/ folder move.

The stage folders 01-08 moved from scripts/ to scripts/30-track/ without any
byte changes, so every recorded implementation hash still matches. A few
sources located the repository from their own folder depth, and saved stage-04
evidence records the former absolute script paths. This module corrects both
when the modules load. It does not alter saved files.
"""
import json as _json
from pathlib import Path, PurePosixPath
import sys

SCRIPTS = Path(__file__).resolve().parent
REPO = SCRIPTS.parent
STAGES = SCRIPTS/'30-track'
MOVED = ('01_ingest', '02_alignment', '03_denoise', '04_crosstalk', '05_deflutter',
         '06_retime', '07_normalize', '08_irig')


def current_script(path):
    """Map a recorded pre-move script location to the same file in scripts/30-track/."""
    if not isinstance(path, str):
        return path
    candidate = Path(path)
    if not candidate.is_absolute() or not candidate.is_relative_to(SCRIPTS):
        return path
    parts = candidate.relative_to(SCRIPTS).parts
    if len(parts) < 2 or parts[0] not in MOVED:
        return path
    moved = STAGES.joinpath(*parts)
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
