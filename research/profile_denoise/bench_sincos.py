import sys, time, numpy as np
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent/'fast'))
import fast_sincos as fs, fast_kernels as fk
n=136*8000; t=np.arange(n)/8000
rng=np.random.default_rng(0)
for name, ph, scale in [('60Hz*65 harmonic', 2*np.pi*60*t+.3*np.sin(t), 65.), ('random 1e6', rng.uniform(-3e6,3e6,n), 1.), ('random small', rng.uniform(-4,4,n), 1.)]:
    ref = np.exp(1j*scale*ph)
    for f in (fs.exp_1j_affine, fs.exp_1j_affine_direct, fk.tone_phase):
        f(scale, ph, 0.); t0=time.perf_counter()
        for _ in range(10): r=f(scale, ph, 0.)
        ms=(time.perf_counter()-t0)/10*1e3
        err = abs(r-ref); print(f'{name:18s} {f.__name__:22s} {ms:6.2f} ms  max|err| {err.max():.2e}  ulps(max) {err.max()/2.2e-16:.1f}')
