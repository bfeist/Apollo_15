import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import numpy as np
import soundfile as sf

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'scripts'))
from audio_processing import denoise
from review_noise_reduction import annotated_candidates


class NoiseReviewTests(unittest.TestCase):
    def test_final_window_does_not_duplicate_asr_speech(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tape = root/'A15_T999999'
            tape.mkdir()
            annotations = root/'asr'
            annotations.mkdir()
            stem = 'A15_T999999_HR1U_CH15_CAPCOM_RT_207-01-00-00_207-01-30-00-0'
            sf.write(tape/(stem+'.wav'), np.zeros(1800*1000), 1000)
            (annotations/(stem+'.json')).write_text(json.dumps({'0':dict(start_time=1780,
                end_time=1800, words='This is an example conversation with ten or more words.')}))
            candidates = annotated_candidates(tape, annotations, 30)
            self.assertEqual(len(candidates), 1)
            self.assertEqual(candidates[0]['offset_seconds'], 1770)
            self.assertEqual(candidates[0]['annotated_speech_seconds'], 20)
            # The supplied archive also contains TRS conversions without JSON.
            trs = root/'trs'
            trs.mkdir()
            (trs/(stem+'.trs')).write_text('''<?xml version="1.0"?><Trans><Episode>
                <Turn startTime="1780" endTime="1800" speaker="sp1"><Sync time="1780"/>
                This is an example conversation with ten or more words.</Turn>
                </Episode></Trans>''')
            converted = annotated_candidates(tape, trs, 30)
            self.assertEqual(converted[0]['offset_seconds'], candidates[0]['offset_seconds'])
            self.assertEqual(converted[0]['annotated_words'], candidates[0]['annotated_words'])

    def test_broadband_zero_and_reduction_order(self):
        sr = 8000
        rng = np.random.default_rng(23)
        t = np.arange(30*sr)/sr
        x = rng.normal(0, .02, len(t))
        x += .04*np.sin(2*np.pi*710*t)*(t > 12)*(t < 19)
        bypass, _ = denoise(x, sr, 0)
        mild, p3 = denoise(x, sr, 3)
        stronger, p6 = denoise(x, sr, 6)
        current, _ = denoise(x, sr)
        np.testing.assert_array_equal(current, mild)
        np.testing.assert_allclose(bypass, x, atol=1e-15)
        self.assertEqual(p3, p6)
        self.assertGreater(np.linalg.norm(mild), np.linalg.norm(stronger))
        self.assertLess(np.linalg.norm(mild), np.linalg.norm(x))
        for cap in [-1, 7, float('nan')]:
            with self.assertRaises(ValueError):
                denoise(x, sr, cap)

    def test_export_and_residuals_reconstruct_original(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder = root/'delivery/A15_T999999/A15-CH15_CAPCOM_RT'
            folder.mkdir(parents=True)
            path = folder/'A15_T999999_HR1U_CH15_CAPCOM_RT_test.wav'
            sr = 8000
            t = np.arange(30*sr)/sr
            rng = np.random.default_rng(2)
            x = .04*np.sin(2*np.pi*60*t)+rng.normal(0, .003, len(t))
            x += .03*np.sin(2*np.pi*743*t)*(t > 12)*(t < 20)
            sf.write(path, x, sr, subtype='FLOAT')
            before = path.read_bytes()
            out = root/'review'
            result = subprocess.run([sys.executable, str(ROOT/'scripts/review_noise_reduction.py'),
                '--delivery', str(root/'delivery'), '--annotations', str(root/'no_annotations'),
                '--output', str(out), '--extra-quiet', '0'], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
            manifest = json.loads((out/'results.json').read_text())
            self.assertEqual(manifest['verification']['wavs'], 17)
            self.assertTrue(manifest['verification']['current_pipeline_exact'])
            self.assertEqual(manifest['current_variant'], 'full_b3')
            sample = manifest['samples'][0]
            self.assertEqual(sample['stage03_method'], 'iterative_tones_plus_residual_hum_v1')
            self.assertIn('stage03_hum_passes', sample)
            self.assertIn('stage03_residual_hum_lines', sample)
            original, _ = sf.read(out/sample['files']['off_b0']['audio'])
            for entry in sample['files'].values():
                if entry['removed']:
                    clean, _ = sf.read(out/entry['audio'])
                    residual, _ = sf.read(out/entry['removed'])
                    np.testing.assert_allclose(clean+residual, original, atol=8e-8)
            half, _ = sf.read(out/sample['files']['half_b0']['audio'])
            full, _ = sf.read(out/sample['files']['full_b0']['audio'])
            np.testing.assert_allclose(half, (original+full)/2, atol=8e-8)
            self.assertEqual(before, path.read_bytes())
            self.assertIn('Export review JSON', (out/'index.html').read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
