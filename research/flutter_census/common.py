"""Shared helpers for the November 15 flutter-census experiments.

Research only. Reads canonical originals read-only; never writes beside them.
Canonical tape time = channel WAV time + inventory ``start_seconds``.
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy import ndimage, signal

ROOT = Path('D:/Apollo_15_30-Track')
ORIG = ROOT / 'orig_concatenated'
V5 = ROOT / 'deflutter_planning_v5_allchannel'
RATE = 200  # map / trajectory rate used by the pipeline
SR = 8000


@lru_cache(None)
def inventory(tape: str) -> dict[int, dict]:
    """Channel -> {path, start_seconds, frames}; from the saved v5 plan if
    available, otherwise from filenames (offset 0)."""
    plan = V5 / tape / 'plan' / 'plan.json'
    out = {}
    if plan.exists():
        # plan.json is big; inventory is first key -> cheap enough to load once
        inv = json.loads(plan.read_text(encoding='utf-8'))['inventory']
        for c in inv['channels']:
            out[c['channel']] = dict(path=c['path'], start=float(c.get('start_seconds', 0)),
                                     frames=c['frames'])
        return out
    for p in sorted((ORIG / tape).glob('*.wav')):
        m = re.search(r'_CH(\d+)_', p.name)
        if m:
            out[int(m.group(1))] = dict(path=str(p), start=0.0, frames=sf.info(p).frames)
    return out


def read(tape: str, ch: int, t0: float, dur: float) -> np.ndarray:
    """Read canonical interval [t0, t0+dur) from an original channel.
    Missing samples are zero-filled (and reported by the caller via support)."""
    c = inventory(tape)[ch]
    a = int(round((t0 - c['start']) * SR))
    n = int(round(dur * SR))
    out = np.zeros(n, dtype=np.float64)
    lo, hi = max(a, 0), min(a + n, c['frames'])
    if hi > lo:
        with sf.SoundFile(c['path']) as f:
            f.seek(lo)
            out[lo - a:hi - a] = f.read(hi - lo, dtype='float64')
    return out


def find_lines(x: np.ndarray, sr: int = SR, nper_s: float = 8., fmin=40., fmax=3950.,
               min_snr_db=10., min_sep_hz=1.0):
    """Persistent narrow lines from a Welch spectrum (0.125 Hz bins).
    Returns list of dicts: hz, snr_db, mains_offset_hz (distance to nearest
    exact 60 Hz harmonic)."""
    nper = int(nper_s * sr)
    f, p = signal.welch(x, sr, nperseg=nper, noverlap=nper // 2, window='hann')
    lp = 10 * np.log10(p + 1e-30)
    bg = ndimage.median_filter(lp, size=int(40 / (f[1] - f[0])) | 1, mode='nearest')
    snr = lp - bg
    peaks, _ = signal.find_peaks(snr, height=min_snr_db, distance=max(1, int(min_sep_hz / (f[1] - f[0]))))
    rows = []
    for i in peaks:
        hz = f[i]
        if not fmin < hz < fmax:
            continue
        # parabolic refinement
        if 0 < i < len(lp) - 1:
            y0, y1, y2 = lp[i - 1], lp[i], lp[i + 1]
            d = .5 * (y0 - y2) / (y0 - 2 * y1 + y2 + 1e-12)
            hz = f[i] + np.clip(d, -.5, .5) * (f[1] - f[0])
        n = round(hz / 60)
        rows.append(dict(hz=float(hz), snr_db=float(snr[i]), level_db=float(lp[i]),
                         mains_offset_hz=float(hz - 60 * n), harmonic=int(n)))
    return rows


def demod(x, sr, hz, capture, out_rate=RATE):
    """Complex demodulation at ``hz`` with a zero-phase low-pass of +-capture Hz.
    Returns (amp, inst_freq_hz) sampled at out_rate."""
    t = np.arange(len(x)) / sr
    dec = sr // 1000
    z = signal.resample_poly(x * np.exp(-2j * np.pi * hz * t), 1, dec)
    sos = signal.butter(6, capture, fs=sr / dec, output='sos')
    z = signal.sosfiltfilt(sos, z)
    step = (sr // dec) // out_rate
    ph = np.unwrap(np.angle(z))
    fr = hz + np.gradient(ph) * (sr / dec) / (2 * np.pi)
    # average to out_rate (boxcar) -- keeps 2-30 Hz flutter band intact
    k = len(z) // step * step
    amp = np.abs(z[:k]).reshape(-1, step).mean(1)
    fr = fr[:k].reshape(-1, step).mean(1)
    return amp, fr


def frac_dev(fr, amp, band=(2., 20.), rate=RATE, capture=None, centre=None):
    """Fractional speed deviation (bandpassed) and support mask."""
    c = float(np.median(fr)) if centre is None else centre
    e = (fr - c) / c
    med = np.median(amp)
    ok = amp > .3 * med
    if capture is not None:
        ok &= np.abs(fr - c) < .9 * capture
    ok = ndimage.binary_erosion(ok, iterations=10)
    e = np.where(ok, e, 0.)
    e = np.clip(e, -.05, .05)
    sos = signal.butter(3, band, fs=rate, btype='bandpass', output='sos')
    eb = signal.sosfiltfilt(sos, e)
    return eb, ok, c


def corr(a, b, m=None):
    if m is not None:
        a, b = a[m], b[m]
    a = a - a.mean(); b = b - b.mean()
    d = np.sqrt((a * a).sum() * (b * b).sum())
    return float((a * b).sum() / d) if d > 0 else 0.


def slope(y, x, m=None):
    """Least-squares gain of y on x."""
    if m is not None:
        y, x = y[m], x[m]
    return float((x * y).sum() / max((x * x).sum(), 1e-30))


def lagcorr(a, b, maxlag_s=2.0, rate=RATE):
    """Normalized cross-correlation over +-maxlag; returns (best_lag_s, r, r0).
    Positive lag: b is delayed relative to a."""
    n = len(a)
    a = a - a.mean(); b = b - b.mean()
    cc = signal.fftconvolve(b, a[::-1], mode='full')
    lags = np.arange(-n + 1, n)
    norm = np.sqrt((a * a).sum() * (b * b).sum()) + 1e-30
    k = np.abs(lags) <= int(maxlag_s * rate)
    i = np.argmax(np.abs(cc[k]))
    return float(lags[k][i] / rate), float(cc[k][i] / norm), float(cc[lags == 0][0] / norm)


def dewarp(x, e, sr=SR, rate=RATE, up=4):
    """Resample x so that a tone whose fractional frequency deviation is e(t)
    (sampled at ``rate``) becomes steady. e is speed error: playback content
    time advances at (1+e) per second of file time."""
    n = len(x)
    t = np.arange(n) / sr
    te = np.arange(len(e)) / rate
    ee = np.interp(t, te, e)
    tau = np.concatenate([[0.], np.cumsum((1 + ee[:-1]) / sr)])  # content time at file time t
    # want samples at uniform content time
    tau_u = np.arange(n) / sr * (tau[-1] / t[-1])
    t_of_tau = np.interp(tau_u, tau, t)
    xu = signal.resample_poly(x, up, 1)
    return np.interp(t_of_tau * sr * up, np.arange(len(xu)), xu)


def exact_mains(x, sr=SR, nper=16 * SR, fmax=3900.):
    """Frequencies of strong, narrow lines at exact 60 Hz multiples."""
    f, p = signal.welch(x, sr, nperseg=nper)
    lp = 10 * np.log10(p + 1e-30)
    out = []
    for n in range(1, int(fmax / 60)):
        k = np.abs(f - 60 * n) < .25
        side = (np.abs(f - 60 * n) > 2) & (np.abs(f - 60 * n) < 6)
        if k.any() and lp[k].max() - np.median(lp[side]) > 10:
            out.append(float(f[k][np.argmax(lp[k])]))
    return out


def remove_stationary(x, hz_list, sr=SR, block_s=16.):
    """Least-squares removal of stationary lines (digitization hum), fitted in
    blocks so slow amplitude drift is followed. A fluttered on-tape line near
    the same frequency is spread by FM and is largely untouched."""
    y = x.copy()
    n = int(block_s * sr)
    t = np.arange(len(x)) / sr
    for hz in hz_list:
        c, s = np.cos(2 * np.pi * hz * t), np.sin(2 * np.pi * hz * t)
        for a in range(0, len(x), n):
            sl = slice(a, min(a + n, len(x)))
            m = sl.stop - sl.start
            if m < sr:
                continue
            ca, sa = c[sl], s[sl]
            A = np.array([[ca @ ca, ca @ sa], [ca @ sa, sa @ sa]])
            bvec = np.array([y[sl] @ ca, y[sl] @ sa])
            u, v = np.linalg.solve(A, bvec)
            y[sl] -= u * ca + v * sa
    return y
