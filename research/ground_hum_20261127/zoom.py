"""Top lines near the first harmonics, fine resolution, several positions."""
import sys
import numpy as np, soundfile as sf
from scipy.signal import welch
from scipy.ndimage import median_filter
path = sys.argv[1]; npos = int(sys.argv[2])
info = sf.info(path); sr = info.samplerate; n = info.frames
print(path)
for s in np.linspace(0.05, 0.95, npos) * n / sr:
    x, _ = sf.read(path, start=int(s*sr), frames=int(120*sr), dtype="float32")
    f, p = welch(x.astype(float), sr, nperseg=sr*20, noverlap=sr*10)  # 0.05 Hz
    lp = 10*np.log10(p+1e-30); prom = lp - median_filter(lp, size=401)
    out = []
    for lo, hi in [(40, 80), (100, 140), (160, 200), (280, 320)]:
        m = (f > lo) & (f < hi)
        ii = np.where(m)[0]; top = ii[np.argsort(prom[ii])[-3:][::-1]]
        out.append(" ".join(f"{f[i]:6.2f}({prom[i]:4.1f})" for i in top))
    print(f" t={s/3600:5.2f}h | " + " | ".join(out))
