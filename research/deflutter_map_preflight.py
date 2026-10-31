"""Exact source-derived map preflight before any target validation outcomes.

Invalid controls are rejected, never clipped/refitted after map failure. This
module is not enabled in the frozen I016 real batch.
"""
import numpy as np
import hashlib
from pathlib import Path
from deflutter_real_experiment import make_map
from flutter_policy import LIMITS,shifted_curve


def required_curves(curve,amplitude):
    am=amplitude-np.median(amplitude)
    am*=np.std(curve)/max(np.std(am),1e-12)
    curves=dict(main=curve,no_motion=np.zeros_like(curve),wrong_sign=-curve,
                amplitude=np.clip(am,-.049,.049))
    curves.update({f'time_{s:g}':shifted_curve(curve,s) for s in LIMITS['wrong_time_seconds']})
    return curves


def preflight(curve,support,amplitude,sr,frames):
    curves=required_curves(curve,amplitude)
    stats={};errors={}
    for name,c in curves.items():
        try:
            _,_,stats[name]=make_map(c,support,sr,frames)
        except ValueError as exc:errors[name]=str(exc)
    return dict(passed=not errors,maps=stats,errors=errors,
        response_outcomes_accessed=False,refit_or_post_failure_clipping=False,
        control_construction='Existing AM normalization and +/-0.049 control bound before map anchoring; unchanged')


def select_preflight(scores,curves,sr,frames,progress=None):
    """Best calibration score among exact-map-valid variants, before outcomes.

    Invalid required controls are a source-derived physical failure, not a
    reason to rescale a map or inspect candidate validation alternatives.
    """
    records=[]
    for item in sorted(scores,key=lambda r:(r['calibration_loss'],r['key'])):
        curve,support,amplitude=curves[item['key']]
        result=preflight(curve,support,amplitude,sr,frames)
        record=dict(key=item['key'],calibration_loss=item['calibration_loss'],**result)
        records.append(record)
        if progress is not None:progress(records)
        if result['passed']:return item,records
    return None,records


def freeze_required_maps(directory,curve,support,amplitude,sr,frames,expected):
    """Save exact main/control grids before target validation, without refitting."""
    directory=Path(directory);controls=directory/'required_maps';controls.mkdir(exist_ok=False)
    entries={}
    for name,c in required_curves(curve,amplitude).items():
        _,grid,stats=make_map(c,support,sr,frames)
        if stats['positions_sha256']!=expected[name]['positions_sha256']:
            raise ValueError('Exact required map changed after preflight')
        path=directory/'challenger_map.npz' if name=='main' else controls/(name+'.npz')
        if path.exists():raise ValueError('Never overwrite a frozen map')
        temp=path.with_name(path.name+'.partial')
        with temp.open('xb') as stream:np.savez_compressed(stream,**grid)
        temp.replace(path)
        entries[name]=dict(file=path.relative_to(directory).as_posix(),grid_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),**stats)
    return dict(sample_rate=sr,frames=frames,maps=entries,refit=False,response_outcomes_accessed=False)


def load_required_positions(directory,entry,sr,frames):
    directory=Path(directory).resolve();path=(directory/entry['file']).resolve();path.relative_to(directory)
    if hashlib.sha256(path.read_bytes()).hexdigest()!=entry['grid_sha256']:
        raise ValueError('Frozen map file identity changed')
    with np.load(path) as grid:
        tt=grid['tt'];nominal=grid['nominal']
        if tt.ndim!=1 or nominal.shape!=tt.shape or not np.isfinite(tt).all() or not np.isfinite(nominal).all() or np.any(np.diff(tt)<=0) or np.any(np.diff(nominal)<=0):
            raise ValueError('Invalid exact frozen map coordinates')
        positions=np.interp(np.arange(frames)/sr,nominal,tt)*sr
    if not np.isfinite(positions).all() or np.any(np.diff(positions)<=0):raise ValueError('Invalid inverse frozen map')
    if hashlib.sha256(positions.tobytes()).hexdigest()!=entry['positions_sha256']:
        raise ValueError('Frozen inverse map differs; never reanchor or refit')
    return positions
