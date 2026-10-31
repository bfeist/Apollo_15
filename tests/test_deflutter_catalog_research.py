"""Scientific failure checks for the isolated research detector."""
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'research'))
from deflutter_catalog import CatalogConfig, contamination_mask, discover_ridges, matching_truth


def test_hum_does_not_suppress_close_valid_tone():
    sr=8000; t=np.arange(24*sr)/sr
    rng=np.random.default_rng(7)
    x=.5*np.cos(2*np.pi*120*t)+.01*np.cos(2*np.pi*129.3*t)+rng.normal(0,.0001,len(t))
    rows, accounting=discover_ridges(x,sr,CatalogConfig(resolutions=(2.,8.)))
    assert any(abs(r['carrier_hz']-129.3)<1 for r in rows)
    assert all(abs(r['carrier_hz']-round(r['carrier_hz']/60)*60)>.5 for r in rows)
    assert sum(r['contaminated'] for r in accounting)>0


def test_interrupted_tone_and_response_isolation():
    sr=8000;t=np.arange(40*sr)/sr
    x=.02*np.cos(2*np.pi*777*t)*((t>=6)&(t<20))+.1*np.cos(2*np.pi*2500*t)
    x+=np.random.default_rng(3).normal(0,.001,len(t))
    rows,accounting=discover_ridges(x,sr,CatalogConfig(resolutions=(2.,)))
    assert any(abs(r['carrier_hz']-777)<1 and r['end_seconds']<24 for r in rows)
    assert not any(2150<=r['carrier_hz']<=2850 for r in rows)
    assert sum(r['response_band_withheld'] for r in accounting)>0


def test_noise_and_hum_do_not_create_long_ridges():
    sr=8000;t=np.arange(24*sr)/sr
    x=np.random.default_rng(2).normal(0,.001,len(t))
    for h in range(1,50):x+=.1*np.cos(2*np.pi*60*h*t)
    rows,_=discover_ridges(x,sr)
    assert rows==[]


def test_mask_width_is_resolution_bound_not_five_hz_exclusion():
    mask,width=contamination_mask(np.array([120.,120.5,121.,124.9,125.]),8.,CatalogConfig())
    assert width==.5
    assert mask.tolist()==[True,True,False,False,False]


def test_short_tail_is_explicit_and_no_false_full_interval_recall():
    rows,accounts=discover_ridges(np.zeros(100),8000)
    assert not rows and all(r['status']=='insufficient_context' for r in accounts)
    result=matching_truth([dict(carrier_hz=777,start_seconds=0,end_seconds=3)],
                          [dict(hz=777,start=0,end=32)])
    assert result['recall']==0
