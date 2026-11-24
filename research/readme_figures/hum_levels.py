"""Sample how loud the digitization hum is relative to the program and to the 1971 hum.

For sampled minutes on several tapes and channels: (a) power of the stage-03 hum
model (original minus denoised, gain-corrected) relative to the remaining audio,
and (b) the exact 60/120/180 Hz digitization line peak relative to the on-tape
60·rho·n line peak (16 s Welch). Prints medians; read-only.
"""
import json
import sys
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy import signal

sys.path.insert(0, str(Path(__file__).resolve().parents[2]/'scripts'))
from _bootstrap import install  # noqa: E402
install()

ROOT = Path('D:/Apollo_15_30-Track')
TAPES = ['A15_T510076', 'A15_T510083', 'A15_T510090', 'A15_T510099', 'A15_T510106']
CHANNELS = [5, 9, 14, 20, 25]
POSITIONS = (.2, .4, .6, .8)


def main():
    ratios, lines = [], {1: [], 2: [], 3: []}
    for tape in TAPES:
        plan = json.loads((ROOT/'deflutter_maps'/tape/'plan.json').read_text(encoding='utf-8'))
        rows = {r['channel']: r for r in plan['inventory']['channels']}
        for ch in CHANNELS:
            r = rows[ch]
            folder = ROOT/'denoised'/tape/f'CH{ch:02d}'
            gain = json.loads((folder/'denoised.json').read_text(encoding='utf-8'))['gain']
            n = r['frames']
            for q in POSITIONS:
                a = int(q*(n-60*8000))
                x, _ = sf.read(r['path'], start=a, frames=60*8000, dtype='float64')
                y, _ = sf.read(folder/f'CH{ch:02d}.wav', start=a, frames=60*8000, dtype='float64')
                y /= gain
                ratios.append(10*np.log10(np.mean((x-y)**2)/max(np.mean(y**2), 1e-30)))
                f, p = signal.welch(x, 8000, nperseg=16*8000, noverlap=8*8000)
                for k in lines:
                    exact = p[np.abs(f-60*k) <= .1].max()
                    band = (f > 58.3*k) & (f < 59.8*k)
                    lines[k].append(10*np.log10(exact/p[band].max()))
    print(f'hum model power / remaining audio: median {np.median(ratios):+.1f} dB, '
          f'IQR {np.percentile(ratios, 25):+.1f}..{np.percentile(ratios, 75):+.1f} dB, n={len(ratios)}')
    for k, v in lines.items():
        print(f'{60*k} Hz digitization line over on-tape line: median {np.median(v):+.1f} dB, '
              f'IQR {np.percentile(v, 25):+.1f}..{np.percentile(v, 75):+.1f} dB')


if __name__ == '__main__':
    main()
