"""Stage 03: tone/hum removal followed by 3 dB-capped broadband denoising.

Process review excerpts with --input and --output, or a canonical tape channel
with --input and --channel. Whole channels use the persistent denoised cache.
"""

# Resolve shared modules from the stage folders.
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))
from _bootstrap import install as _install
_install()

import os

for name in ('OPENBLAS_NUM_THREADS', 'OMP_NUM_THREADS', 'MKL_NUM_THREADS'):
    os.environ.setdefault(name, '1')

from audio_stage import main


if __name__ == '__main__':
    main('denoise')
