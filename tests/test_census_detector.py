"""Synthetic checks of the blind, check-tone-free tape-motion detector."""
from pathlib import Path
import sys
import unittest

import numpy as np
from scipy import signal

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'scripts'))
sys.path.insert(0, str(ROOT/'tests'))
from _bootstrap import install
install()
import flutter_census as census
from census_fixtures import SR, channels, flutter, fm_tone

SETTINGS = census.make_settings()
LOW = SR/SETTINGS['decimation']


def band(e):
    return signal.sosfiltfilt(signal.butter(3, [2, 30], fs=census.RATE, btype='band', output='sos'), e)


def truth(seconds):
    return band(flutter(np.arange(int(seconds*census.RATE))/census.RATE))


def decimated(x):
    return np.asarray(signal.resample_poly(np.asarray(x, np.float32), 1, SETTINGS['decimation']), float)


def analyse(audio, seconds, tape='A15_T900', **overrides):
    settings = census.make_settings(**overrides) if overrides else SETTINGS
    return census.analyse_block(lambda ch: (audio[ch], 1.), sorted(audio), tape, 0., seconds, settings)


class SpectrumTests(unittest.TestCase):
    def test_baseband_wrapped_crop_and_partial_oscillator_tile(self):
        x = np.random.default_rng(7).normal(size=8051)
        spec = census.BlockSpectrum(x, SR)
        cap, rate = 100., 1000.
        length = len(x)//8
        for hz in (996.3, 1517.2):
            with self.subTest(hz=hz):
                z, r = census.baseband(spec, hz, cap, rate)
                bins = np.arange(round((hz-cap)/spec.df), round((hz+cap)/spec.df)+1)
                folded = np.zeros(length, complex)
                folded[bins % length] = np.fft.rfft(x)[bins]
                centre = hz-round(hz/r)*r
                mixed = 8*np.fft.ifft(folded)*np.exp(-2j*np.pi*centre*np.arange(length)/r)
                expected = signal.sosfiltfilt(signal.butter(6, cap, fs=r, output='sos'), mixed)
                np.testing.assert_allclose(z, expected, rtol=1e-12, atol=1e-12)

    def test_batched_welch_matches_scipy_across_batches_and_unpaired_bins(self):
        rng = np.random.default_rng(72)
        for nper, dtype in [(5333, np.float64), (16000, np.float32)]:
            x = rng.normal(size=350123).astype(dtype)
            x += 4  # Each frame has its own mean removed before windowing.
            for data in (x, x[:nper]):
                with self.subTest(nper=nper, length=len(data)):
                    f, p = census._welch(data, SR, nper)
                    expected_f, expected_p = signal.welch(data, SR, nperseg=nper)
                    np.testing.assert_array_equal(f, expected_f)
                    self.assertEqual(p.dtype, expected_p.dtype)
                    np.testing.assert_allclose(p, expected_p,
                                               rtol=1e-6 if dtype == np.float32 else 1e-11)


class StationaryRemovalTests(unittest.TestCase):
    def test_refined_harmonics_and_partial_subblocks_match_dense_fit(self):
        rng = np.random.default_rng(32)
        hz = np.array([59.981, 120.016, 299.973])
        n = int(1.3*LOW)
        for tail in (int(LOW)-1, int(LOW)+9):
            x = rng.normal(size=2*n+tail)
            expected = x.copy()
            for first in range(0, len(x), n):
                part = x[first:first+n]
                if len(part) < LOW:
                    continue
                phase = 2*np.pi*np.outer(np.arange(len(part))/LOW, hz)
                for wave in phase.T:
                    basis = np.column_stack((np.cos(wave), np.sin(wave)))
                    expected[first:first+n] -= basis @ np.linalg.lstsq(basis, part, rcond=None)[0]
            np.testing.assert_allclose(census.remove_stationary(x, hz, LOW, 1.3),
                                       expected, rtol=0, atol=1e-12)

    def test_hum_beside_40_db_stronger_digitization_line_needs_removal(self):
        t = np.arange(64*SR)/SR
        e = flutter(t)
        rng = np.random.default_rng(3)
        x = fm_tone(t, 59.1, e, .003)+.3*np.sin(2*np.pi*60*t+.4)+.0005*rng.standard_normal(len(t))
        xd = decimated(x)
        lines = census.exact_mains(xd, LOW, SETTINGS, fmax=1250)
        self.assertTrue(any(abs(f-60) < .05 for f in lines))
        edge = slice(4*census.RATE, -4*census.RATE)
        expected = truth(64)[edge]
        cleaned = census.track(census.BlockSpectrum(census.remove_stationary(xd, lines, LOW), LOW),
                               59.1, SETTINGS['tracking'])
        raw = census.track(census.BlockSpectrum(xd, LOW), 59.1, SETTINGS['tracking'])
        self.assertGreater(census.correlation(cleaned['e'][edge].astype(float), expected), .9)
        self.assertLess(abs(census.correlation(raw['e'][edge].astype(float), expected)), .5)

    def test_batched_removal_matches_independent_least_squares(self):
        t = np.arange(40*SR)/SR
        x = .2*np.sin(2*np.pi*60*t+.3)+.05*np.sin(2*np.pi*180*t+1.)+.01*np.random.default_rng(1).standard_normal(len(t))
        fast = census.remove_stationary(x, [60., 180.], SR, 16.)
        slow = x.copy()
        for hz in (60., 180.):
            for a in range(0, len(x), 16*SR):
                part = slice(a, min(a+16*SR, len(x)))
                basis = np.column_stack((np.cos(2*np.pi*hz*t[part]), np.sin(2*np.pi*hz*t[part])))
                slow[part] -= basis @ np.linalg.lstsq(basis, slow[part], rcond=None)[0]
        self.assertLess(np.max(abs(fast-slow)), 1e-6)


class ConsensusTests(unittest.TestCase):
    def test_blind_consensus_recovers_motion_on_every_window(self):
        audio, _ = channels(140)
        result, curves = analyse(audio, 140)
        expected = truth(140)
        self.assertEqual(result['windows_eligible'], len(result['windows']))
        for window in result['windows']:
            curve = curves[window['curve_index']].astype(float)
            i0 = int(window['valid_start_seconds']*census.RATE)
            reference = expected[i0:i0+len(curve)]
            self.assertGreater(census.correlation(curve, reference), .98)
            self.assertAlmostEqual(curve.std()/reference.std(), 1, delta=.1)
            self.assertGreaterEqual(len(window['families']), 2)

    def test_brief_peaks_are_bounded_and_saturated_estimates_rejected(self):
        n = int(64*census.RATE)
        t = np.arange(n)/census.RATE
        rng = np.random.default_rng(4)

        def window(curve):
            members = []
            for i, (ch, hz) in enumerate([(1, 59.2), (2, 118.4), (3, 431.), (4, 987.), (5, 640.)]):
                e = curve+.001*rng.standard_normal(n)
                members.append((dict(channel=ch, centre=hz), e.astype(np.float32), np.ones(n, bool)))
            return census.screen_window(members, {c: c for c in range(1, 6)}, SETTINGS)

        brief = .02*np.sqrt(2)*np.sin(2*np.pi*6.6*t)
        brief[(t > 30) & (t < 30.06)] = .06  # One short excursion beyond the bound.
        record, consensus = window(brief)
        self.assertGreater(record['peak_speed_percent'], 4.9)
        self.assertLessEqual(record['clipped_fraction'], .01)
        self.assertTrue(record['eligible'], record['reasons'])
        self.assertLessEqual(float(abs(consensus).max()), .049)
        record, consensus = window(.038*np.sqrt(2)*np.sin(2*np.pi*6.6*t))
        self.assertEqual(record['reasons'], ['speed_bound'])
        self.assertLessEqual(float(abs(consensus).max()), .049)

    def test_inverted_mirror_cluster_of_equal_fm_loses_to_more_channels(self):
        n = int(64*census.RATE)
        rng = np.random.default_rng(8)
        motion = band(rng.standard_normal(n))
        motion *= .02/motion.std()
        members = []
        for ch, hz in [(1, 59.2), (2, 118.4), (3, 431.), (4, 987.), (5, 640.), (6, 59.3)]:
            members.append((dict(channel=ch, centre=hz), (motion+.001*rng.standard_normal(n)).astype(np.float32),
                            np.ones(n, bool)))
        for ch, hz in [(7, 60.6), (8, 121.2), (9, 180.9)]:  # Mirror products: inverted, slightly larger FM.
            members.append((dict(channel=ch, centre=hz), (-1.05*motion+.001*rng.standard_normal(n)).astype(np.float32),
                            np.ones(n, bool)))
        record, consensus = census.screen_window(members, {c: c for c in range(1, 10)}, SETTINGS)
        self.assertEqual(len([c for c in record['clusters'] if c['size'] >= 3]), 2)
        self.assertTrue(record['sign_conflict'])
        self.assertEqual(record['cluster_size'], 6)
        edge = slice(4*census.RATE, -4*census.RATE)
        self.assertGreater(census.correlation(consensus, motion[edge]), .99)

    def test_stationary_line_disagrees_and_loses_concentration_under_dewarp(self):
        seconds = 64
        t = np.arange(seconds*SR)/SR
        e = flutter(t)
        stationary = .1*np.sin(2*np.pi*700*t)
        fluttered = fm_tone(t, 700, e, .1)
        x = decimated(stationary)
        result = census.track(census.BlockSpectrum(x, LOW), 700, SETTINGS['tracking'])
        edge = slice(4*census.RATE, -4*census.RATE)
        self.assertLess(abs(census.correlation(result['e'][edge].astype(float), truth(seconds)[edge])), .2)
        curve = e[::SR//census.RATE]
        before = census.concentration(x, 700, LOW)
        self.assertLess(census.concentration(census.dewarp(x, curve, LOW), 700, LOW), .5*before)
        y = decimated(fluttered)
        plain = census.concentration(y, 700, LOW)
        right = census.concentration(census.dewarp(y, curve, LOW), 700, LOW)
        self.assertGreater(right, 2*plain)
        # Pure sinusoidal motion can re-concentrate at a Bessel null; compare signs.
        self.assertGreater(right, 2*census.concentration(census.dewarp(y, -curve, LOW), 700, LOW))

    def test_post_tape_common_comb_loses_to_on_tape_motion(self):
        t = np.arange(100*SR)/SR
        comb_motion = .0015*np.sqrt(2)*np.sin(2*np.pi*8.3*t+.2)
        audio, _ = channels(100)
        for ch in audio:
            extra = sum(fm_tone(t, hz, comb_motion, .02, ch) for hz in (1020.3, 1140.3, 1260.3))
            audio[ch] = (audio[ch]+extra).astype(np.float32)
        result, curves = analyse(audio, 100)
        for window in result['windows']:
            self.assertGreaterEqual(len(window['clusters']), 2, window['clusters'])
            self.assertTrue(any(c['median_fm_percent'] < .3 for c in window['clusters']))
            self.assertGreater(window['cluster_fm_percent'], 1.)
            self.assertTrue(window['eligible'])
            curve = curves[window['curve_index']].astype(float)
            i0 = int(window['valid_start_seconds']*census.RATE)
            self.assertGreater(census.correlation(curve, truth(100)[i0:i0+len(curve)]), .95)

    def test_copied_channels_count_once(self):
        base, _ = channels(70, count=3, carriers={1: 431., 2: 987., 3: 640.})
        copies = dict(base)
        copies[4], copies[5] = base[3].copy(), base[3].copy()
        result, _ = analyse(copies, 70)
        self.assertIn([3, 4, 5], result['duplicate_groups'])
        window = result['windows'][0]
        self.assertEqual(sorted(int(k) for k in window['leave_one_out']), [1, 2, 3])
        self.assertEqual(len(window['agreeing_groups']), 3)
        # Two independent channels plus copies are never three witnesses.
        pair = {1: base[1], 2: base[2], 3: base[2].copy(), 4: base[2].copy()}
        result, _ = analyse(pair, 70)
        self.assertFalse(result['windows'][0]['eligible'])
        audit = census.duplicate_groups({1: np.ones(4), 2: np.arange(4.)}, .95, known=[[1, 2]])
        self.assertEqual(audit, {1: 1, 2: 1})


class JoinAndAuditTests(unittest.TestCase):
    def test_overlap_add_is_continuous_and_never_fills_failed_windows(self):
        duration = 300.
        expected = truth(duration+1)
        starts = census.window_starts(0., duration, SETTINGS)
        self.assertTrue(all(abs(s*census.RATE-round(s*census.RATE)) < 1e-6 for s in starts))
        length = int((SETTINGS['window']['seconds']-2*SETTINGS['window']['edge_seconds'])*census.RATE)
        rng = np.random.default_rng(2)
        passing = []
        for i, start in enumerate(starts):
            if i == 3:
                continue  # A failed window between passing neighbours.
            valid = start+SETTINGS['window']['edge_seconds']
            i0 = int(round(valid*census.RATE))
            scale = 1+.03*rng.standard_normal()
            passing.append((valid, scale*expected[i0:i0+length]))
        speed, support = census.join_windows(passing, duration, SETTINGS)
        self.assertEqual(len(speed), int(duration*census.RATE)+1)
        gap = (support == 0)
        self.assertTrue(np.all(speed[gap] == 0))
        covered = support == 1
        self.assertGreater(census.correlation(speed[covered], expected[:len(speed)][covered]), .999)
        inner = covered & np.r_[covered[1:], False] & np.r_[False, covered[:-1]]
        jumps = abs(np.diff(speed))[inner[:-1]]
        self.assertLess(jumps.max(), 2*abs(np.diff(expected)).max())
        # Windows every 42 s with 56 s valid regions: dropping the fourth
        # leaves 144-172 s unsupported rather than extrapolated.
        self.assertEqual(starts[:5], [0., 42., 84., 126., 168.])
        self.assertTrue(np.all(support[144*census.RATE+1:172*census.RATE] == 0))
        self.assertTrue(np.all(support[89*census.RATE:144*census.RATE] == 1))
        with self.assertRaisesRegex(ValueError, 'bound'):
            census.join_windows([(4., np.full(length, .06))], duration, SETTINGS)

    def test_held_out_beep_controls_behave_as_measured(self):
        seconds = 64
        t = np.arange(seconds*SR)/SR
        # Random-phase narrowband motion: a strictly periodic synthetic curve
        # would make the 1.5 s shift control partially "correct" by aliasing.
        noise = np.random.default_rng(9).standard_normal(seconds*census.RATE)
        slow = signal.sosfiltfilt(signal.butter(4, [4, 10], fs=census.RATE, btype='band', output='sos'), noise)
        e = np.interp(t, np.arange(len(slow))/census.RATE, .015*slow/slow.std())
        keyed = np.zeros(len(t))
        for start in np.arange(8, 56, 3.):
            keyed[(t >= start) & (t < start+.25)] = 1.
        x = fm_tone(t, 2500., e, .3)*keyed+.0003*np.random.default_rng(5).standard_normal(len(t))
        beeps = census.detect_beeps(census.BlockSpectrum(x, SR), SR)
        self.assertGreaterEqual(len(beeps), 12)
        snippets = {7: [(b, dict(block_start=0., snippet_start=max(0., b['start']-1)),
                         x[int(max(0., b['start']-1)*SR):int((b['end']+1)*SR)]) for b in beeps]}
        curve = band(e[::SR//census.RATE])
        gate = np.zeros(len(curve))
        gate[4*census.RATE:-4*census.RATE] = 1.
        audit = census.beep_audit(snippets, 0., curve*gate, gate, SR, SETTINGS)
        self.assertLess(audit['curve_ratio'], .5)
        self.assertGreater(audit['wrong_sign_ratio'], 1.)
        self.assertGreater(audit['shift_ratio'], .9)
        self.assertTrue(audit['controls_pass'])
        self.assertEqual(audit['worse'], 0)

    def test_checks_beeps_never_enter_the_estimate(self):
        f = np.linspace(40, 4000, 20000)
        level = np.full(len(f), -60.)
        for hz in (2500., 700.):
            level += 30*np.exp(-.5*((f-hz)/5)**2)
        found = [r['hz'] for r in census.candidates(f, level, 50, 3900, SETTINGS['candidates'])]
        self.assertTrue(any(abs(h-700) < 5 for h in found))
        self.assertFalse(any(2150 < h < 2850 for h in found))


if __name__ == '__main__':
    unittest.main()
