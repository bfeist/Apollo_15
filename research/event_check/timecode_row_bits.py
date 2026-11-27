"""Head row of the time-code track from its own changing bits leaking into neighbours. Read-only.

The 1 kHz envelope of each channel is cut into frames on the time-code track's decoded frame starts
(shifted by a trial lag), the mean frame is removed so only the bits that change from second to
second remain, and the residual is correlated with the time-code track's own residual. A copy of
the code written in 1971 onto a neighbour track by the other head row appears at about +/-1.38 s;
playback-side leakage at 0.

  uv run python research/event_check/timecode_row_bits.py A15_T510075 27,28,29
"""
import json
import sys

import numpy as np
import soundfile as sf
from scipy.signal import butter, sosfiltfilt

ROOT = 'D:/Apollo_15_30-Track'
FS = 1000


def envelope(x, sr):
    e = np.abs(sosfiltfilt(butter(4, [900, 1100], 'bandpass', fs=sr, output='sos'), x))
    e = sosfiltfilt(butter(4, 300, 'lowpass', fs=sr, output='sos'), e)
    return np.interp(np.arange(0, len(x) / sr, 1 / FS), np.arange(len(x)) / sr, e)


def residual(env, t0, starts, lens, lag):
    u = np.arange(FS) / FS
    pos = (starts[:, None] + lag + u[None, :] * lens[:, None] - t0) * FS
    frames = np.interp(pos.ravel(), np.arange(len(env)), env).reshape(pos.shape)
    frames = frames / (np.median(frames, axis=1, keepdims=True) + 1e-12)
    r = frames - frames.mean(axis=0)
    return r / (np.linalg.norm(r) + 1e-12)


def main():
    tape, chans = sys.argv[1], [int(c) for c in sys.argv[2].split(',')]
    dec = json.loads(open(f'{ROOT}/stage_evidence/retiming/irig/{tape}/decode/CH30.json').read())
    f = np.asarray(dec['frames'], float)
    info = sf.info(f'{ROOT}/defluttered_shared/{tape}/CH30/deflutter.wav')
    sr = info.samplerate
    lags = np.arange(-2.0, 2.0005, 0.002)
    total = {c: np.zeros(len(lags)) for c in chans}
    for w0 in np.linspace(3000, f[-1, 0] - 3000, 8):
        sel = f[(f[:, 0] > w0) & (f[:, 0] < w0 + 300)]
        ok = np.diff(sel[:, 1]) == 1
        if ok.sum() < 200:
            continue
        starts, lens = sel[:-1, 0][ok], np.diff(sel[:, 0])[ok]
        t0 = w0 - 3
        n = int((300 + 6) * sr)
        x30, _ = sf.read(f'{ROOT}/defluttered_shared/{tape}/CH30/deflutter.wav', start=int(t0 * sr), frames=n)
        r30 = residual(envelope(x30, sr), t0, starts, lens, 0.0)
        for c in chans:
            x, _ = sf.read(f'{ROOT}/defluttered_shared/{tape}/CH{c:02d}/deflutter.wav', start=int(t0 * sr), frames=n)
            e = envelope(x, sr)
            total[c] += [np.sum(r30 * residual(e, t0, starts, lens, lag)) for lag in lags]
    for c in chans:
        r = total[c]
        floor = np.median(np.abs(r)) + 1e-12
        order = np.argsort(r)[::-1]
        peaks = []
        for k in order:
            if all(abs(lags[k] - p) > 0.05 for p, _ in peaks):
                peaks.append((lags[k], r[k] / floor))
            if len(peaks) == 3:
                break
        print(f'{tape} CH{c:02d}: peaks (lag s, x floor) ' + ', '.join(f'{p:+.3f} ({v:.0f}x)' for p, v in peaks))


if __name__ == '__main__':
    main()
