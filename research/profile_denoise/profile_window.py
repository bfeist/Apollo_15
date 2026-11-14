"""Profile one real 136-second model window with the reference and numba backends."""
import os
for n in ('OPENBLAS_NUM_THREADS','OMP_NUM_THREADS','MKL_NUM_THREADS'):
    os.environ[n]='1'
import sys, time, cProfile, pstats
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]/'scripts'))
from _bootstrap import install; install()
import numpy as np, soundfile as sf
import denoise_v2 as reference
import denoise_v2_numba as accelerated
from render_denoise_v2 import gather_seeds, saved_seeds

tape, ch = 'A15_T510077', 3
src = next(Path(f'D:/Apollo_15_30-Track/orig_concatenated/{tape}').glob(f'*_CH{ch}_*.wav'))
hint = Path(f'D:/Apollo_15_30-Track/denoised_v1/{tape}/CH{ch:02d}/cleanup.jsonl')
frames = sf.info(src).frames
rows = saved_seeds(hint, 0, frames)
start = int(sys.argv[1]) if len(sys.argv) > 1 else 2048
a, b = (start-4)*8000, (start+128+4)*8000
x, _ = sf.read(src, start=a, frames=b-a, dtype='float64')
seeds = gather_seeds(rows, a, b)
print('context', len(x)/8000, 's; seeds', len(seeds))
accelerated.warmup()
for name, dsp in (('reference', reference), ('numba', accelerated)):
    t = time.perf_counter(); m, r = dsp.model_window(x, seeds, defer_compact=True); dt = time.perf_counter()-t
    print(f'{name}: {dt:.2f}s  components={len(r["components"])} refinement={len(r["refinement"])}')
pr = cProfile.Profile(); pr.enable()
accelerated.model_window(x, seeds, defer_compact=True)
pr.disable()
st = pstats.Stats(pr); st.sort_stats('cumulative').print_stats(45)
st.sort_stats('tottime').print_stats(30)
