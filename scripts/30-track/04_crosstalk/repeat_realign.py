"""Requalify stage-04 donors against measured capture origins, reusing target-only scans."""
import json
from pathlib import Path

from repeat_archive import DATA, HERE, SSD, run_jobs, stage_tape
from repeat_cluster import complete
from repeat_qualification import frozen_qualification, immutable_copy, qualify_tape
from repeat_archive import snapshot_sources
from repeat_render import render_archive
from denoise_archive import atomic_json, sha256
from flutter_plan_store import planning_lock
from shared_tape import bound, verify_bindings


def immutable_json(path, value):
    if path.exists():
        if json.loads(path.read_bytes()) != value:
            raise ValueError(f'Existing artifact differs: {path}')
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_json(path,value)


def recover_stage04(tape, inv, name, local, genus):
    old = DATA/'repeat-v2'/tape
    original_spec = json.loads((old/'sources.json').read_bytes())
    prior = DATA.parent/'crosstalk_qualification/repeat-v2'/tape
    raw_root, qual_root = DATA/name, DATA.parent/'crosstalk_qualification'/name
    raw, qual, scratch = raw_root/tape, qual_root/tape, SSD/name/tape
    with planning_lock(raw_root/'release'):
        snapshot_sources(raw_root/'implementation',immutable_copy)
        implementation = frozen_qualification(qual_root,raw_root)
        release = qual_root/'complete.json'
        if release.exists():
            saved = json.loads(release.read_bytes())
            if (saved['status']!='complete' or {r['tape'] for r in saved['results']} != {tape}
                    or not (qual/'qualified.json').is_file()):
                raise ValueError(f'Incomplete aligned release {release}')
            verify_bindings([json.loads((qual/'sources.json').read_bytes())['alignment']])
            return release
        if not complete(old):
            raise ValueError(f'Source scan release is incomplete: {old}')
        shifts = {str(r['channel']): round((r['start_seconds']-r['acquisition_start_seconds'])*r['sample_rate'])
                  for r in inv['channels']}
        alignment = {k:inv['alignment'][k] for k in ('path','sha256')}
        verify_bindings([alignment])
        spec = dict(original_spec, clock_shift_frames=shifts, alignment=alignment,
                    prior_catalog=str(prior/'catalog'), previous_scan_manifest=bound(old/'sources.json'))
        immutable_json(raw/'sources.json',spec)
        for entry in spec['channels']:
            ch = entry['row']['channel']
            stem = f'CH{ch:02d}'
            src = old/'scan'/(stem+'.npy')
            receipt = src.with_suffix('.receipt.json')
            old_receipt = json.loads(receipt.read_bytes())
            if (old_receipt['identity']!={'code':sha256(HERE/'repeat_scan.py'),'sources':spec['previous_scan_manifest']['sha256']}
                    or old_receipt['sha256'] != sha256(src)):
                raise ValueError(f'Target scan no longer matches the original denoised inputs: {src}')
            dst = raw/'scan'/src.name
            immutable_copy(src,dst)
            immutable_json(dst.with_suffix('.receipt.json'),dict(identity=dict(code=old_receipt['identity']['code'],
                sources=sha256(raw/'sources.json')), sha256=old_receipt['sha256'],
                reused_from=[bound(receipt),spec['previous_scan_manifest']]))
        with planning_lock(scratch):
            rows = stage_tape(tape,scratch,raw)
            if not complete(raw):
                run_jobs(tape,scratch,raw,rows,'validate',local,genus)
                files = {p.relative_to(raw).as_posix():sha256(p) for p in raw.rglob('*')
                         if p.is_file() and p.suffix in ('.json','.npy')}
                immutable_json(raw/'detected.json',dict(channels=len(rows),files=files))
            qualify_tape(raw,qual,implementation,staged=scratch)
        render_archive(qual_root,[tape],min(4,max(1,local)))
        return release
