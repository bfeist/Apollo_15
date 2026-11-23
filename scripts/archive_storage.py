"""Locations for the consolidated archive, including immutable pre-move receipts."""
import json
import os
import re
from pathlib import Path
from uuid import uuid4

from stage_storage import data_root

ROOT = data_root()
ARCHIVE = ROOT/'stage_evidence/archive'
SSD = Path('F:/Apollo_15_30-Track' if os.name == 'nt' else '/mnt/f/Apollo_15_30-Track')
INTERNAL = {'capture_alignment_recovery', 'crosstalk_detection', 'crosstalk_qualification',
            'crosstalk_removed_archive', 'deflutter_analysis', 'deflutter_planning_qualified'}
PRIOR_AUDIO = ARCHIVE/'crosstalk_removed_archive/repeat-v2'
RETIRED = ROOT/'_to_delete/replaced_stage_results'


def relocated(path):
    path = Path(path)
    if path.is_relative_to(ROOT):
        parts = path.relative_to(ROOT).parts
        if parts and parts[0] in INTERNAL:
            path = ARCHIVE.joinpath(*parts)
    if path.is_relative_to(PRIOR_AUDIO):
        previous = RETIRED/'crosstalk'/path.relative_to(PRIOR_AUDIO)
        if not path.exists() and previous.exists():
            return previous
    return path


def publish_audio(tape, release_path):
    """Expose the completed stage-04 edition and park a superseded baseline on the same disk."""
    import _winapi
    if not re.fullmatch(r'A15_T\d+', tape):
        raise ValueError(tape)
    release_path = Path(release_path).resolve(strict=True)
    if (release_path.name != 'complete.json' or release_path.parent.parent != ARCHIVE/'crosstalk_qualification'):
        raise ValueError(f'Unexpected stage-04 release: {release_path}')
    release = json.loads(release_path.read_bytes())
    source = ARCHIVE/'crosstalk_removed_archive'/release_path.parent.name/tape
    rows = [row for row in release['results'] if row['tape'] == tape]
    if (release['status'] != 'complete' or not rows or
            relocated(release['output_root']).resolve(strict=True) != source.parent or
            source.resolve(strict=True) != source or
            {p.name for p in source.glob('CH*')} != {f"CH{r['channel']:02d}" for r in rows}):
        raise ValueError(f'Incomplete stage-04 release for {tape}')
    for row in rows:
        channel = source/f"CH{row['channel']:02d}"
        record = json.loads((channel/'crosstalk.json').read_bytes())
        if record['audio']['sha256'] != row['sha256'] or not (channel/'crosstalk.wav').is_file():
            raise ValueError(f'Unverified channel receipt: {channel}')
    output = ROOT/'crosstalk_removed'
    output.mkdir(exist_ok=True)
    if output.resolve() != output:
        raise ValueError(output)
    link = output/tape
    if link.exists() and (not link.is_junction() or link.resolve() not in (source, PRIOR_AUDIO/tape)):
        raise ValueError(f'Unexpected current stage-04 output: {link}')
    if link.resolve() != source:
        token = uuid4().hex
        staged, backup = output/('.new-'+token), output/('.previous-'+token)
        _winapi.CreateJunction(str(source), str(staged))
        if link.exists():
            link.rename(backup)
        try:
            staged.rename(link)
        except OSError:
            if backup.exists():
                backup.rename(link)
            raise
        finally:
            if staged.is_junction():
                staged.rmdir()
        if backup.is_junction():
            backup.rmdir()
    old = PRIOR_AUDIO/tape
    if source != old and old.exists():
        dest = RETIRED/'crosstalk'/tape
        dest.parent.mkdir(parents=True, exist_ok=True)
        if old.resolve() != old or dest.resolve() != dest or dest.exists() or link.resolve() != source:
            raise ValueError(f'Unexpected retirement path for {tape}')
        old.rename(dest)


def compatible_path(saved, current):
    if isinstance(saved, str) and isinstance(current, str) and saved != current:
        path = relocated(saved)
        if path.is_absolute() and path.is_file() and path.resolve() == relocated(current).resolve():
            return saved
    return current


def configure(module):
    if not (ARCHIVE/'layout.json').is_file():
        return
    name = module.__name__
    if name == 'denoise_archive':
        original = module.bound_path
        module.bound_path = lambda item: relocated(original(item))
    elif name == 'flutter_plan_store':
        original = module.planning_lock
        def planning_lock(output):
            path = Path(output).resolve()
            return original(ARCHIVE/'locks'/path.name if path.parent == ROOT else path)
        module.planning_lock = planning_lock
    elif name == 'flutter_analysis':
        module.DEFAULT_ANALYSIS_ROOT = ARCHIVE/'deflutter_analysis'
        original = module.assess
        def assess(*args, **kwargs):
            kwargs.setdefault('root', module.DEFAULT_ANALYSIS_ROOT)
            return original(*args, **kwargs)
        module.assess = assess
    elif name == 'shared_tape':
        module.DEFAULT_PLANNING_ROOT = ROOT/'deflutter_maps'
        original = module.qualified_candidates
        def candidates(*args, **kwargs):
            result = original(*args, **kwargs)
            for binding in result[2]:
                path = Path(binding['path'])
                if path.is_relative_to(ARCHIVE/'deflutter_analysis'):
                    binding['path'] = str(ROOT/path.relative_to(ARCHIVE))
            return result
        module.qualified_candidates = candidates
    elif name == 'repeat_archive':
        module.DATA = ARCHIVE/'crosstalk_detection'
        module.SSD = SSD/'work/crosstalk'
    elif name == 'repeat_render':
        module.CANONICAL = ARCHIVE/'denoised'
    elif name == 'repeat_retain':
        module.HOLD = SSD/'work/qualification'
    elif name == 'recover_capture_alignment':
        module.DATA = ARCHIVE
        module.OLD_ANALYSIS = ARCHIVE/'deflutter_analysis'/module.PRIOR
        module.OLD_PLANS = ARCHIVE/'deflutter_planning_qualified'/module.PRIOR
        module.REPORTS = ARCHIVE/'capture_alignment_recovery'
        tape_directory = module.tape_directory
        def publication_directory(root, tape):
            if root.name == 'previous_defluttered_shared' and root.parent.parent == module.REPORTS:
                root = RETIRED/'defluttered'/root.parent.name
            return tape_directory(root, tape)
        module.tape_directory = publication_directory
        immutable = module.immutable_json
        def immutable_json(path, value):
            if path.name == 'validation.json' and path.is_file():
                previous = json.loads(path.read_bytes())['inputs']
                aliases = {str(relocated(p)):p for p in previous}
                value = dict(value, inputs={aliases.get(p, p):v for p,v in value['inputs'].items()})
            return immutable(path, value)
        module.immutable_json = immutable_json
        recover = module.recover_stage04
        def recover_stage04(tape, *args, **kwargs):
            release = recover(tape, *args, **kwargs)
            publish_audio(tape, release)
            return release
        module.recover_stage04 = recover_stage04
    elif name == '05b_render_archive':
        module.DATA = ARCHIVE
        module.RELEASE = ARCHIVE/'crosstalk_qualification/repeat-v2/complete.json'
        module.PLANS = ARCHIVE/'deflutter_planning_qualified'/module.QUALIFIED
        module.WORK = SSD/'work/render/repeat-v2'/module.QUALIFIED
        read = module.read
        def read_record(path):
            record = read(path)
            if path.name == 'complete.json' and 'output_root' in record:
                record['output_root'] = str(relocated(record['output_root']))
            return record
        module.read = read_record
        verify = module.valid_result
        def valid_result(folder, summary):
            saved = read(folder/'review.json')
            expected = dict(summary)
            if 'shared_map' in expected:
                expected['shared_map'] = compatible_path(saved.get('shared_map'), expected['shared_map'])
            if 'denoised_intermediate' in expected:
                item = dict(expected['denoised_intermediate'])
                item['file'] = compatible_path(saved.get('denoised_intermediate', {}).get('file'), item['file'])
                expected['denoised_intermediate'] = item
            if 'input_bindings' in expected and len(expected['input_bindings']) == len(saved.get('input_bindings', [])):
                expected['input_bindings'] = [dict(b, path=compatible_path(a['path'], b['path']))
                                              for a,b in zip(saved['input_bindings'], expected['input_bindings'])]
            return verify(folder, expected)
        module.valid_result = valid_result
