"""Speech selection, sparse/noise safety, peak handling and immutable outputs."""
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
import soundfile as sf

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'scripts'))
import _bootstrap
from speech_normalization import Settings, analyze, normalize, limit_peaks
import pipeline


def voice(rate=8000, seconds=8):
    t = np.arange(round(rate*seconds))/rate
    phase = 2*np.pi*np.cumsum(145+20*np.sin(2*np.pi*.7*t))/rate
    # Several changing formant harmonics, syllabic amplitude and pitch.
    x = sum(np.sin(h*phase)/h**.35 for h in range(2, 16))
    return .012*x*(.55+.45*np.sin(2*np.pi*3.2*t)**2)


class SpeechNormalizationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root/'input'/'source.wav'
        self.source.parent.mkdir()
        self.output = self.root/'output'
        self.rate = 8000

    def save(self, x):
        sf.write(self.source, x, self.rate, subtype='FLOAT')

    def test_silence_noise_tones_and_isolated_spikes_never_get_boost(self):
        rng = np.random.default_rng(13)
        t = np.arange(self.rate*40)/self.rate
        for name, x in [('silence', np.zeros(len(t))), ('noise', rng.normal(0,.002,len(t))),
                        ('tone', .03*np.sin(2*np.pi*1000*t)),
                        ('bursty_tone', .03*np.sin(2*np.pi*1000*t)*(t > 20)),
                        ('noise_burst', rng.normal(0,.05,len(t))*(t > 20))]:
            x = x.copy()
            x[self.rate*25] = .95
            self.save(x)
            with self.subTest(signal=name):
                result = normalize(self.source, self.root/name, progress=lambda _:None)
                self.assertEqual(result['analysis']['gain'], 1.)
                self.assertEqual(result['analysis']['reason'], 'insufficient_speech')
                original, _ = sf.read(self.source, dtype='float32')
                output, _ = sf.read(self.root/name/'normalized.wav', dtype='float32')
                np.testing.assert_array_equal(output, original)

    def test_sparse_speech_and_interior_spike_do_not_set_gain(self):
        x = np.random.default_rng(42).normal(0, .0001, self.rate*90)
        x[self.rate*10:self.rate*18] += voice()
        self.save(x)
        before = analyze(self.source, progress=lambda _:None)
        self.assertGreater(before['speech_seconds'], 3)
        self.assertGreater(before['gain_db'], 0)
        x[self.rate*70] = .99
        self.save(x)
        digest = hashlib.sha256(self.source.read_bytes()).hexdigest()
        result = normalize(self.source, self.output, progress=lambda _:None)
        self.assertAlmostEqual(before['gain_db'], result['analysis']['gain_db'], places=3)
        audio, rate = sf.read(self.output/'normalized.wav', dtype='float32')
        self.assertEqual((len(audio), rate), (len(x), self.rate))
        self.assertLessEqual(np.max(np.abs(audio)), 10**(-1/20)+1e-7)
        self.assertGreater(result['limiter']['affected_seconds'], 0)
        self.assertLess(result['limiter']['affected_fraction'], .001)
        # Speech is a constant gain, without a gate or timeline shift.
        original, _ = sf.read(self.source, dtype='float32')
        np.testing.assert_allclose(audio[self.rate*10:self.rate*18],
            original[self.rate*10:self.rate*18]*result['analysis']['gain'], rtol=2e-7, atol=1e-8)
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), digest)
        with self.assertRaisesRegex(ValueError, 'immutable'):
            normalize(self.source, self.output)

    def test_limiter_is_independent_of_block_boundaries(self):
        x = np.zeros(self.rate*2)
        x[self.rate-2:self.rate+3] = [2., -5., 4., -3., 1.]
        whole, _ = limit_peaks(x, self.rate, .8)
        halo = 2*round(self.rate*.005)
        left, _ = limit_peaks(x[:self.rate+halo], self.rate, .8)
        right, _ = limit_peaks(x[self.rate-halo:], self.rate, .8)
        np.testing.assert_allclose(np.r_[left[:self.rate], right[halo:]], whole, atol=1e-12)
        self.assertLessEqual(np.max(np.abs(whole)), .8+1e-12)

    def test_noise_and_boost_caps_and_timing_bypass(self):
        x = np.random.default_rng(1).normal(0,.001,self.rate*30)
        x[self.rate*5:self.rate*13] += voice()
        self.save(x)
        result = analyze(self.source, Settings(max_boost_db=2), progress=lambda _:None)
        self.assertGreater(result['speech_seconds'], 3)
        self.assertLessEqual(result['gain_db'], 2)
        capped = analyze(self.source, Settings(max_noise_dbfs=-70), progress=lambda _:None)
        self.assertEqual(capped['gain_db'], 0)
        timing = normalize(self.source, self.output, timing_only=True, dry_run=True, progress=lambda _:None)
        self.assertEqual(timing['analysis']['reason'], 'timing_only')
        self.assertFalse(self.output.exists())

    def test_default_gain_reaches_target_without_inheriting_denoise_limits(self):
        x = np.random.default_rng(22).normal(0,.0001,self.rate*30)
        x[self.rate*5:self.rate*13] += voice()*.2
        self.save(x)
        result = normalize(self.source, self.output, progress=lambda _:None)
        analysis = result['analysis']
        self.assertGreater(analysis['gain_db'], 12.)
        self.assertTrue(analysis['target_reached'])
        self.assertAlmostEqual(analysis['expected_speech_dbfs'], -23.)
        self.assertIsNone(analysis['settings']['max_boost_db'])
        self.assertIsNone(analysis['settings']['max_noise_dbfs'])
        original, _ = sf.read(self.source, dtype='float32')
        output, _ = sf.read(self.output/'normalized.wav', dtype='float32')
        # Constant gain includes both transmissions and the intervening noise.
        np.testing.assert_allclose(output, original*analysis['gain'], rtol=2e-7, atol=1e-8)

    def test_loud_transmission_sets_headroom_without_compressing_speech(self):
        x = np.random.default_rng(23).normal(0,.0001,self.rate*40)
        x[self.rate*5:self.rate*13] += voice()*.2
        x[self.rate*15:self.rate*17] += voice(seconds=2)*3
        x[self.rate*30] = .99
        self.save(x)
        result = normalize(self.source, self.output, progress=lambda _:None)
        analysis = result['analysis']
        self.assertTrue(analysis['headroom_limited'])
        self.assertLess(analysis['gain_db'], analysis['desired_gain_db'])
        self.assertEqual(result['limiter']['protected_limited_samples'], 0)
        self.assertLess(result['limiter']['affected_seconds'], .03)
        original, _ = sf.read(self.source, dtype='float32')
        output, _ = sf.read(self.output/'normalized.wav', dtype='float32')
        # Both quiet and loud transmissions remain exact scalar multiples.
        np.testing.assert_allclose(output[:self.rate*20], original[:self.rate*20]*analysis['gain'],
                                   rtol=2e-7, atol=1e-8)
        self.assertLessEqual(np.max(np.abs(output)), 10**(-1/20)+1e-7)

    def test_underestimated_headroom_cannot_publish_broadly_limited_audio(self):
        x = np.random.default_rng(24).normal(0,.0001,self.rate*30)
        x[self.rate*5:self.rate*13] += voice()*.2
        x[self.rate*15:self.rate*17] += voice(seconds=2)*3
        self.save(x)
        faulty = dict(program_peak=1e-6, isolated_impulse_intervals=[], isolated_impulse_samples=0)
        with patch('speech_normalization.program_headroom', return_value=faulty):
            with self.assertRaisesRegex(ValueError, 'limiting would alter protected audio'):
                normalize(self.source, self.output, progress=lambda _:None)
        self.assertFalse((self.output/'normalized.wav').exists())
        saved = json.loads((self.output/'normalization.json').read_text())
        self.assertEqual(saved['status'], 'failed')

    def test_weak_voice_like_activity_in_noise_does_not_qualify_for_gain(self):
        t = np.arange(self.rate*30)/self.rate
        # A background offset can be absent from the detector's
        # speech band. A band-only SNR gate must not authorize boosting it.
        x = .02 + np.random.default_rng(11).normal(0,.0001,len(t))
        x[self.rate*5:self.rate*13] += voice()
        self.save(x)
        result = normalize(self.source, self.output, progress=lambda _:None)
        analysis = result['analysis']
        self.assertGreater(analysis['candidate_seconds'], 3)
        self.assertEqual(analysis['reason'], 'insufficient_speech_contrast')
        self.assertEqual(analysis['gain'], 1.)
        self.assertEqual(analysis['speech_seconds'], 0.)
        self.assertFalse(result['limiter']['applied'])
        original, _ = sf.read(self.source, dtype='float32')
        output, _ = sf.read(self.output/'normalized.wav', dtype='float32')
        np.testing.assert_array_equal(output, original)

    def test_short_transmission_and_invalid_inputs_bypass_or_fail(self):
        x = np.zeros(self.rate*10)
        x[self.rate:self.rate+len(voice(seconds=.2))] = voice(seconds=.2)
        self.save(x)
        self.assertEqual(analyze(self.source, progress=lambda _:None)['gain'], 1.)
        with self.assertRaises(ValueError):
            normalize(self.source, self.source.parent/'child')
        self.save(np.array([0., float('nan')]*8000))
        with self.assertRaisesRegex(ValueError, 'Non-finite'):
            normalize(self.source, self.output)
        self.assertFalse(self.output.exists())
        with self.assertRaises(ValueError):
            Settings(max_boost_db=float('nan')).validate()

    def test_changed_input_between_passes_is_not_published(self):
        self.save(np.zeros(self.rate*3))
        real_analyze = analyze
        def mutate(*args, **kwargs):
            result = real_analyze(*args, **kwargs)
            self.save(np.ones(self.rate*3)*.1)
            return result
        with patch('speech_normalization.analyze', side_effect=mutate):
            with self.assertRaisesRegex(ValueError, 'Source changed'):
                normalize(self.source, self.output, progress=lambda _:None)
        self.assertFalse((self.output/'normalized.wav').exists())
        self.assertEqual(json.loads((self.output/'normalization.json').read_text())['status'], 'failed')

    def test_cli_keeps_tail_samples_and_timing_metadata(self):
        x = np.zeros(self.rate*3+17, dtype=np.float32)
        x[-1] = .8
        self.save(x)
        marker = b'start_seconds,end_seconds\n1.0,2.0\n'
        (self.source.parent/'markers.csv').write_bytes(marker)
        review = dict(status='complete', source=dict(role='timing_only'), source_offset_frames=2400)
        (self.source.parent/'review.json').write_text(json.dumps(review))
        cmd = [sys.executable, str(ROOT/'scripts/07_normalize/07_normalize.py'),
               '--input', str(self.source), '--output', str(self.output)]
        preview = subprocess.run([*cmd, '--dry-run'], capture_output=True, text=True)
        self.assertEqual(preview.returncode, 0, preview.stdout+preview.stderr)
        self.assertFalse(self.output.exists())
        result = subprocess.run(cmd, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
        output, rate = sf.read(self.output/'normalized.wav', dtype='float32')
        np.testing.assert_array_equal(output, x)
        self.assertEqual(rate, self.rate)
        self.assertEqual((self.output/'markers.csv').read_bytes(), marker)
        self.assertEqual(json.loads((self.output/'source_review.json').read_text()), review)
        saved = json.loads((self.output/'normalization.json').read_text())
        self.assertEqual(saved['analysis']['reason'], 'timing_only')

    def test_pipeline_order_alias_and_batch_source(self):
        args, selected = pipeline.parse_args(['plan', '--stages', '07,05b,06', '--tape', 'A15_T001', '--channel', '7'])
        jobs = [j for j in pipeline.make_jobs(args, selected, self.root) if j['status'] == 'pending']
        self.assertEqual([j['number'] for j in jobs], ['05b', '06', '07'])
        self.assertIn(str(args.retimed_root), jobs[-1]['command'])
        self.assertNotIn('--max-boost-db', jobs[-1]['command'])
        self.assertNotIn('--max-noise-dbfs', jobs[-1]['command'])
        capped_args, capped_selected = pipeline.parse_args(['plan', '--stages', '07', '--channel', '7',
            '--speech-max-boost-db', '5', '--speech-max-noise-dbfs', '-50'])
        capped_job = next(j for j in pipeline.make_jobs(capped_args, capped_selected, self.root) if j['number'] == '07')
        self.assertIn('--max-boost-db', capped_job['command'])
        self.assertIn('--max-noise-dbfs', capped_job['command'])
        _, selected = pipeline.parse_args(['plan', '--stages', '05c', '--channel', '7'])
        self.assertEqual(selected, {'06'})
        source = self.root/'defluttered'/'A15_T001'/'CH07'
        source.mkdir(parents=True)
        args, selected = pipeline.parse_args(['plan', '--stages', '07', '--all-tapes',
            '--defluttered-root', str(source.parents[1]), '--normalized-root', str(self.output)])
        jobs = pipeline.make_jobs(args, selected, self.root)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]['channel'], 7)
        self.assertIn(str(source.parents[1]), jobs[0]['command'])
        self.assertFalse(self.output.exists())


if __name__ == '__main__':
    unittest.main()
