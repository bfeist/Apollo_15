import sys
from pathlib import Path
import numpy as np
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'research'))
from deflutter_adaptive_source import derive_original,SourceWithheld


def test_clipped_original_is_withheld_before_any_transform_can_hide_clipping():
    x=np.zeros(100*8000);x[45*8000]=1.
    with pytest.raises(SourceWithheld,match='Original clipping'):derive_original(x,8000,40,60)
    assert x[45*8000]==1


def test_context_cannot_be_manufactured_by_padding():
    with pytest.raises(SourceWithheld,match='real source context'):derive_original(np.zeros(100*8000),8000,20,80)


def test_missing_pilot_returns_exact_original_core_without_touching_input():
    sr=8000;t=np.arange(100*sr)/sr
    x=.025*np.cos(2*np.pi*59.2*t+.05*np.sin(2*np.pi*6.5*t));before=x.copy()
    core,meta=derive_original(x,sr,40,60)
    assert np.array_equal(core,x[40*sr:60*sr]) and np.array_equal(x,before)
    assert meta['extraction']['status']=='pilot_unavailable_identity_extraction'
