"""E2: de-warp every channel by a reference speed curve (+e) and by its
wrong-sign control (-e). A line that is sharp under +e but not under -e is an
on-tape tone carrying the same tape motion. Stationary (digitization) lines
produce symmetric FM sidebands under +-e, so they cancel in the contrast."""
import json, sys
from pathlib import Path
import numpy as np
from scipy import signal, ndimage
sys.path.insert(0, str(Path(__file__).parent))
from common import *


def line_spectrum(x, nper=8*SR):
    f, p = signal.welch(x, SR, nperseg=nper, noverlap=nper//2)
    lp = 10*np.log10(p+1e-30)
    bg = ndimage.median_filter(lp, size=int(60/(f[1]-f[0])) | 1, mode='nearest')
    return f, lp, lp-bg


def census(tape, t0, dur, e, channels=None, thr=10.):
    rows = []
    for ch in channels or sorted(inventory(tape)):
        x = read(tape, ch, t0, dur)
        if not x.any():
            continue
        f, _, s0 = line_spectrum(x)
        _, _, sp = line_spectrum(dewarp(x, e))
        _, _, sm = line_spectrum(dewarp(x, -e))
        pk, _ = signal.find_peaks(sp, height=12, distance=int(2/(f[1]-f[0])))
        for i in pk:
            if f[i] < 40:
                continue
            w = slice(max(0, i-2), i+3)
            con = sp[i]-sm[w].max()
            rows.append(dict(ch=ch, hz=round(float(f[i]), 3), snr_plus=round(float(sp[i]), 1),
                             snr_minus=round(float(sm[w].max()), 1), snr_raw=round(float(s0[w].max()), 1),
                             contrast_db=round(float(con), 1)))
    on = [r for r in rows if r['contrast_db'] > thr]
    on.sort(key=lambda r: -r['contrast_db'])
    return rows, on


def main(tape, t0, dur, refch, refhz, cap, out=None, band=(2, 30)):
    xr = read(tape, refch, t0, dur)
    amp, fr = demod(xr, SR, refhz, cap)
    e, ok, c = frac_dev(fr, amp, band=band, capture=cap)
    print(f'ref CH{refch} centre {c:.2f} rms {100*e.std():.2f}% support {ok.mean():.3f}')
    rows, on = census(tape, t0, dur, e)
    print(len(rows), 'peaks; ', len(on), 'with +e/-e contrast >10 dB; channels:', sorted({r['ch'] for r in on}))
    for r in on[:60]:
        print(r)
    if out:
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_text(json.dumps(dict(tape=tape, t0=t0, dur=dur, ref=[refch, refhz, cap],
                                             ref_rms_pct=100*float(e.std()), rows=rows), indent=1))


if __name__ == '__main__':
    a = sys.argv[1:]
    main(a[0], float(a[1]), float(a[2]), int(a[3]), float(a[4]), float(a[5]), a[6] if len(a) > 6 else None)
