"""Time and compare the fast prototype against the production numba backend."""
import os
for n in ('OPENBLAS_NUM_THREADS','OMP_NUM_THREADS','MKL_NUM_THREADS'):
    os.environ[n]='1'
import sys, time, json, cProfile, pstats
from pathlib import Path
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]/'scripts'))
from _bootstrap import install; install()
import numpy as np, soundfile as sf
import denoise_v2_numba as production
import denoise_v2_fast as fast
import fast_kernels
from render_denoise_v2 import gather_seeds, saved_seeds

tape, ch = (sys.argv[1], sys.argv[2]) if len(sys.argv) > 2 else ('A15_T510077', '3')
starts = [int(v) for v in sys.argv[3].split(',')] if len(sys.argv) > 3 else [2048]
src = next(Path(f'D:/Apollo_15_30-Track/orig_concatenated/{tape}').glob(f'*_CH{ch}_*.wav'))
hint = Path(f'D:/Apollo_15_30-Track/denoised_v1/{tape}/CH{int(ch):02d}/cleanup.jsonl')
frames = sf.info(src).frames
rows = saved_seeds(hint, 0, frames)
production.warmup(); fast_kernels.warmup()
STEP = 64*8000
with sf.SoundFile(src) as stream:
    def read(a, n):
        stream.seek(a); return stream.read(n, dtype='float64')
    for start in starts:
        a, b = start*8000, min(frames, start*8000+2*STEP)
        out = {}
        for name, dsp in (('production', production), ('fast', fast)):
            t0 = time.perf_counter()
            chunks = list(dsp.corrected_chunks(read, frames, a, b, lambda s,e: gather_seeds(rows,s,e)))
            dt = time.perf_counter()-t0
            audio = np.concatenate([c[2] for c in chunks])
            records = [(c[3], c[4]) for c in chunks]
            out[name] = (dt, audio, records)
        (dp, ap, rp), (df, af, rf) = out['production'], out['fast']
        d = ap-af
        f32 = ap.astype('<f4').astype('float64')-af.astype('<f4').astype('float64')
        kinds = lambda recs: sorted(set((c['kind'], round(c['hz'],1)) for pair in recs for r in pair for c in r['components']))
        passes = lambda recs: sum(len(v.get('passes',[])) for pair in recs for r in pair for v in r.get('refinement',[]))
        print(f'{tape} CH{ch} @{start}s: production {dp:.2f}s  fast {df:.2f}s  speedup {dp/df:.2f}x | '
              f'float64 peak {np.max(abs(d)):.2e} rms {np.sqrt(np.mean(d*d)):.2e} | float32 peak {np.max(abs(f32)):.2e} '
              f'| components equal {kinds(rp)==kinds(rf)} ({len(kinds(rp))}) | passes {passes(rp)} vs {passes(rf)} | out peak {np.max(abs(ap)):.3f}', flush=True)
