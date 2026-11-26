"""Locate stage modules without changing hash-bound processing source files.

The denoise runtime retains the historical scripts/research layout internally.
Only its locations of external review artifacts need adapting after relocation.
"""
import importlib.machinery
import importlib.util
import json
from pathlib import Path
import shlex
import sys

SCRIPTS = Path(__file__).resolve().parent
REPO = SCRIPTS.parent
LAYOUT = json.loads((SCRIPTS/'layout.json').read_text(encoding='utf-8'))
STAGES = SCRIPTS/'30-track'  # 30-track stage folders, moved from scripts/ on 2026-11-24 (renumbered 2026-11-26)
RUNTIME = STAGES/'03_denoise/runtime'
GENUS_SSH = 'bfeist@192.168.0.3'
LEGACY_GENUS_SSH = {'bfeist@192.168.0.4', 'bfeist@192.168.0.240'}  # earlier genus addresses


def genus_ssh(destination):
    return GENUS_SSH if destination in LEGACY_GENUS_SSH else destination


def route_ssh(argv):
    if isinstance(argv, (list, tuple)) and len(argv) >= 3 and argv[0] == 'ssh':
        destination = genus_ssh(argv[-2])
        if destination != argv[-2]:
            return [*argv[:-2], destination, argv[-1]]
    return argv


def source_path(name):
    """Resolve a registered command/module name, including its former path."""
    name = str(name).replace('\\', '/')
    if name.startswith('scripts/'):
        name = name[len('scripts/'):]
    if name in LAYOUT:
        return SCRIPTS/LAYOUT[name]
    # Also accept the explicit, current path relative to scripts/.
    if name in LAYOUT.values():
        return SCRIPTS/name
    raise ValueError(f'Unknown pipeline script: {name}')


def command(name, *arguments):
    """All child processes initialize the same stage layout."""
    source_path(name)  # Refuse unregistered scripts before spawning a child.
    return [sys.executable, '-u', str(SCRIPTS/'run.py'), str(name), *map(str, arguments)]


class _ArtifactLoader(importlib.machinery.SourceFileLoader):
    def exec_module(self, module):
        super().exec_module(module)
        if module.__name__ == 'denoise_v2_rollout':
            module.REPO = REPO
            module.INVENTORY = REPO/'reports/denoise_v2_rollout_20261108/inventory.json'
            module.REVIEW = REPO/'reports/denoise_v2_late_hum_20261108'
        else:
            from stage_storage import configure
            configure(module)
            from archive_storage import configure as configure_archive
            configure_archive(module)
            from script_relocation import configure as configure_relocation
            configure_relocation(module)


class _ArtifactFinder:
    """Preserve frozen code and hashes; resolve the same external artifacts."""
    def find_spec(self, fullname, path=None, target=None):
        if fullname not in {'denoise_v2_rollout', 'denoise_archive', 'denoise_v2_production',
                            'denoise_v2_cluster', 'shared_tape', 'flutter_analysis', 'flutter_plan_store',
                            'repeat_archive', 'repeat_render', 'repeat_retain',
                            'recover_capture_alignment', '05b_render_archive',
                            # Hash-bound sources that locate the repo from their own folder depth.
                            'crosstalk_stage', 'repeat_qualification', 'flutter_detection',
                            'flutter_qualification'}:
            return None
        filename = source_path(fullname+'.py')
        loader = _ArtifactLoader(fullname, str(filename))
        return importlib.util.spec_from_file_location(fullname, filename, loader=loader)


def install():
    paths = {str((SCRIPTS/relative).parent) for relative in LAYOUT.values()}
    # One frozen normalization snapshot serves both processing branches; the
    # runtime folder comes first, so it is the copy every stage imports.
    ordered = [str(RUNTIME/'scripts'), *sorted(paths-{str(RUNTIME/'scripts')})]
    for path in reversed(ordered):
        if path not in sys.path:
            sys.path.insert(0, path)
    if not any(isinstance(finder, _ArtifactFinder) for finder in sys.meta_path):
        sys.meta_path.insert(0, _ArtifactFinder())


class ScriptProcesses:
    """Dispatch frozen workers through the layout adapter in each new process."""
    def __init__(self, original):
        self.original = original

    def __getattr__(self, name):
        return getattr(self.original, name)

    def Popen(self, argv, *args, **kwargs):
        if (isinstance(argv, (list, tuple)) and len(argv) >= 3
                and argv[0] == sys.executable and argv[1] == '-u'):
            script = Path(argv[2])
            name = script.name
            if name in LAYOUT and script.absolute() == source_path(name):
                argv = command(name, *argv[3:])
        return self.original.Popen(route_ssh(argv), *args, **kwargs)

    def run(self, argv, *args, **kwargs):
        if isinstance(argv, (list, tuple)) and argv and argv[0] == 'ssh':
            words = shlex.split(argv[-1])
            for index, word in enumerate(words):
                prefix, separator, name = word.rpartition('/scripts/')
                if separator and name in LAYOUT:
                    words[index:index+1] = [prefix+'/scripts/run.py', name]
                    argv = [*argv[:-1], shlex.join(words)]
                    break
        return self.original.run(route_ssh(argv), *args, **kwargs)


install()
