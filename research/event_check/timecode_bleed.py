"""Which head row carries the time-code track? Delay of its 1 kHz bleed into neighbouring channels.

Read-only. Band-limits each 05b channel around the 1 kHz IRIG carrier, takes the envelope (the
IRIG-B pulse-width pattern, 100 symbols/s) and cross-correlates it with the time-code channel's
envelope in windows. A neighbour on the same head row shows its bleed at lag 0; one on the other
row at about +/-1.38 s.

  uv run python research/event_check/timecode_bleed.py --tape A15_T510075 --channels 26,27,28,29
"""
import argparse

import numpy as np
import soundfile as sf
from scipy.signal import butter, correlate, sosfiltfilt

ROOT = 'D:/Apollo_15_30-Track/defluttered_shared'


def envelope(x, sr):
    sos = butter(4, [900, 1100], 'bandpass', fs=sr, output='sos')
    e = np.abs(sosfiltfilt(sos, x))
    sos2 = butter(4, 150, 'lowpass', fs=sr, output='sos')
    e = sosfiltfilt(sos2, e)[:: sr // 1000]  # 1 kHz envelope rate
    e = e - e.mean()
    return e / (e.std() + 1e-12)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--tape', required=True)
    ap.add_argument('--timecode', type=int, default=30)
    ap.add_argument('--channels', default='26,27,28,29')
    ap.add_argument('--starts', default='1800,9000,18000,30000,45000')
    ap.add_argument('--seconds', type=float, default=120.0)
    ap.add_argument('--max-lag', type=float, default=3.0)
    args = ap.parse_args()
    tc_path = f'{ROOT}/{args.tape}/CH{args.timecode:02d}/deflutter.wav'
    sr = sf.info(tc_path).samplerate
    pad = args.max_lag
    for start in map(float, args.starts.split(',')):
        ref, _ = sf.read(tc_path, start=int(start * sr), frames=int(args.seconds * sr))
        r = envelope(ref, sr)
        line = [f'tape {start:7.0f} s']
        for ch in map(int, args.channels.split(',')):
            x, _ = sf.read(f'{ROOT}/{args.tape}/CH{ch:02d}/deflutter.wav',
                           start=int((start - pad) * sr), frames=int((args.seconds + 2 * pad) * sr))
            e = envelope(x, sr)
            c = correlate(e, r, mode='valid') / len(r)  # lag index 0 = -pad
            lags = np.arange(len(c)) / 1000.0 - pad
            k = int(np.argmax(c))
            floor = np.median(np.abs(c))
            line.append(f'CH{ch:02d} lag {lags[k]:+.3f} s peak {c[k]:.3f} ({c[k] / floor:5.1f}x)')
        print(' | '.join(line))


if __name__ == '__main__':
    main()
