"""Compare fused production DSP against the frozen reference implementation."""
import os
for n in ('OPENBLAS_NUM_THREADS','OMP_NUM_THREADS','MKL_NUM_THREADS'):
    os.environ[n]='1'
import sys, time, json, cProfile, pstats
from pathlib import Path
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]/'scripts'))
from _bootstrap import install; install()
import numpy as np, soundfile as sf
import denoise_v2 as production
import denoise_v2_fast as fast
import fast_kernels
from render_denoise_v2 import gather_seeds, saved_seeds

tape, ch = (sys.argv[1], sys.argv[2]) if len(sys.argv) > 2 else ('A15_T510077', '3')
starts = [int(v) for v in sys.argv[3].split(',')] if len(sys.argv) > 3 else [2048]
src = next(Path(f'D:/Apollo_15_30-Track/orig_concatenated/{tape}').glob(f'*_CH{ch}_*.wav'))
hint = Path(f'D:/Apollo_15_30-Track/denoised_v1/{tape}/CH{int(ch):02d}/cleanup.jsonl')
frames = sf.info(src).frames
rows = saved_seeds(hint, 0, frames)
fast_kernels.warmup()
sys.path.insert(0, str(HERE.parents[1]/'scripts/03_denoise/runtime/research'))
from qualify_denoise_numba import decision_structure, difference, within_tolerance, render_with_models
STEP = 64*8000
with sf.SoundFile(src) as stream:
    def read(a, n):
        stream.seek(a); return stream.read(n, dtype='float64')
    for start in starts:
        a, b = start*8000, min(frames, start*8000+2*STEP)
        out = {}
        for name, dsp in (('production', production), ('fast', fast)):
            t0 = time.perf_counter()
            chunks, models = render_with_models(dsp, read, frames, a, b, lambda s,e: gather_seeds(rows,s,e))
            dt = time.perf_counter()-t0
            audio = np.concatenate([c[2] for c in chunks])
            records = [(c[3], c[4]) for c in chunks]
            out[name] = (dt, audio, records, models)
        (dp, ap, rp, mp), (df, af, rf, mf) = out['production'], out['fast']
        decisions = [[decision_structure(r) for pair in records for r in pair] for records in (rp,rf)]
        model = difference(np.concatenate(mp), np.concatenate(mf))
        output = difference(ap.astype('<f4'), af.astype('<f4'))
        passed = within_tolerance(model, output, decisions[0] == decisions[1])
        row = dict(tape=tape, channel=int(ch), interval=[a,b], seconds=[dp,df],
                   model_difference=model, corrected_float32_difference=output,
                   decisions_equal=decisions[0] == decisions[1], passed=passed)
        if not passed:
            row.update(reference_decisions=decisions[0], accelerated_decisions=decisions[1])
        print(json.dumps(row), flush=True)
        if not passed:
            raise RuntimeError(f'Equivalence failed: {tape} CH{ch} @{start}s')
