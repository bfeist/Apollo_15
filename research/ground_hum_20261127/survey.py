"""Measure residual mains-harmonic hum across pipeline stages (read-only)."""
import sys, json
import numpy as np, soundfile as sf
from scipy.signal import welch

D = "D:/Apollo_15_30-Track"
CASES = {
    ("A15_T510087", 27): {
        "orig": f"{D}/orig_concatenated/A15_T510087/A15_T510087_HR1L_CH27_TLM_IC_RT_188-14-53-00_189-06-20-49.wav",
    },
    ("A15_T510088", 10): {
        "orig": f"{D}/orig_concatenated/A15_T510088/A15_T510088_HR1U_CH10_NTWK_LT_211-19-36-00_212-12-50-57.wav",
    },
}
for (t, c), d in CASES.items():
    d["denoised"] = f"{D}/denoised/{t}/CH{c:02d}/CH{c:02d}.wav"
    d["crosstalk"] = f"{D}/crosstalk_removed/{t}/CH{c:02d}/crosstalk.wav"
    d["deflutter"] = f"{D}/defluttered_shared/{t}/CH{c:02d}/deflutter.wav"

WIN = 30.0
NPOS = int(sys.argv[1]) if len(sys.argv) > 1 else 12
HARM = list(range(1, 21))  # 60..1200 Hz

def measure(x, sr):
    f, p = welch(x, sr, nperseg=int(sr * 8), noverlap=int(sr * 4))  # 0.125 Hz bins
    res = {}
    for k in HARM:
        f0 = 60.0 * k
        pk = (f > f0 - 1.5) & (f < f0 + 1.5)
        fl = ((f > f0 - 12) & (f < f0 - 4)) | ((f > f0 + 4) & (f < f0 + 12))
        i = np.argmax(np.where(pk, p, 0))
        peak = p[i]
        floor = np.median(p[fl])
        # -10 dB width of the line
        band = pk.nonzero()[0]
        above = band[p[band] > peak / 10]
        width = (f[above.max()] - f[above.min()]) if above.size else 0
        res[k] = (10 * np.log10(peak / floor), float(f[i]), float(width))
    tot = 10 * np.log10(np.mean(p[(f > 300) & (f < 3000)]))
    return res, tot

out = {}
for (t, c), paths in CASES.items():
    info = sf.info(paths["orig"])
    sr, n = info.samplerate, info.frames
    positions = np.linspace(0.05, 0.95, NPOS) * n / sr
    for stage, path in paths.items():
        rows = []
        for s in positions:
            x, _ = sf.read(path, start=int(s * sr), frames=int(WIN * sr), dtype="float32")
            r, tot = measure(x.astype(np.float64), sr)
            rows.append({"t": float(s), "speech_band_db": float(tot), "harm": {k: v for k, v in r.items()}})
        out[f"{t}_CH{c:02d}_{stage}"] = rows
        med = {k: np.median([row["harm"][k][0] for row in rows]) for k in HARM}
        wid = {k: np.median([row["harm"][k][2] for row in rows]) for k in HARM}
        print(f"{t} CH{c:02d} {stage:10s} prominence dB (median over {NPOS} windows) / -10dB width Hz")
        print("   " + " ".join(f"{60*k:>5d}" for k in HARM))
        print("   " + " ".join(f"{med[k]:5.1f}" for k in HARM))
        print("   " + " ".join(f"{wid[k]:5.2f}" for k in HARM))
json.dump(out, open(__file__.replace("survey.py", "survey.json"), "w"), indent=1)
