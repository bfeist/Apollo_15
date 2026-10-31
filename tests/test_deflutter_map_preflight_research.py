import sys
from pathlib import Path
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'research'))
from deflutter_map_preflight import preflight,select_preflight,freeze_required_maps,load_required_positions
from deflutter_real_experiment import make_map


def test_valid_preflight_preserves_exact_main_map_and_all_controls():
    t=np.arange(40*200)/200
    curve=.003*np.sin(2*np.pi*7.3*t);support=np.ones_like(t)
    result=preflight(curve,support,1+.1*np.sin(2*np.pi*5.7*t),4000,40*4000)
    assert result['passed'] and len(result['maps'])==7
    assert result['maps']['main']['positions_sha256']==make_map(curve,support,4000,40*4000)[2]['positions_sha256']
    assert not result['response_outcomes_accessed']


def test_local_anchoring_invalid_am_control_is_rejected_without_refitting():
    t=np.arange(300*200)/200;curve=.02*np.sin(2*np.pi*6.7*t)
    support=(t<100).astype(float)
    amplitude=np.ones_like(t);amplitude[t<100]=.1;amplitude[(t>40)&(t<42)]=2.1
    result=preflight(curve,support,amplitude,4000,300*4000)
    assert not result['passed']
    assert 'main' in result['maps'] and 'amplitude' in result['errors']
    assert '5%' in result['errors']['amplitude']
    assert not result['refit_or_post_failure_clipping']


def test_calibration_choice_filters_required_map_invalidity_before_any_outcome():
    t=np.arange(300*200)/200;support=(t<100).astype(float)
    amp=np.ones_like(t);amp[t<100]=.1;amp[(t>40)&(t<42)]=2.1
    curves=dict(invalid=(.02*np.sin(2*np.pi*6.7*t),support,amp),
                valid=(.003*np.sin(2*np.pi*7.3*t),support,1+.1*np.sin(2*np.pi*5.7*t)))
    scores=[dict(key='invalid',calibration_loss=.1),dict(key='valid',calibration_loss=.2)]
    selected,records=select_preflight(scores,curves,4000,300*4000)
    assert selected['key']=='valid'
    assert [r['passed'] for r in records]==[False,True]
    assert all(not r['response_outcomes_accessed'] and not r['refit_or_post_failure_clipping'] for r in records)


def test_saved_required_maps_reproduce_full_inverse_including_nonuniform_last_sample(tmp_path):
    t=np.arange(40*200)/200;curve=.003*np.sin(2*np.pi*7.3*t);support=np.ones_like(t)
    amp=1+.1*np.sin(2*np.pi*5.7*t);sr=8000;frames=40*sr+13
    check=preflight(curve,support,amp,sr,frames)
    manifest=freeze_required_maps(tmp_path,curve,support,amp,sr,frames,check['maps'])
    pos=load_required_positions(tmp_path,manifest['maps']['main'],sr,frames)
    assert np.array_equal(pos,make_map(curve,support,sr,frames)[0])
    assert len(manifest['maps'])==7 and not manifest['response_outcomes_accessed']
    path=tmp_path/manifest['maps']['main']['file'];path.write_bytes(path.read_bytes()+b'tampered')
    try:load_required_positions(tmp_path,manifest['maps']['main'],sr,frames)
    except ValueError as exc:assert 'identity changed' in str(exc)
    else:raise AssertionError('A changed frozen map was accepted')
