import sys, numpy as np
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent/'fast'))
import fast_kernels as fk
n, sr = 136*8000, 8000.
t = np.arange(n)/sr
xp = np.arange(543)*.25+.25; fp = np.cumsum(np.random.default_rng(1).normal(0,.05,543))
ref = np.interp(t, xp, fp); got = fk.interp_uniform(n, sr, xp, fp); print('interp err', np.max(abs(ref-got)))
fpc = fp+1j*fp[::-1]; ref = np.interp(t, xp, fpc); got = fk.interp_uniform_complex(n, sr, xp, fpc); print('interp_c err', np.max(abs(ref-got)))
hz = 1234.5
ref = np.exp(2j*np.pi*hz*t)*np.exp(1j*np.interp(t, xp, fp)); got = fk.tone_piecewise(hz, n, sr, xp, fp); print('tone_piecewise err', np.max(abs(ref-got)))
x = np.random.default_rng(2).normal(0,.01,n); m = x*.1; osc = fk.tone(hz, n, sr); starts = np.arange(0, n-4000+1, 2000); w = np.hanning(4000); w/=w.sum()
ref = np.einsum('ij,j->i', np.lib.stride_tricks.sliding_window_view(2*(x-m)*osc.conj(), 4000)[::2000], w)
got = fk.observations_residual(x, m, osc, starts, w); print('obs_residual rel err', np.max(abs(ref-got))/np.max(abs(ref)))
ref = np.einsum('ij,j->i', np.lib.stride_tricks.sliding_window_view(2*(x-m)*osc.conj(), 4000)[::2000], w)
got = fk.tone_observations(x, m, True, hz, sr, starts, w); print('tone_observations rel err', np.max(abs(ref-got))/np.max(abs(ref)))
ph = 2*np.pi*60*t+.3*np.sin(t)
ref = np.einsum('ij,j->i', np.lib.stride_tricks.sliding_window_view(2*x*np.exp(-1j*7*ph), 4000)[::2000], w)
got = fk.phase_observations(x, 7., ph, starts, w); print('phase_observations rel err', np.max(abs(ref-got))/np.max(abs(ref)))
mm = m.copy(); fk.accumulate_tone(mm, .3-.2j, hz, sr); print('accumulate_tone err', np.max(abs(mm-(m+np.real((.3-.2j)*np.exp(2j*np.pi*hz*t))))))
print('demodulate_tone err', np.max(abs(fk.demodulate_tone(x, hz, sr)-2*x*np.exp(-2j*np.pi*hz*t))))
Z = fk.harmonic_observations(x, ph, 3, 8, starts, w)
for j in range(3, 8):
    ref = np.einsum('ij,j->i', np.lib.stride_tricks.sliding_window_view(2*x*np.exp(-1j*j*ph), 4000)[::2000], w)
    print(f'harmonic_observations j={j} rel err', np.max(abs(ref-Z[j-3]))/np.max(abs(ref)))
centers = (starts+2000)/sr; g = np.random.rand(n)
js = np.array([3, 7, 20, 65]); cs = np.array([.3-.1j, .2j, -.1+.05j, .01+.02j]); pres = np.random.rand(4, len(centers))
mm = m.copy(); fk.accumulate_harmonics(mm, ph, g, js, cs, centers, pres, sr)
ref = m + g*sum(np.interp(t, centers, pres[a])*np.real(cs[a]*np.exp(1j*js[a]*ph)) for a in range(4))
print('accumulate_harmonics err', np.max(abs(mm-ref)))
