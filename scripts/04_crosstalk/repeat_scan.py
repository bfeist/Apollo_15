"""Find paired waveform occurrences within one target, in its original sample coordinates."""
import argparse
import json
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy import fft, signal


RATE = 8000
WINDOW = 2000  # Non-overlapping quarter seconds at native rate.
LOW, HIGH = 6400, 15200
BLOCK = 256 * WINDOW
COARSE_FIR = signal.firwin(97, [310, 890], fs=RATE, pass_zero=False).astype('float32')
NATIVE_FIR = signal.firwin(97, [320, 2850], fs=RATE, pass_zero=False).astype('float32')
DTYPE = [('frame', '<i8'), ('lag', '<f8'), ('correlation', '<f4'), ('later_db', '<f4')]


def profiles(phrase, search):
    """Normalized valid cross-correlations along the last dimension."""
    width = phrase.shape[-1]
    length = search.shape[-1] - width + 1
    n = fft.next_fast_len(search.shape[-1], real=True)
    product = fft.rfft(search, n=n) * fft.rfft(phrase, n=n).conj()
    dots = fft.irfft(product, n=n)[..., :length]
    sums = np.pad(np.cumsum(search * search, axis=-1, dtype='float64'),
                  [(0, 0)] * (search.ndim - 1) + [(1, 0)])
    energy = sums[..., width:] - sums[..., :-width]
    reference_power = np.sum(phrase * phrase, axis=-1, keepdims=True)
    # Near silence, FFT roundoff from a louder part of the search can exceed
    # the dot product. Such a position cannot establish a waveform match.
    usable = (energy > np.maximum(1e-20, energy.max(axis=-1, keepdims=True)*1e-8)) & (reference_power > 1e-20)
    norm = np.sqrt(np.maximum(energy * reference_power, 1e-30))
    return np.divide(dots, norm, out=np.zeros_like(norm), where=usable)


def distinctive(values, threshold, exclusion, ratio=1.16):
    best = np.argmax(abs(values), axis=-1)
    height = np.take_along_axis(abs(values), best[..., None], axis=-1)[..., 0]
    mask = abs(np.arange(values.shape[-1]) - best[..., None]) > exclusion
    other = np.max(np.where(mask, abs(values), 0), axis=-1)
    return best, (height >= threshold) & (height >= ratio * other)


def fractional(values, peak):
    if 0 < peak < len(values)-1:
        a, b, c = abs(values[peak-1:peak+2])
        if a+c < 2*b:
            return peak + float(np.clip(.5*(a-c)/(a-2*b+c), -.5, .5))
    return float(peak)


def windows(audio, width, count, hop):
    return np.lib.stride_tricks.sliding_window_view(audio, width)[::hop][:count]


def scan_block(audio, count, first_frame):
    # Include native FIR halo on both sides of the valid samples.
    coarse = signal.upfirdn(COARSE_FIR, audio, down=4)[24:24+(len(audio)-96)//4]
    hop, lo, hi = WINDOW//4, LOW//4, HIGH//4
    refs = windows(coarse, hop, count, hop)
    candidates = profiles(refs, windows(coarse[lo:], hi-lo+hop, count, hop))
    lag, use = distinctive(candidates, .31, 12)
    if not np.any(use):
        return []
    native = signal.oaconvolve(audio, NATIVE_FIR, mode='valid')
    hits = []
    for i in np.flatnonzero(use):
        a = i * WINDOW
        ref = native[a:a+WINDOW]
        center = (lo + int(lag[i])) * 4
        offset = max(LOW, center-20)
        match = native[a+offset:a+min(HIGH, center+20)+WINDOW]
        corr = profiles(ref, match)
        k = int(np.argmax(abs(corr)))
        if abs(corr[k]) < .30:
            continue
        aligned = match[k:k+WINDOW]
        # Both halves must carry the same signed repeat. A lone impulse or
        # a short common tone does not qualify a quarter second of speech.
        halves = ref.reshape(2, -1)
        paired = aligned.reshape(2, -1)
        agreement = np.sum(halves*paired, axis=1) / np.sqrt(np.maximum(
            np.sum(halves**2, axis=1)*np.sum(paired**2, axis=1), 1e-30))
        if np.min(np.sign(corr[k])*agreement) < .18:
            continue
        db = 10*np.log10(np.maximum(np.dot(aligned, aligned), 1e-30) /
                         np.maximum(np.dot(ref, ref), 1e-30))
        hits.append((first_frame+a, offset+fractional(corr, k), corr[k], db))
    return hits


def scan(path, start=0, stop=None):
    hits = []
    with sf.SoundFile(path) as source:
        if (source.samplerate, source.channels, source.subtype) != (RATE, 1, 'FLOAT'):
            raise ValueError(f'Invalid denoised input {path}')
        end = min(len(source)-HIGH-48, stop if stop is not None else len(source))
        for begin in range(start, end, BLOCK):
            count = min(BLOCK, end-begin)//WINDOW
            if not count:
                continue
            left = max(0, begin-48)
            source.seek(left)
            audio = source.read(begin+count*WINDOW+HIGH+48-left, dtype='float32')
            if not np.isfinite(audio).all():
                raise ValueError(f'Nonfinite input at {begin}')
            if begin < 48:
                audio = np.pad(audio, (48-begin, 0))
            hits.extend(scan_block(audio, count, begin))
    return np.asarray(hits, dtype=DTYPE)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('output', type=Path)
    parser.add_argument('--start', type=float, default=0)
    parser.add_argument('--end', type=float)
    args = parser.parse_args()
    result = scan(args.source, round(args.start*RATE), None if args.end is None else round(args.end*RATE))
    temporary = args.output.with_suffix('.partial')
    with temporary.open('wb') as f:
        np.save(f, result, allow_pickle=False)
    temporary.replace(args.output)
    print(json.dumps(dict(windows_matched=len(result))))
