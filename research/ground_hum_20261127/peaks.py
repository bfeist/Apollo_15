"""List persistent tonal lines in a stage output (read-only)."""
import sys
import numpy as np, soundfile as sf
from scipy.signal import welch
from scipy.ndimage import median_filter
path = sys.argv[1]; npos = int(sys.argv[2]) if len(sys.argv) > 2 else 24
info = sf.info(path); sr = info.samplerate; n = info.frames
hits = {}
for s in np.linspace(0.02, 0.98, npos) * n / sr:
    x, _ = sf.read(path, start=int(s*sr), frames=int(30*sr), dtype="float32")
    f, p = welch(x.astype(float), sr, nperseg=sr*4, noverlap=sr*2)  # 0.25 Hz
    lp = 10*np.log10(p + 1e-30)
    base = median_filter(lp, size=81)  # +-10 Hz
    prom = lp - base
    sel = (f > 40) & (f < 2500)
    idx = np.where(sel & (prom > 8))[0]
    # local maxima only
    idx = [i for i in idx if prom[i] >= prom[i-1] and prom[i] >= prom[i+1]]
    for i in idx:
        key = round(f[i])
        hits.setdefault(key, []).append(prom[i])
print(path)
for k in sorted(hits):
    v = hits[k]
    if len(v) >= npos * 0.25:
        print(f"  {k:5d} Hz  in {len(v):2d}/{npos} windows  median prom {np.median(v):5.1f} dB")
