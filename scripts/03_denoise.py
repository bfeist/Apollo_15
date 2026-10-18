"""Stage 03: tone/hum removal followed by 3 dB-capped broadband denoising.

Process review excerpts with --input and --output, or a canonical tape channel
with --input and --channel. Whole channels use the persistent denoised cache.
"""
import os

for name in ('OPENBLAS_NUM_THREADS', 'OMP_NUM_THREADS', 'MKL_NUM_THREADS'):
    os.environ.setdefault(name, '1')

from audio_stage import main


if __name__ == '__main__':
    main('denoise')
