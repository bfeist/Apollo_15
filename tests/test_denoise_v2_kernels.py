"""Fused production kernels against independent NumPy calculations."""
from pathlib import Path
import sys

import numpy as np
import pytest
from numba.core.registry import CPUDispatcher

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from _bootstrap import install
install()
import fast_kernels as fk
import fast_sincos as fs


def projection(x, oscillator, starts, window):
    return np.array([np.sum(2*x[s:s+len(window)]*oscillator[s:s+len(window)].conj()*window)
                     for s in starts])


def test_denoise_every_kernel_uses_strict_single_thread_arithmetic():
    for module in (fk, fs):
        for value in vars(module).values():
            if isinstance(value, CPUDispatcher):
                assert value.targetoptions['fastmath'] is False
                assert value.targetoptions['parallel'] is False


def test_denoise_uniform_interpolation_and_long_reseeded_tones():
    n, sr = 136*8000, 8000.
    t = np.arange(n)/sr
    xp = np.arange(543)*.25+.25
    fp = np.cumsum(np.random.default_rng(1).normal(0,.05,len(xp)))
    np.testing.assert_allclose(fk.interp_uniform(n,sr,xp,fp), np.interp(t,xp,fp), atol=1e-14,rtol=1e-14)
    fc = fp+1j*fp[::-1]
    np.testing.assert_allclose(fk.interp_uniform_complex(n,sr,xp,fc), np.interp(t,xp,fc), atol=1e-14,rtol=1e-14)
    hz = 1234.5
    osc = np.exp(2j*np.pi*hz*t)
    np.testing.assert_allclose(fk.tone(hz,n,sr),osc,atol=5e-10,rtol=0)
    np.testing.assert_allclose(fk.tone_piecewise(hz,n,sr,xp,fp),osc*np.exp(1j*np.interp(t,xp,fp)),atol=5e-10,rtol=0)


@pytest.mark.parametrize('strided', [False, True])
def test_denoise_observations_accumulation_and_demodulation(strided):
    rng = np.random.default_rng(2)
    n,sr,hz = 16001,8000.,1234.5
    x = rng.normal(0,.01,n*(2 if strided else 1))
    if strided: x=x[::2]
    m=x*.1; t=np.arange(n)/sr; osc=np.exp(2j*np.pi*hz*t)
    starts=np.arange(0,n-4000+1,2000); w=np.hanning(4000); w/=w.sum()
    expected=projection(x-m,osc,starts,w)
    np.testing.assert_allclose(fk.observations_residual(x,m,osc,starts,w),expected,atol=1e-14,rtol=1e-12)
    for use in (False,True):
        np.testing.assert_allclose(fk.tone_observations(x,m,use,hz,sr,starts,w),projection(x-m if use else x,osc,starts,w),atol=1e-13,rtol=1e-10)
    ph=2*np.pi*60*t+.3*np.sin(t)
    np.testing.assert_allclose(fk.phase_observations(x,7.,ph,starts,w),projection(x,np.exp(7j*ph),starts,w),atol=1e-13,rtol=1e-10)
    out=m.copy(); fk.accumulate_tone(out,.3-.2j,hz,sr)
    np.testing.assert_allclose(out,m+np.real((.3-.2j)*osc),atol=2e-12,rtol=0)
    np.testing.assert_allclose(fk.demodulate_tone(x,hz,sr),2*x*osc.conj(),atol=1e-12,rtol=0)
    np.testing.assert_allclose(fk.demodulate(x,osc),2*x*osc.conj(),atol=1e-15,rtol=0)


def test_denoise_harmonic_banks_match_individual_numpy_harmonics():
    rng=np.random.default_rng(5); n,sr=16001,8000.
    t=np.arange(n)/sr; phase=2*np.pi*60*t+.3*np.sin(t); x=rng.normal(0,.01,n)
    starts=np.arange(0,n-4000+1,2000); w=np.hanning(4000); w/=w.sum()
    z=fk.harmonic_observations(x,phase,3,8,starts,w)
    for j in range(3,8):
        np.testing.assert_allclose(z[j-3],projection(x,np.exp(1j*j*phase),starts,w),atol=1e-13,rtol=1e-10)
    centers=(starts+2000)/sr; gate=rng.random(n)
    js=np.array([3,7,20,65]); cs=np.array([.3-.1j,.2j,-.1+.05j,.01+.02j]); pres=rng.random((4,len(centers)))
    out=x.copy(); fk.accumulate_harmonics(out,phase,gate,js,cs,centers,pres,sr)
    expected=x+gate*sum(np.interp(t,centers,pres[a])*np.real(cs[a]*np.exp(1j*js[a]*phase)) for a in range(4))
    np.testing.assert_allclose(out,expected,atol=2e-12,rtol=0)


def test_denoise_sincos_across_production_phase_range():
    phase=np.random.default_rng(11).uniform(-4e6,4e6,2000)
    np.testing.assert_allclose(fs.exp_1j_affine(1.,phase,0.),np.exp(1j*phase),atol=6e-16,rtol=0)
