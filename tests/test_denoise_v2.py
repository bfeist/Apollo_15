from dataclasses import replace
import hashlib
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
from scipy import signal
import soundfile as sf

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from _bootstrap import install as _install
_install()
from denoise_v2 import SETTINGS, Settings, corrected_chunks, model_window, rising_cosine
from render_denoise_v2 import render


SR = SETTINGS.sample_rate


def tone(t, hz):
    return np.sin(2*np.pi*hz*t)


def synthetic(seconds=36):
    t = np.arange(seconds*SR)/SR
    noise = np.random.default_rng(815).normal(0, .0015, len(t))
    phase = .0015*tone(t, .37)
    hum = sum(level*np.sin(2*np.pi*hz*t + (hz/60)*phase + hz/1200)
              for hz, level in [(60,.020),(120,.012),(180,.007),(420,.004),
                                (780,.005),(3180,.004),(3420,.003)])
    return t, noise, hum


class DenoiseV2Tests(unittest.TestCase):
    def test_no_comb_preserves_voice_and_hiss(self):
        t, noise, _ = synthetic()
        pitch = 120 + 3*tone(t, .31)
        phase = 2*np.pi*np.cumsum(pitch)/SR
        syllables = rising_cosine((tone(t, .9)+.5)*2)
        wanted = noise + .06*syllables*sum(np.sin(k*phase)/k for k in range(1, 12))
        wanted += .002*tone(t, 3420)  # A persistent line cannot authorize a comb.
        removed, record = model_window(wanted)
        self.assertIn('bypass', record)
        np.testing.assert_array_equal(wanted-removed, wanted)

    def test_correlated_hum_subtraction_preserves_off_grid_content(self):
        t, noise, hum = synthetic()
        wanted = noise + .012*tone(t, 521)*rising_cosine((tone(t, .8)+.5)*2)
        removed, record = model_window(wanted+hum)
        center = slice(4*SR, -4*SR)
        self.assertIn(record['reference_hz'], (3180, 3420))
        self.assertLess(np.std((hum-removed)[center])/np.std(hum[center]), .08)
        bp = signal.butter(4, [500, 540], fs=SR, btype='bandpass', output='sos')
        self.assertLess(np.std(signal.sosfilt(bp, removed)[center]), 1e-5)

    def test_interrupted_comb_bypasses_or_fades_during_gap(self):
        t, noise, hum = synthetic()
        availability = 1-rising_cosine((t-12)*2)*rising_cosine((24-t)*2)
        removed, record = model_window(noise + hum*availability)
        inside = (t > 15) & (t < 21)
        self.assertLess(np.max(abs(removed[inside])), 1e-6)
        self.assertTrue(record.get('upper_bypass') or record.get('reference_off_intervals_frames'))

    def test_low_hum_survives_an_upper_reference_outage(self):
        t, noise, hum = synthetic(48)
        low = .025*tone(t, 60)+.011*tone(t, 120)
        upper = .005*tone(t, 180)+.004*tone(t, 3180)+.004*tone(t, 3420)
        outage = rising_cosine((t-14)*2)*rising_cosine((34-t)*2)
        x = noise + low + upper*(1-outage)
        removed, record = model_window(x)
        interior = (t >= 19) & (t < 29)
        self.assertTrue(any(r['kind'] == 'low_envelope' for r in record['components']))
        self.assertLess(np.std((x-removed-noise)[interior]), .001)
        # The independent 60 Hz path works with no candidate upper carrier.
        only_low, record = model_window(noise+low)
        self.assertEqual(record['upper_bypass'], 'no_coherent_reference')
        self.assertLess(np.std((low-only_low)[interior]), .001)

    def test_single_harmonic_dropout_does_not_inject_a_new_tone(self):
        t, noise, _ = synthetic(52)
        outage = rising_cosine((t-19)*2)*rising_cosine((32-t)*2)
        low = .025*tone(t, 60)+.012*tone(t, 120)
        main = .007*tone(t, 180)+.004*tone(t, 3180)+.004*tone(t, 3420)
        line = .009*tone(t, 780)*(1-outage)
        model, _ = model_window(noise+low+main+line)
        detector = signal.butter(4, [776, 784], fs=SR, btype='bandpass', output='sos')
        tracked = signal.sosfilt(detector, model)
        self.assertGreater(np.std(tracked[9*SR:15*SR]), .004)
        self.assertLess(np.std(tracked[23*SR:28*SR]), .000025)

    def test_tracked_refinement_reduces_changing_hum_and_stops(self):
        t, noise, hum = synthetic(48)
        shift = .22*t + .055*tone(t, .09)
        phase = 2*np.pi*(540*t+shift)
        changing = (.004+.003*rising_cosine((t-20)/8))*np.sin(phase)
        source = noise+hum+changing
        # This checks the adaptive iteration independently of the later
        # fixed low-harmonic projection.
        with patch('denoise_v2.suppress_persistent_low_residual',return_value=[]):
            initial, _ = model_window(source, settings=replace(SETTINGS, residual_passes=0))
            one, _ = model_window(source, settings=replace(SETTINGS, residual_passes=1))
            refined, record = model_window(source)
        band = signal.butter(4, [535, 545], fs=SR, btype='bandpass', output='sos')
        crop = slice(5*SR,-5*SR)
        def error(model):
            return np.std(signal.sosfilt(band, hum+changing-model)[crop])
        self.assertLess(error(refined), .65*error(initial))
        self.assertLessEqual(error(refined), 1.05*error(one))
        tone_row = next(r for r in record['refinement'] if r['hz'] == 540)
        self.assertTrue(tone_row['passes'])
        self.assertLessEqual(len(tone_row['passes']), 2)
        self.assertGreater(tone_row['tracked_hz_p10_p90'][0], 540)

    def test_masking_is_not_absence_for_established_phase_and_amplitude(self):
        from denoise_v2_refinement import supported_continuation, continue_coefficients
        t = np.arange(100)*.25
        known = .18*t
        masked = (t >= 8) & (t < 13)
        observed = known.copy()
        observed[masked] += np.linspace(0, 6, masked.sum())
        valid = ~masked
        absent = np.zeros_like(valid)
        phase, apply = supported_continuation(observed, valid, absent, .25)
        self.assertTrue(apply[masked].all())
        np.testing.assert_allclose(np.exp(1j*phase), np.exp(1j*known), atol=1e-12)
        groups = np.array([4.,8.,12.,16.])
        coeff = np.array([.02,0,0,.018], dtype=complex)
        held = continue_coefficients(coeff, coeff != 0, groups, t, apply, absent)
        self.assertEqual(held, [[4.,16.]])
        self.assertGreater(coeff[1].real, .018)
        absent[masked] = True
        _, apply = supported_continuation(observed, valid, absent, .25)
        self.assertFalse(apply[masked].any())

    def test_continuous_drifting_line_stays_suppressed_during_speech(self):
        t, noise, hum = synthetic(48)
        phase = 2*np.pi*(540*t+.22*t+.04*tone(t,.09))
        target = .008*np.sin(phase)
        mask = rising_cosine((t-19)*2)*rising_cosine((29-t)*2)
        voice = .015*mask*np.sin(2*np.pi*np.cumsum(109+4*tone(t,.38))/SR*5)
        rng = np.random.default_rng(2)
        bp = signal.butter(3, [350,2200], fs=SR, btype='bandpass', output='sos')
        breath = .025*mask*signal.sosfilt(bp, rng.normal(size=len(t)))
        removed, report = model_window(noise+hum+target+voice+breath)
        for a, b in ((9,18),(20,28),(30,39)):
            region = (t>=a)&(t<b)
            projection = 2*np.mean(removed[region]*np.exp(-1j*phase[region]))
            self.assertLess(abs(projection+.008j), .002)
        record = next(line for line in report['refinement'] if line['hz'] == 540)
        self.assertGreater(record['application_phase_fraction'], record['valid_fraction'])
        self.assertTrue(any(p['held_intervals_seconds'] for p in record['passes']))

    def test_shifted_comb_uses_supported_stationary_lines(self):
        from denoise_v2 import stationary_shifted_comb
        t = np.arange(60*SR)/SR
        rng = np.random.default_rng(153)
        indices = (15,19,25,29,35,41)
        shifted = sum(.007*tone(t, k*60.019) for k in indices)
        target = shifted+rng.normal(0,.002,len(t))
        starts = np.arange(0,len(t)-SR//2+1,SR//4)
        centers = (starts+SR//4)/SR
        win = np.hanning(SR//2)
        win /= win.sum()
        inner = (centers%2 >= .25) & (centers%2 <= 1.75)
        train = inner & ((centers//2)%2 == 0)
        check = inner & ~train
        model = np.zeros(len(t))
        records = stationary_shifted_comb(target,model,t,SR,starts,centers,
                                          win,train,check,SETTINGS)
        self.assertGreaterEqual(len(records),5)
        self.assertLess(np.std(shifted-model),.15*np.std(shifted))
        self.assertTrue(all(abs(row['hz']-row['harmonic']*60.019)<.01 for row in records))

    def test_compact_line_suppression_bounds_burst_tails(self):
        from denoise_v2_refinement import COMPACT_HALF_SECONDS, suppress_persistent_shifted_lines
        t = np.arange(60*SR)/SR
        phase = 2*np.pi*23*60.019*t
        rng = np.random.default_rng(1)
        dropout = rising_cosine((t-35)*2)*rising_cosine((46-t)*2)
        line = .004*np.sin(phase)*(1-dropout)
        noise = .0015*rng.normal(size=len(t)) + .004*tone(t, 1420)
        x = line+noise
        a,b = 26.,26.12
        changed = (t>=a)&(t<b)
        burst = .035*changed*np.sin(phase+.8)
        base = np.zeros(len(t))
        alternate = base.copy()
        self.assertEqual(suppress_persistent_shifted_lines(x,base,t,SR,.019), [])
        np.testing.assert_array_equal(base, np.zeros_like(base))
        records = suppress_persistent_shifted_lines(x,base,t,SR,.019,minimum_lines=1)
        suppress_persistent_shifted_lines(x+burst,alternate,t,SR,.019,minimum_lines=1)
        self.assertEqual([r['harmonic'] for r in records], [23])
        quiet = (t>=8)&(t<20)
        self.assertLess(np.std((line-base)[quiet]), .35*np.std(line[quiet]))
        gone = (t>=39)&(t<42)
        self.assertLess(np.std(base[gone]), .00003)
        for lo,hi in ((a-.3,a-.02),(b+.02,b+.3)):
            self.assertLess(np.std((alternate-base)[(t>=lo)&(t<hi)]), .0007)
        far = ((t>=a-1)&(t<a-COMPACT_HALF_SECONDS)) | ((t>b+COMPACT_HALF_SECONDS)&(t<=b+1))
        self.assertLess(np.std((alternate-base)[far]), .00001)
        side = abs(2*np.mean(base[quiet]*np.exp(-2j*np.pi*1420*t[quiet])))
        self.assertLess(side,.00002)

    def test_burst_at_refined_line_has_bounded_near_tails(self):
        t, noise, hum = synthetic(48)
        phase = 2*np.pi*(540*t+.22*t+.055*tone(t,.09))
        source = noise+hum+(.004+.003*rising_cosine((t-20)/8))*np.sin(phase)
        with patch('denoise_v2.suppress_persistent_low_residual',return_value=[]):
            baseline, record = model_window(source)
        self.assertTrue(next(r for r in record['refinement'] if r['hz']==540)['passes'])
        a, b = 27., 27.12
        window = rising_cosine((t-a)/.004)*rising_cosine((b-t)/.004)
        burst = window*.035*np.sin(phase+.8)
        with patch('denoise_v2.suppress_persistent_low_residual',return_value=[]):
            changed, _ = model_window(source+burst)
        bp = signal.butter(4, [300, 3000], fs=SR, btype='bandpass', output='sos')
        delta = signal.sosfilt(bp, changed-baseline)
        for lo, hi in ((a-.3,a-.02), (b+.02,b+.3)):
            self.assertLess(np.std(delta[(t>=lo)&(t<hi)]), .001)
        far = ((t>=a-2)&(t<a-.3)) | ((t>b+.3)&(t<=b+2))
        self.assertLess(np.std(delta[far]), .00025)
        inside = (t>=a)&(t<b)
        self.assertLess(np.std(delta[inside]), .07*np.std(burst[inside]))

    def test_reference_burst_has_bounded_pre_and_post_effect(self):
        t, noise, hum = synthetic()
        raw = noise+hum
        baseline, clean = model_window(raw)
        a, b = 17., 17.12
        window = rising_cosine((t-a)/.004)*rising_cosine((b-t)/.004)
        # Off-grid and selected-reference perturbations exercise full refitting,
        # then measure change outside the changed samples.
        burst = window*(.035*tone(t, 620)+.015*tone(t, clean['reference_hz']))
        perturbed, changed = model_window(raw+burst)
        self.assertEqual(changed['reference_hz'], clean['reference_hz'])
        delta = perturbed-baseline
        far = ((t >= a-2) & (t < a-.3)) | ((t > b+.3) & (t <= b+2))
        bp = signal.butter(4, [300, 3000], fs=SR, btype='bandpass', output='sos')
        speech_delta = signal.sosfilt(bp, delta)
        self.assertLess(np.std(speech_delta[far]), .00025)
        # Check the first 20–300 ms on both sides. A burst exactly at the
        # reference can still modulate the synthesized estimate.
        for lo, hi in ((a-.3, a-.02), (b+.02, b+.3)):
            near = (t >= lo) & (t < hi)
            self.assertLess(np.std(speech_delta[near]), .001)
        speech_only, _ = model_window(raw + window*.035*tone(t, 620))
        near = ((t >= a-.3) & (t < a-.02)) | ((t > b+.02) & (t <= b+.3))
        self.assertLess(np.std(signal.sosfilt(bp, speech_only-baseline)[near]), .00005)
        # Below-speech-band envelope filters have finite response.
        low_burst = window*.03*tone(t, 120)
        low_changed, _ = model_window(raw+low_burst)
        self.assertLess(np.std((low_changed-baseline)[far]), .00025)

    def test_absolute_anchors_make_crops_sample_exact_and_joins_smooth(self):
        _, noise, hum = synthetic(43)
        x = noise+hum
        settings = Settings(step_seconds=16)

        def read(a, n):
            return x[a:a+n]

        def apply(a, b):
            return np.concatenate([y for _, _, y, _, _ in
                                   corrected_chunks(read, len(x), a, b, settings=settings)])

        a, b = 12*SR+3, 37*SR+17
        entire = apply(0, len(x))
        cropped = apply(a, b)
        np.testing.assert_array_equal(entire[a:b], cropped)
        self.assertEqual(len(x), len(entire))
        correction_steps = abs(np.diff(x-entire))
        for anchor in (16*SR, 32*SR):
            # Nearby physical hum slopes give an independent scale for the join.
            around = np.r_[correction_steps[anchor-400:anchor-4],
                           correction_steps[anchor+4:anchor+400]]
            self.assertLess(correction_steps[anchor-1], 1.05*np.max(around))

    def test_low_residual_correction_survives_unrelated_reference_outage(self):
        from denoise_v2_refinement import suppress_persistent_low_residual
        t = np.arange(80*SR)/SR
        source = .018*tone(t,179.96)+.004*tone(t,539.96)
        n = np.random.default_rng(101).normal(0,.001,len(t))
        data = source+n
        outage = rising_cosine((t-27)*2)*rising_cosine((47-t)*2)
        baseline = source*(1-outage)
        rows = suppress_persistent_low_residual(data,baseline,t,SR,corroborated={9})
        self.assertEqual([r['harmonic'] for r in rows],[3,9])
        interior = (t>=30)&(t<44)
        self.assertLess(np.std((data-baseline)[interior]), .0013)
        a,b = 35.,35.12
        burst = .035*tone(t,179.96)*((t>=a)&(t<b))
        changed = source*(1-outage)
        self.assertEqual([r['harmonic'] for r in suppress_persistent_low_residual(data+burst,changed,t,SR,corroborated={9})],[3,9])
        near = ((t>=a-.3)&(t<a-.02)) | ((t>b+.02)&(t<=b+.3))
        far = ((t>=a-2)&(t<a-.05)) | ((t>b+.05)&(t<=b+2))
        self.assertLess(np.std((changed-baseline)[near]),.0007)
        self.assertLess(np.std((changed-baseline)[far]),.00001)
        already_clean = source.copy()
        self.assertEqual(suppress_persistent_low_residual(data,already_clean,t,SR), [])

    def test_corroborated_low_harmonic_overshoot_is_removed(self):
        from denoise_v2_refinement import suppress_persistent_low_residual
        t = np.arange(80*SR)/SR
        low = .020*tone(t,180)
        harmonic = .004*tone(t,660)
        speech = .006*tone(t,604.5)*rising_cosine(t-30)*rising_cosine(44-t)
        source = low+harmonic+speech
        low_outage = rising_cosine(t-24)*rising_cosine(48-t)
        wrong_phase = (t>=35)&(t<39.5)
        baseline = low*(1-low_outage)+harmonic*(1-2*wrong_phase)
        uncorroborated = baseline.copy()
        self.assertEqual([r['harmonic'] for r in suppress_persistent_low_residual(
            source,uncorroborated,t,SR)],[3])
        records = suppress_persistent_low_residual(source,baseline,t,SR,corroborated={11})
        self.assertEqual([r['harmonic'] for r in records],[3,11])
        self.assertGreaterEqual(records[1]['overshoot_frames'],4)
        self.assertLess(records[1]['material_residual_fraction'],.10)
        region = (t>=36)&(t<38)
        z = np.exp(-2j*np.pi*660*t[region])
        before = abs(np.mean((source-uncorroborated)[region]*z))
        after = abs(np.mean((source-baseline)[region]*z))
        self.assertLess(after,.03*before)
        speech_projection = np.exp(-2j*np.pi*604.5*t[region])
        self.assertAlmostEqual(abs(np.mean((source-baseline)[region]*speech_projection)),.003,places=5)

    def test_compact_bank_carries_across_anchor_and_preserves_crop(self):
        t = np.arange(48*SR)/SR
        x = .009*tone(t,1380.5)
        settings = Settings(step_seconds=16)
        context_start = 0

        def read(frame, count):
            nonlocal context_start
            context_start = frame
            return x[frame:frame+count]

        def fit(samples, seeds, settings, defer_compact):
            self.assertTrue(defer_compact)
            rows = []
            if context_start == 0 and len(samples) == 36*SR:
                rows = [dict(kind='compact_shifted_line',harmonic=23,hz=1380.5,
                             true_absence_intervals_seconds=[])]
            return np.zeros(len(samples)), dict(components=rows)

        def apply(a,b):
            return np.concatenate([y for _,_,y,_,_ in
                                   corrected_chunks(read,len(x),a,b,settings=settings)])

        with patch('denoise_v2.model_window',side_effect=fit):
            full = apply(0,len(x))
            a,b = 13*SR+41,29*SR+75
            clipped = apply(a,b)
        np.testing.assert_allclose(full[a:b],clipped,rtol=0,atol=1e-14)
        for a,b in ((11,15),(17,26)):
            region = slice(a*SR,b*SR)
            self.assertLess(np.std(full[region]),.06*np.std(x[region]))
        anchor = 16*SR
        delta = np.diff(full[anchor-10:anchor+10])
        self.assertLess(np.max(abs(delta)),.001)
        np.testing.assert_array_equal(full[34*SR:35*SR],x[34*SR:35*SR])

    def test_runner_keeps_gain_alignment_and_existing_destination(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            source = base/'source.wav'
            out = base/'new_v2'
            x = np.random.default_rng(17).uniform(-.7, .7, 4*SR)
            sf.write(source, x, SR, subtype='PCM_24')
            before = hashlib.sha256(source.read_bytes()).hexdigest()
            meta = render(source, out, .5, 2, gain=2., offset_frames=7)
            self.assertLess(meta['common_gain'], 2.)
            original, _ = sf.read(source)
            raw, _ = sf.read(out/'01_original.wav')
            corrected, _ = sf.read(out/'02_denoised_v2.wav')
            self.assertEqual(len(raw), 2*SR)
            np.testing.assert_array_equal(raw, corrected)
            expected = original[SR//2+7:SR//2+7+2*SR]*meta['common_gain']
            np.testing.assert_allclose(raw, expected, atol=2**-23, rtol=0)
            pair = (out/'02_denoised_v2.wav').read_bytes()
            with self.assertRaises(FileExistsError):
                render(source, out, .5, 2)
            with self.assertRaises(FileExistsError):
                render(source, base/'denoised'/'new', .5, 2)
            with self.assertRaises(ValueError):
                render(base/'denoised'/'not_original.wav', base/'other_v2', .5, 2)
            self.assertEqual(pair, (out/'02_denoised_v2.wav').read_bytes())
            self.assertEqual(before, hashlib.sha256(source.read_bytes()).hexdigest())


if __name__ == '__main__':
    unittest.main()
