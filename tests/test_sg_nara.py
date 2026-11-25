"""NARA flight tapes: IRIG-B decoding, file alignment evidence and the shared timing map."""
from pathlib import Path
import sys
import tempfile
import unittest

import numpy as np
import soundfile as sf

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from _bootstrap import install as _install
_install()
import nara_align
import nara_irig
import nara_tape_map

SR = 44100


def bcd(value, weights):
    bits = []
    for w in sorted(weights, reverse=True):
        bits.append(1 if value >= w else 0)
        value -= w * bits[-1]
    return bits[::-1]


def frame_codes(day, h, m, s):
    """100 IRIG-B symbols: 2 = position marker, else the bit value."""
    c = [0] * 100
    for p in [0, *range(9, 100, 10)]:
        c[p] = 2
    c[1:5] = bcd(s % 10, (1, 2, 4, 8)); c[6:9] = bcd(s // 10, (1, 2, 4))
    c[10:14] = bcd(m % 10, (1, 2, 4, 8)); c[15:18] = bcd(m // 10, (1, 2, 4))
    c[20:24] = bcd(h % 10, (1, 2, 4, 8)); c[25:27] = bcd(h // 10, (1, 2))
    c[30:34] = bcd(day % 10, (1, 2, 4, 8)); c[35:39] = bcd(day // 10 % 10, (1, 2, 4, 8))
    c[40:42] = bcd(day // 100, (1, 2))
    sbs = h * 3600 + m * 60 + s
    c[80:89] = [(sbs >> i) & 1 for i in range(9)]
    c[90:98] = [(sbs >> (9 + i)) & 1 for i in range(8)]
    return c


def irig_signal(seconds, start_sod, day=215, speed=1.0, noise=0.0, seed=0):
    """IRIG-B played back ``speed`` times too fast. Returns (samples, true Pr times in file seconds)."""
    n = int(seconds / speed * SR) + SR
    file_t = np.arange(n) / SR
    tape_t = file_t * speed  # recorded seconds
    env = np.full(n, 1 / 3.3)
    pr = []
    for k in range(int(seconds) + 1):
        sod = start_sod + k
        codes = frame_codes(day, sod // 3600, sod // 60 % 60, sod % 60)
        for i, code in enumerate(codes):
            a = k + i * 0.01
            high = (0.002, 0.005, 0.008)[code]
            env[(tape_t >= a) & (tape_t < a + high)] = 1.0
        pr.append(k / speed)
    x = env * np.sin(2 * np.pi * 1000 * tape_t)
    x += noise * np.random.default_rng(seed).standard_normal(n)
    return (0.3 * x).astype(np.float32), np.array(pr)


class DecoderTests(unittest.TestCase):
    def decode(self, x):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'irig.wav'
            sf.write(path, x, SR, subtype='PCM_24')
            return nara_irig.decode_stream(path, block_s=20.0)[0]

    def test_frames_decode_with_labels_and_times_under_speed_error(self):
        x, pr = irig_signal(45, 11 * 3600 + 27 * 60 + 31, speed=1.003, noise=0.02)
        frames = [f for f in self.decode(x) if f.strict]
        self.assertGreater(len(frames), 40)
        for f in frames:
            self.assertEqual(f.day, 215)
            k = f.sod - (11 * 3600 + 27 * 60 + 31)
            self.assertLess(abs(f.t - pr[k]), 0.0005, f)  # Pr edge to half a millisecond

    def test_sbs_check_rejects_a_misread_frame(self):
        codes = frame_codes(215, 11, 27, 31)
        codes[2] = 1 - codes[2]  # seconds field misread; SBS no longer agrees
        day, h, m, s, sbs = nara_irig.fields(np.array(codes))
        self.assertNotEqual(sbs, h * 3600 + m * 60 + s)


class MapTests(unittest.TestCase):
    def test_misread_frames_do_not_split_or_bend_the_map(self):
        t = np.arange(0, 4000, 1.0)
        rate = 1500e-6 + 300e-6 * t / t[-1]      # speed error drifting along the tape
        o = 1.8e7 + np.cumsum(np.r_[0, rate[:-1]])  # GMT - file seconds
        o += np.random.default_rng(1).normal(0, 3e-4, len(t))
        o[[500, 501, 2000]] += [86400, 86400, 3600]  # misread day / hour fields
        runs = nara_tape_map.chains(t, o)
        segs = nara_tape_map.segments(t, o, runs)
        self.assertEqual(len(segs), 1)
        self.assertEqual(len(segs[0]), len(t) - 3)
        bt, f, res = nara_tape_map.fit_segment(t[segs[0]], o[segs[0]])
        self.assertLess(np.sqrt(np.mean(res ** 2)), 5e-4)
        self.assertLess(np.max(np.abs(f(t[100:-100], 1) - rate[100:-100])), 30e-6)

    def test_a_recorder_stop_starts_a_new_segment(self):
        t = np.arange(0, 2000, 1.0)
        o = 1.8e7 + 1e-3 * t
        o[t >= 1000] += 42.0  # 42 s of clock elapsed while the recorder was stopped
        segs = nara_tape_map.segments(t, o, nara_tape_map.chains(t, o))
        self.assertEqual([len(s) for s in segs], [1000, 1000])


class AlignmentTests(unittest.TestCase):
    def test_timecode_offset_from_shared_seconds(self):
        g = np.arange(1000.0)
        fa = (g * 1.002 + 0.25, g)
        fb = (g[100:] * 1.002 + 0.25 + 80.4047, g[100:])
        off, mad, n = nara_align.irig_offset(fa, fb)
        self.assertAlmostEqual(off, 80.4047, places=6)
        self.assertEqual(n, 900)

    def test_candidate_offset_survives_misread_labels(self):
        rng = np.random.default_rng(2)
        g = np.arange(2000.0)
        fa = (g + 0.1, g)
        tb = g[::5] + 0.1 + 1.1201
        gb = g[::5].copy()
        bad = rng.random(len(gb)) < 0.6
        gb[bad] = rng.integers(0, 2000, bad.sum())  # most bleed frames misread
        off, count, rival = nara_align.candidate_offset(fa, (tb, gb))
        self.assertAlmostEqual(off, 1.1201, places=4)

    def test_audio_offset_between_files_cut_at_different_points(self):
        rng = np.random.default_rng(3)
        rate = nara_align.RATE
        programme = rng.standard_normal(int(1200 * rate)).astype(np.float32)
        shift = int(round(43.8362 * rate))
        a, b = programme[shift:], programme  # b started 43.8362 s earlier
        r = nara_align.audio_offset(a + 0.3 * rng.standard_normal(len(a)).astype(np.float32),
                                    b + 0.3 * rng.standard_normal(len(b)).astype(np.float32))
        self.assertEqual(r['status'], 'ok')
        self.assertAlmostEqual(r['offset_s'], 43.8362, places=3)


if __name__ == '__main__':
    unittest.main()
