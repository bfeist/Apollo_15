"""IRIG-B timecode decoding for the NARA Apollo 15 flight tapes.

IRIG-B (as recorded at MSC in 1971): a ~1 kHz carrier, amplitude modulated at 100 symbols a
second, one frame a second. Each 10 ms symbol starts with the carrier at its high level for
2 ms (binary 0), 5 ms (binary 1) or 8 ms (position marker P). A frame starts at the leading edge
of the reference marker Pr, the second of two consecutive markers (P0 of the previous frame,
then Pr). Fields are BCD: seconds (symbols 1-8), minutes (10-17), hours (20-26), day of year
(30-41); straight binary seconds of the day (SBS) occupy symbols 80-97. A frame is accepted only
when its SBS matches its hours, minutes and seconds (the same check as the legacy decoder,
``Apollo_Audio_Processing/processing_scripts/irig_b.py``, option -m) and every marker is where
the format puts it.

This is a vectorized reimplementation of that legacy decoder's logic, not a copy: the carrier
is demodulated against 1 kHz, the envelope is sliced at a level that follows the local high and
low levels, and symbols are classified by their high time. Frame times are the interpolated
envelope crossing at Pr's leading edge, in original sample seconds. Envelope filters are
zero-phase, so the remaining bias is a constant fraction of a millisecond that is the same for
every frame and cancels in every rate measurement.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass, asdict

import numpy as np
from numba import njit
from scipy import signal

CARRIER_HZ = 1000.0
ENV_RATE = 8000            # envelope sample rate after decimation
SYMBOL_S = 0.010
SPACING_TOL = 0.25         # a symbol's leading edge within this share of 10 ms of the expected time
LEVEL_WINDOW_S = 0.5       # high/low level tracking window


@dataclass
class Frame:
    t: float                # original-file seconds of Pr's leading edge
    day: int
    hours: int
    minutes: int
    seconds: int
    sbs: int
    quality: float          # median symbol decision margin (0..0.5) across the frame
    strict: bool            # fully valid frame (label trusted); otherwise timing only
    marks: int              # position markers found in place (of 11)

    @property
    def sod(self) -> int:
        return self.hours * 3600 + self.minutes * 60 + self.seconds

    @property
    def clock(self) -> int:  # seconds since 1971-01-01 00:00 GMT (day 1 = Jan 1)
        return (self.day - 1) * 86400 + self.sod

    def row(self) -> dict:
        return {**asdict(self), 'clock': self.clock}


def ffmpeg_reader(path, channel: int | None = None, sr: int = 44100, start_s: float = 0.0,
                  duration_s: float | None = None):
    """A process streaming float32 samples of one channel (mono files: channel None)."""
    cmd = ['ffmpeg', '-v', 'error', '-nostdin']
    if start_s:
        cmd += ['-ss', f'{start_s:.6f}']
    if duration_s is not None:
        cmd += ['-t', f'{duration_s:.6f}']
    cmd += ['-i', str(path)]
    if channel is not None:
        cmd += ['-af', f'pan=mono|c0=c{channel}']
    cmd += ['-f', 'f32le', '-ac', '1', '-ar', str(sr), '-']
    return subprocess.Popen(cmd, stdout=subprocess.PIPE, bufsize=1 << 22)


def read_blocks(proc, block: int):
    """Yield float32 arrays of ``block`` samples (the last may be shorter)."""
    nbytes = block * 4
    pending = b''
    while True:
        chunk = proc.stdout.read(nbytes - len(pending))
        if not chunk:
            break
        pending += chunk
        if len(pending) >= nbytes:
            yield np.frombuffer(pending, np.float32)
            pending = b''
    if len(pending) >= 4:
        yield np.frombuffer(pending[:len(pending) // 4 * 4], np.float32)
    proc.wait()
    if proc.returncode:
        raise RuntimeError(f'ffmpeg exited with {proc.returncode}')


class Envelope:
    """Complex demodulation at 1 kHz, low-passed and decimated to ENV_RATE."""

    def __init__(self, sr: int):
        if sr % ENV_RATE and sr != 44100:
            raise ValueError(f'unsupported sample rate {sr}')
        self.sr = sr
        self.pre = signal.butter(4, [600, 1500], btype='bandpass', fs=sr, output='sos')
        self.lp = signal.butter(4, 400, fs=sr, output='sos')

    def __call__(self, x: np.ndarray, t0: float) -> tuple[np.ndarray, np.ndarray]:
        x = signal.sosfiltfilt(self.pre, x.astype(np.float64))
        n = np.arange(len(x))
        bb = x * np.exp(-2j * np.pi * CARRIER_HZ * (n / self.sr + t0))
        bb = signal.sosfiltfilt(self.lp, bb)
        step = self.sr / ENV_RATE
        idx = np.arange(0, len(x) - 1, step)
        i0 = idx.astype(np.int64)
        env = np.abs(bb[i0] + (bb[np.minimum(i0 + 1, len(x) - 1)] - bb[i0]) * (idx - i0))
        return t0 + idx / self.sr, env


def slice_levels(env: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Local high and low envelope levels (90th and 10th percentile per window, interpolated)."""
    w = int(LEVEL_WINDOW_S * ENV_RATE)
    nblk = max(1, len(env) // w)
    blocks = env[:nblk * w].reshape(nblk, w) if len(env) >= w else env[None, :]
    hi = np.percentile(blocks, 90, axis=1)
    lo = np.percentile(blocks, 10, axis=1)
    centres = (np.arange(len(hi)) + .5) * blocks.shape[1]
    pos = np.arange(len(env))
    return np.interp(pos, centres, hi), np.interp(pos, centres, lo)


@njit(cache=True)
def _window_mean(e, a, b):
    ia, ib = int(np.floor(a)), int(np.ceil(b))
    if ia < 0 or ib > len(e) or ib <= ia:
        return np.nan
    return e[ia:ib].mean()


@njit(cache=True)
def _track(e, edge, period0, min_edge):
    """Symbol-clock tracker on the normalized envelope ``e`` (0 = low level, 1 = high level).

    Returns per symbol: leading-edge position (env samples, fractional), code (0, 1, 2 = P),
    quality (smallest distance of a window mean from the 0.5 decision level) and whether the
    edge was measured (True) or flywheeled (False).
    """
    n = len(e)
    pos = np.empty(n // 40 + 1)
    code = np.empty(n // 40 + 1, np.int8)
    qual = np.empty(n // 40 + 1)
    seen = np.empty(n // 40 + 1, np.bool_)
    m = 0
    locked = False
    p = 0.0
    P = period0
    misses = 0
    i = 0
    while True:
        if not locked:
            # acquisition: phase and period that maximise the edge comb over 12 symbols
            if i + 13 * period0 * 1.02 + 4 >= n:
                break
            best, bp, bP = -1e9, 0.0, period0
            for k in range(-8, 9):
                T = period0 * (1 + 0.0015 * k)
                for ph in range(int(period0)):
                    s = 0.0
                    for j in range(12):
                        q = int(i + ph + j * T + .5)
                        s += edge[q]
                    if s > best:
                        best, bp, bP = s, i + ph, T
            if best / 12 < min_edge:
                i += int(period0 * 6)
                continue
            locked, p, P, misses = True, bp, bP, 0
            while p - 8 < 0:  # too close to the buffer start to measure: take the next symbol
                p += P
        if p + P + 8 >= n:
            break
        # measure the edge near p
        c = int(p + .5)
        bi, bv = c, -1e9
        for q in range(c - 5, c + 6):
            if edge[q] > bv:
                bv, bi = edge[q], q
        measured = bv >= min_edge and abs(bi - p) < 5
        if measured:
            a, b0, cc = edge[bi - 1], edge[bi], edge[bi + 1]
            den = a - 2 * b0 + cc
            frac = 0.5 * (a - cc) / den if den != 0 else 0.0
            if frac > .5:
                frac = .5
            if frac < -.5:
                frac = -.5
            err = bi + frac - p
            p += 0.6 * err
            P += 0.02 * err
            misses = 0
        else:
            misses += 1
        u = P / 10.0  # one nominal millisecond
        A = _window_mean(e, p + .6 * u, p + 1.6 * u)
        B = _window_mean(e, p + 2.8 * u, p + 4.4 * u)
        C = _window_mean(e, p + 5.8 * u, p + 7.4 * u)
        D = _window_mean(e, p + 8.4 * u, p + 9.6 * u)
        if np.isnan(A + B + C + D):
            break
        if B < .5:
            k = 0
            q = min(A - .5, .5 - B)
        elif C < .5:
            k = 1
            q = min(A - .5, B - .5, .5 - C)
        else:
            k = 2
            q = min(A - .5, B - .5, C - .5)
        q = min(q, .5 - D)
        pos[m], code[m], qual[m], seen[m] = p, k, q, measured
        m += 1
        if misses > 20 or P < period0 * .97 or P > period0 * 1.03:
            locked = False
            i = int(p + P)
            continue
        p += P
    return pos[:m], code[:m], qual[:m], seen[:m]


def symbols(times: np.ndarray, env: np.ndarray, min_quality: float = 0.0):
    """(leading-edge seconds, code 0/1/2=P/-1 doubtful, quality) for every tracked symbol."""
    hi, lo = slice_levels(env)
    span = np.maximum(hi - lo, 1e-12)
    e = np.clip((env - lo) / span, -1, 2)
    sm = signal.convolve(e, np.ones(3) / 3, mode='same')
    edge = np.zeros_like(sm)
    edge[2:-2] = (sm[4:] - sm[:-4]) / 2  # rise over 4 env samples (0.5 ms)
    period0 = SYMBOL_S * ENV_RATE
    pos, code, qual, seen = _track(e, edge, period0, 0.12)
    t = np.interp(pos, np.arange(len(times)), times)
    code = np.where(qual >= min_quality, code, -1).astype(np.int8)
    return t, code, qual


def _bcd(bits, weights):
    return int(sum(b * w for b, w in zip(bits, weights)))


MARKS = np.r_[0, np.arange(9, 100, 10)]
NOT_MARKS = np.setdiff1d(np.arange(100), MARKS)


def fields(code: np.ndarray) -> tuple[int, int, int, int, int]:
    """(day, hours, minutes, seconds, sbs) read from 100 symbol codes (no validation)."""
    b = (code == 1).astype(int)
    seconds = _bcd(b[1:5], (1, 2, 4, 8)) + 10 * _bcd(b[6:9], (1, 2, 4))
    minutes = _bcd(b[10:14], (1, 2, 4, 8)) + 10 * _bcd(b[15:18], (1, 2, 4))
    hours = _bcd(b[20:24], (1, 2, 4, 8)) + 10 * _bcd(b[25:27], (1, 2))
    day = (_bcd(b[30:34], (1, 2, 4, 8)) + 10 * _bcd(b[35:39], (1, 2, 4, 8))
           + 100 * _bcd(b[40:42], (1, 2)))
    sbs = _bcd(b[80:89], [1 << i for i in range(9)]) + (_bcd(b[90:98], [1 << i for i in range(8)]) << 9)
    return day, hours, minutes, seconds, sbs


def frames_from_symbols(tr, code, qual, min_marks: int = 9) -> list[Frame]:
    """Frame candidates: a marker pair followed by 100 symbols on the 10 ms grid.

    ``strict``: all 11 markers in place, no stray marker, fields in range
    and SBS equal to the hours, minutes and seconds. Other candidates (at least ``min_marks``
    markers in place, at most one stray) carry timing only; their label must come from strict
    neighbours.
    """
    out = []
    if len(tr) < 101:
        return out
    pr = np.flatnonzero((code[1:] == 2) & (code[:-1] == 2)) + 1
    for i in pr:
        if i + 100 > len(tr):
            break
        gaps = np.diff(tr[i:i + 100]) / SYMBOL_S
        if np.any(np.abs(gaps - 1) > SPACING_TOL):
            continue
        c = code[i:i + 100]
        marks = int(np.count_nonzero(c[MARKS] == 2))
        stray = int(np.count_nonzero(c[NOT_MARKS] == 2))
        if marks < min_marks or stray > 1:
            continue
        day, h, m, s, sbs = fields(c)
        strict = bool(marks == 11 and stray == 0
                      and 0 <= s < 60 and 0 <= m < 60 and 0 <= h < 24 and 1 <= day <= 366
                      and sbs == h * 3600 + m * 60 + s)
        out.append(Frame(float(tr[i]), day, h, m, s, sbs, float(np.median(qual[i:i + 100])), strict, marks))
    return out


def decode_stream(path, channel: int | None = None, sr: int = 44100, block_s: float = 60.0,
                  overlap_s: float = 2.0, start_s: float = 0.0, duration_s: float | None = None,
                  progress=None) -> tuple[list[Frame], float]:
    """Decode every frame in a file. Returns (frames sorted by time, decoded duration seconds)."""
    env_of = Envelope(sr)
    block, ov = int(block_s * sr), int(overlap_s * sr)
    proc = ffmpeg_reader(path, channel, sr, start_s, duration_s)
    frames: dict[int, Frame] = {}
    tail = np.empty(0, np.float32)
    consumed = 0  # samples of the stream before ``tail``
    for x in read_blocks(proc, block):
        buf = np.r_[tail, x]
        t0 = start_s + consumed / sr
        times, env = env_of(buf, t0)
        tr, code, qual = symbols(times, env)
        # keep frames whose start lies in this buffer's interior (away from filter edges)
        lo_t = t0 + (0.5 if consumed else 0.0)
        for f in frames_from_symbols(tr, code, qual):
            if f.t >= lo_t:
                frames.setdefault(int(round(f.t * 100)), f)
        keep = min(len(buf), ov + int(1.2 * sr))  # a frame plus margin carried into the next block
        consumed += len(buf) - keep
        tail = buf[-keep:]
        if progress:
            progress(start_s + (consumed + keep) / sr)
    total = start_s + (consumed + len(tail)) / sr
    out = sorted(frames.values(), key=lambda f: f.t)
    # drop duplicates closer than half a second (overlap re-decodes)
    dedup = []
    for f in out:
        if dedup and f.t - dedup[-1].t < 0.5:
            if (f.strict, f.quality) > (dedup[-1].strict, dedup[-1].quality):
                dedup[-1] = f
            continue
        dedup.append(f)
    return dedup, total
