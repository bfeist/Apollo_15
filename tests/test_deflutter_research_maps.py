"""Research exact-map invariants at unsupported gaps and physical endpoints."""
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'research'))
from deflutter_real_experiment import make_map


def test_anchor_never_adds_local_correction_in_a_gap():
    sr=8000;frames=30*sr;tt=np.arange(30*200)/200
    support=np.ones(len(tt));support[(tt>=12)&(tt<17)]=0
    curve=.003+.005*np.sin(2*np.pi*6.5*tt)
    pos,grid,stats=make_map(curve,support,sr,frames)
    assert stats['unsupported_speed_max']==0
    assert np.all(grid['speed'][(grid['tt']>13)&(grid['tt']<16)]==0)
    assert np.all(np.diff(pos)>0)
    assert abs(pos[0])<1e-8 and abs(pos[-1]-(frames-1))<1e-8


def test_map_refuses_anchoring_over_the_speed_bound():
    sr=8000;frames=20*sr;curve=np.full(4000,-.049);curve[0:400]=.049
    try:
        make_map(curve,np.ones(4000),sr,frames)
    except ValueError as exc:
        assert 'speed bound' in str(exc)
    else:
        raise AssertionError('Unsafe exact map was silently accepted')
