import os
for n in ('OPENBLAS_NUM_THREADS','OMP_NUM_THREADS','MKL_NUM_THREADS'): os.environ[n]='1'
import sys, time, json
from pathlib import Path
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]/'scripts')); from _bootstrap import install; install()
sys.path.insert(0, str(HERE.parents[1]/'scripts/03_denoise/runtime/research')); sys.path.insert(0, str(HERE/'fast'))
import numpy as np
import denoise_v2 as reference, denoise_v2_fast as fast, fast_kernels
from qualify_denoise_numba import synthetic_cases
fast_kernels.warmup()
def strip(r):
    """Decision structure without floating coefficients."""
    return json.dumps(r, sort_keys=True, default=lambda v: None)
for name, samples, seeds in synthetic_cases():
    t=time.perf_counter(); a, ra = reference.model_window(samples, seeds, defer_compact=True); ta=time.perf_counter()-t
    t=time.perf_counter(); b, rb = fast.model_window(samples, seeds, defer_compact=True); tb=time.perf_counter()-t
    kinds = lambda r: sorted((c['kind'], round(c['hz'],2)) for c in r['components'])
    passes = lambda r: [len(v.get('passes',[])) for v in r.get('refinement',[])]
    d = a-b
    print(f'{name:20s} ref {ta:5.2f}s fast {tb:5.2f}s | model peak diff {np.max(abs(d)):.1e} | components equal {kinds(ra)==kinds(rb)} ({len(kinds(ra))}) | passes equal {passes(ra)==passes(rb)} | bypass {ra.get("bypass")==rb.get("bypass")}')
