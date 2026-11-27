"""Head row of the time-code track from speech leaking onto it. Read-only.

GCC-PHAT in the speech band (IRIG carrier band removed) between the time-code channel and each
neighbour, in many windows. Playback-side leakage appears at lag 0. Leakage written in 1971
by the time-code track's own head appears at (time-code row - neighbour row): 0 for a neighbour on
the same row, about -/+1.38 s for one on the other row.

  uv run python research/event_check/timecode_row_speech.py A15_T510075 26,27,28,29
"""
import sys

import numpy as np
import soundfile as sf
from scipy.signal import butter, sosfiltfilt

ROOT = 'D:/Apollo_15_30-Track/defluttered_shared'


def band(x, sr):
    lo = sosfiltfilt(butter(6, [300, 800], 'bandpass', fs=sr, output='sos'), x)
    hi = sosfiltfilt(butter(6, [1400, 3400], 'bandpass', fs=sr, output='sos'), x)
    return lo + hi


def gcc(a, b, sr, max_lag):
    n = 1 << int(np.ceil(np.log2(len(a) + len(b))))
    A, B = np.fft.rfft(a, n), np.fft.rfft(b, n)
    R = A * np.conj(B)
    r = np.fft.irfft(R / (np.abs(R) + 1e-12), n)
    k = int(max_lag * sr)
    r = np.r_[r[-k:], r[:k + 1]]
    return np.arange(-k, k + 1) / sr, r


def main():
    tape, chans = sys.argv[1], [int(c) for c in sys.argv[2].split(',')]
    tc = 30
    path = f'{ROOT}/{tape}/CH{tc:02d}/deflutter.wav'
    info = sf.info(path)
    sr, dur = info.samplerate, info.frames / info.samplerate
    starts = np.arange(600, dur - 700, (dur - 1300) / 40)
    for ch in chans:
        acc = None
        for s in starts:
            a, _ = sf.read(path, start=int(s * sr), frames=int(60 * sr))
            b, _ = sf.read(f'{ROOT}/{tape}/CH{ch:02d}/deflutter.wav', start=int(s * sr), frames=int(60 * sr))
            lags, r = gcc(band(a, sr), band(b, sr), sr, 3.0)
            acc = r if acc is None else acc + r
        # lag > 0: the time-code channel's copy comes later than the neighbour's
        m = np.abs(lags) > 0.25
        floor = np.median(np.abs(acc[m]))
        k = np.flatnonzero(m)[np.argmax(acc[m])]
        def at(x):
            w = np.abs(lags - x) < 0.04
            return acc[w].max() / floor
        print(f'{tape} CH30 vs CH{ch:02d}: best off-zero lag {lags[k]:+.3f} s ({acc[k] / floor:.1f}x floor); '
              f'at -1.38: {at(-1.38):.1f}x, at +1.38: {at(1.38):.1f}x')


if __name__ == '__main__':
    main()
