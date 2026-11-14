import os
for n in ('OPENBLAS_NUM_THREADS','OMP_NUM_THREADS','MKL_NUM_THREADS'):
    os.environ[n]='1'
import sys, time, cProfile, pstats
from pathlib import Path
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]/'scripts'))
from _bootstrap import install; install()
import numpy as np, soundfile as sf
import denoise_v2_fast as fast, fast_kernels
from render_denoise_v2 import gather_seeds, saved_seeds
tape, ch = 'A15_T510077', 3
src = next(Path(f'D:/Apollo_15_30-Track/orig_concatenated/{tape}').glob(f'*_CH{ch}_*.wav'))
hint = Path(f'D:/Apollo_15_30-Track/denoised_v1/{tape}/CH{ch:02d}/cleanup.jsonl')
frames = sf.info(src).frames
rows = saved_seeds(hint, 0, frames)
fast_kernels.warmup()
a, b = (2048-4)*8000, (2048+132)*8000
x, _ = sf.read(src, start=a, frames=b-a, dtype='float64')
seeds = gather_seeds(rows, a, b)
fast.model_window(x, seeds, defer_compact=True)
pr = cProfile.Profile(); pr.enable(); t0=time.perf_counter()
fast.model_window(x, seeds, defer_compact=True)
pr.disable(); print(f'fast model_window {time.perf_counter()-t0:.2f}s')
st = pstats.Stats(pr); st.sort_stats('tottime').print_stats(28)

# Kernel-level breakdown
import collections, functools
timings = collections.defaultdict(lambda: [0, 0.])
def wrap(name, f):
    @functools.wraps(f)
    def g(*a, **k):
        t = time.perf_counter(); r = f(*a, **k); timings[name][0] += 1; timings[name][1] += time.perf_counter()-t
        return r
    return g
for name in dir(fast_kernels):
    f = getattr(fast_kernels, name)
    if hasattr(f, 'py_func'):
        setattr(fast_kernels, name, wrap(name, f))
fast.observations = fast_kernels.observations
for n in ('sosfiltfilt','spectrogram','butter'):
    setattr(fast.signal, n, wrap('scipy.'+n, getattr(fast.signal, n)))
t0=time.perf_counter(); fast.model_window(x, seeds, defer_compact=True); total=time.perf_counter()-t0
print(f'\ntotal {total:.2f}s')
for name,(n,t) in sorted(timings.items(), key=lambda kv:-kv[1][1]):
    print(f'{name:28s} {n:5d} calls {t:6.3f}s')
print(f'{"(python/numpy remainder)":28s}       {total-sum(t for _,t in timings.values()):6.3f}s')

pr = cProfile.Profile(); pr.enable()
fast.model_window(x, seeds, defer_compact=True)
pr.disable()
print('\n=== with kernels visible ===')
st = pstats.Stats(pr); st.sort_stats('tottime').print_stats(22)
