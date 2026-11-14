"""Aggregate model-window throughput for N concurrent single-thread workers."""
import os, sys, time, subprocess, json
from pathlib import Path
HERE = Path(__file__).resolve().parent

WORKER = r'''
import os
for n in ('OPENBLAS_NUM_THREADS','OMP_NUM_THREADS','MKL_NUM_THREADS'): os.environ[n]='1'
import sys, time
from pathlib import Path
HERE = Path(sys.argv[1]); backend = sys.argv[2]; reps = int(sys.argv[3]); barrier = float(sys.argv[4])
sys.path.insert(0, str(HERE.parents[1]/'scripts'))
from _bootstrap import install; install()
sys.path.insert(0, str(HERE/'fast'))
import numpy as np, soundfile as sf
from render_denoise_v2 import gather_seeds, saved_seeds
if backend == 'production':
    import denoise_v2_numba as dsp; dsp.warmup()
else:
    import denoise_v2_fast as dsp, fast_kernels; fast_kernels.warmup()
src = next(Path('D:/Apollo_15_30-Track/orig_concatenated/A15_T510077').glob('*_CH3_*.wav'))
rows = saved_seeds(Path('D:/Apollo_15_30-Track/denoised_v1/A15_T510077/CH03/cleanup.jsonl'), 0, sf.info(src).frames)
a, b = (2048-4)*8000, (2048+132)*8000
x, _ = sf.read(src, start=a, frames=b-a, dtype='float64'); seeds = gather_seeds(rows, a, b)
dsp.model_window(x[:20*8000], (), defer_compact=True)  # compile/warm
while time.time() < barrier: time.sleep(.01)
t0 = time.perf_counter()
for _ in range(reps): dsp.model_window(x, seeds, defer_compact=True)
print(time.perf_counter()-t0)
'''
def run(backend, n, reps):
    barrier = time.time()+25
    procs = [subprocess.Popen([sys.executable, '-c', WORKER, str(HERE), backend, str(reps), str(barrier)],
                              stdout=subprocess.PIPE, text=True) for _ in range(n)]
    times = [float(p.communicate()[0].strip().splitlines()[-1]) for p in procs]
    wall = max(times)
    return n*reps*64/wall  # new source-seconds per wall-second (64 s per model window)

counts = [int(v) for v in sys.argv[1].split(',')] if len(sys.argv) > 1 else [1, 6, 12]
reps = int(sys.argv[2]) if len(sys.argv) > 2 else 2
print(f'{"workers":>8s} {"production":>12s} {"fast":>12s}  (source-hours per wall-hour)')
for n in counts:
    r = {b: run(b, n, reps) for b in ('production', 'fast')}
    print(f'{n:8d} {r["production"]:12.1f} {r["fast"]:12.1f}', flush=True)
