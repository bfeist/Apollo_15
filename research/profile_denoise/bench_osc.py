import os
os.environ['OMP_NUM_THREADS']='1'
import time, numpy as np, numba

N = 136*8000
t = np.arange(N)/8000.
hz = 3420.0

@numba.njit(cache=True)
def tone(hz, n, sr, block=256):
    out = np.empty(n, dtype=np.complex128)
    w = np.exp(2j*np.pi*hz/sr)
    for start in range(0, n, block):
        z = np.exp(2j*np.pi*hz*(start/sr))
        stop = min(n, start+block)
        for i in range(start, stop):
            out[i] = z
            z = z*w
    return out

@numba.njit(cache=True)
def tone_sincos(hz, n, sr):
    out = np.empty(n, dtype=np.complex128)
    for i in range(n):
        a = 2*np.pi*hz*(i/sr)
        out[i] = complex(np.cos(a), np.sin(a))
    return out

@numba.njit(cache=True)
def harmonic_powers(base, j):
    """exp(1j*j*phase) given base=exp(1j*phase)."""
    out = base.copy()
    for _ in range(j-1):
        out *= base
    return out

@numba.njit(cache=True)
def fused_accumulate(model, coef, osc, gate, cgate):
    for i in range(len(model)):
        model[i] += gate[i]*cgate[i]*(coef.real*osc[i].real - coef.imag*osc[i].imag)

def bench(f, *a, reps=5):
    f(*a); t0=time.perf_counter()
    for _ in range(reps): r=f(*a)
    return (time.perf_counter()-t0)/reps, r

dt, ref = bench(lambda: np.exp(2j*np.pi*hz*t)); print(f'numpy exp        {dt*1e3:7.2f} ms')
dt, a = bench(tone, hz, N, 8000.); print(f'numba recurrence {dt*1e3:7.2f} ms  maxerr {np.max(abs(a-ref)):.2e}')
dt, b = bench(tone_sincos, hz, N, 8000.); print(f'numba sincos     {dt*1e3:7.2f} ms  maxerr {np.max(abs(b-ref)):.2e}')
phase = 2*np.pi*60*t + .01*np.sin(t)
base = np.exp(1j*phase)
dt, ref65 = bench(lambda: np.exp(1j*65*phase)); print(f'numpy exp(65*phase) {dt*1e3:7.2f} ms')
dt, p = bench(lambda: base**65); print(f'numpy base**65   {dt*1e3:7.2f} ms  maxerr {np.max(abs(p-ref65)):.2e}')
model = np.zeros(N); gate=np.ones(N); cg=np.ones(N); coef=0.3+0.2j
dt,_ = bench(lambda: model.__iadd__(gate*cg*np.real(coef*ref))); print(f'numpy accumulate {dt*1e3:7.2f} ms')
dt,_ = bench(fused_accumulate, model, coef, ref, gate, cg); print(f'numba fused acc  {dt*1e3:7.2f} ms')
centers = np.arange(543)*0.25+0.25; vals=np.random.rand(543)
dt,_ = bench(lambda: np.interp(t, centers, vals)); print(f'numpy interp     {dt*1e3:7.2f} ms')
