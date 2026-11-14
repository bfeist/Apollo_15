"""Profile the full corrected_chunks + writer-side per-chunk work for two steps."""
import os
for n in ('OPENBLAS_NUM_THREADS','OMP_NUM_THREADS','MKL_NUM_THREADS'):
    os.environ[n]='1'
import sys, time, cProfile, pstats, hashlib
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]/'scripts'))
from _bootstrap import install; install()
import numpy as np, soundfile as sf
import denoise_v2_fast as accelerated, fast_kernels
from denoise_v2_quality import residual_windows
from render_denoise_v2 import gather_seeds, saved_seeds

tape, ch = 'A15_T510077', 3
src = next(Path(f'D:/Apollo_15_30-Track/orig_concatenated/{tape}').glob(f'*_CH{ch}_*.wav'))
hint = Path(f'D:/Apollo_15_30-Track/denoised_v1/{tape}/CH{ch:02d}/cleanup.jsonl')
frames = sf.info(src).frames
rows = saved_seeds(hint, 0, frames)
fast_kernels.warmup()
STEP = 64*8000
start = 2048*8000
with sf.SoundFile(src) as stream:
    def read(a, n):
        stream.seek(a); return stream.read(n, dtype='float64')
    pr = cProfile.Profile(); pr.enable(); t0 = time.perf_counter()
    for frame, raw, result, left, right in accelerated.corrected_chunks(read, frames, start, start+3*STEP, lambda a,b: gather_seeds(rows,a,b)):
        x = result.astype('<f4')
        w = residual_windows(raw, x, 8000, left, right, None, frame)
        hashlib.sha256(x).hexdigest()
    pr.disable(); print(f'3 steps: {time.perf_counter()-t0:.2f}s')
st = pstats.Stats(pr); st.sort_stats('cumulative').print_stats(25)
