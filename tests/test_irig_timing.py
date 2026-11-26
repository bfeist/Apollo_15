"""Stage 07i: time-code tape curve, gap bridging, trend integration and offset measurement."""

import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import _bootstrap  # noqa: E402,F401
import irig_timing as it  # noqa: E402

LAUNCH = it._nara()[1].RANGE_ZERO_S


def synthetic(duration=20000.0, gap=(8000.0, 8400.0), misreads=40, seed=1):
    """Frames of a reel playing ~1.6 % slow, slowing a further 1 % late, with a gap and misread labels."""
    rng = np.random.default_rng(seed)

    def rate(x):  # dGMT/dt
        return 0.984 - 0.01 / (1 + np.exp(-(x - 16000) / 300.0))
    grid = np.arange(0, duration + 1, 0.5)
    gmt = LAUNCH + 3600 + np.r_[0, np.cumsum((rate(grid[1:]) + rate(grid[:-1])) / 2 * 0.5)]
    seconds = np.arange(np.ceil(gmt[0]), np.floor(gmt[-1]))
    t = np.interp(seconds, gmt, grid)
    keep = (rng.random(len(t)) < 0.6) & ~((t > gap[0]) & (t < gap[1]))
    t, clock = t[keep] + rng.normal(0, 0.0005, keep.sum()), seconds[keep].astype(float)
    bad = rng.choice(len(clock), misreads, replace=False)
    clock[bad] += rng.choice([-86400, 3600, 60, 600], misreads)          # wrong day/hour/minute fields
    return t, clock, grid, gmt, rate


class IrigCurveTests(unittest.TestCase):
    def test_curve_recovers_rate_and_rejects_misreads(self):
        t, clock, grid, gmt, rate = synthetic()
        c = it.IrigCurve(t, clock)
        self.assertEqual(len(c.segments), 1)
        self.assertGreaterEqual(c.rejected, 30)
        x = np.linspace(1000, 19000, 500)
        err = c.gmt(0, x) - np.interp(x, grid, gmt)
        self.assertLess(np.max(np.abs(err)), 0.005)
        self.assertLess(np.max(np.abs(c.rate(0, x) - rate(x))), 2e-4)

    def test_map_integrates_to_the_frames_across_a_gap(self):
        t, clock, grid, gmt, _ = synthetic()
        c = it.IrigCurve(t, clock)
        self.assertEqual(len(c.segments[0]['covered']), 2)        # the 400 s gap is not covered
        g, r, src = it.build_rate(c, 20000.0)
        self.assertIn('bridge', set(src))
        out = np.r_[0, np.cumsum((r[1:] + r[:-1]) / 2 * np.diff(g))]
        rows = it.placement(c, g, out, 20000.0)
        self.assertLess(rows[0]['frame_residual_rms_ms'], 2.0)

    def test_trend_integral_is_exact(self):
        t, clock, *_ = synthetic(misreads=0)
        c = it.IrigCurve(t, clock)
        x = np.linspace(c.trend_t[0], c.trend_t[-1], 20001)
        num = np.r_[0, np.cumsum((np.interp(x[1:], c.trend_t, c.trend_r) + np.interp(x[:-1], c.trend_t, c.trend_r))
                                 / 2 * np.diff(x))]
        exact = c._trend(x) - c._trend(x[:1])
        self.assertLess(np.max(np.abs(exact - num)), 1e-6)

    def test_hermite_meets_values_and_slopes(self):
        v0, d0 = it.hermite(np.array([10.0]), 10.0, 20.0, 1.0, 3.0, 0.5, -0.2)
        v1, d1 = it.hermite(np.array([20.0]), 10.0, 20.0, 1.0, 3.0, 0.5, -0.2)
        np.testing.assert_allclose([v0[0], d0[0], v1[0], d1[0]], [1.0, 0.5, 3.0, -0.2], atol=1e-12)

    def test_offset_from_sg_events(self):
        t, clock, *_ = synthetic(misreads=0)
        c = it.IrigCurve(t, clock)
        ts = np.linspace(500, 19500, 400)
        truth = c.gmt(0, ts) - LAUNCH
        ev = type('E', (), {})()
        ev.t, ev.g = ts, truth - 0.30 + np.random.default_rng(2).normal(0, 0.1, len(ts))
        ev.kind = np.array(['text'] * len(ts), object)
        ev.ref_kind = np.array(['ia'] * 200 + ['ia'] * 200, object)
        ev.bkey = np.array(['ia:a:0'] * 200 + ['ia:b:0'] * 200, object)
        off = it.timecode_offset(c, ev, LAUNCH)
        self.assertAlmostEqual(off['offset_s'], 0.30, delta=0.03)


if __name__ == '__main__':
    unittest.main()
