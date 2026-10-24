"""Known curved-speed recovery, streamed continuity and stage-05c integration."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import numpy as np
import soundfile as sf
from scipy.interpolate import PchipInterpolator
from scipy import signal

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'scripts'))
from quindar_retime import curve_model, timing_map, render_stream, retime
import pipeline


def event(t, hz, kind):
    return dict(start_seconds=t-.125, end_seconds=t+.125, frequency_hz=hz,
                duration_seconds=.25, band_power_fraction=.95, amplitude_ratio=1.05,
                trim_range_hz=.1, residual_fm_sd_hz=1.)


class QuindarRetimeTests(unittest.TestCase):
    def test_recovers_nonlinear_trend_without_chasing_beep_outliers(self):
        rng = np.random.default_rng(10)
        t = np.arange(100., 21500., 45.)
        truth = .987-.00008*t/3600+.002*np.sin(2*np.pi*t/9000)
        n = np.where(np.arange(len(t)) % 2, 2475., 2525.)
        noise = rng.normal(0, .8, len(t))
        noise[::17] += 12
        rows = [event(a, b, k) for a, b, k in zip(t, truth*n+noise, n)]
        model = curve_model(rows, 21600.)
        self.assertEqual(len(model['anchors']), len(rows))
        estimate = np.array([x['smoothed_speed_ratio'] for x in model['anchors']])
        interior = (t > 2000)&(t < 19600)
        a, b = model['linear_coefficients']
        curved_error = np.sqrt(np.mean((estimate[interior]-truth[interior])**2))
        linear_error = np.sqrt(np.mean((a+b*t[interior]/3600-truth[interior])**2))
        self.assertLess(curved_error, linear_error*.65)
        self.assertLess(curved_error, .0006)
        self.assertEqual(model['recorded_speed_ratio'][0], model['recorded_speed_ratio'][1])
        self.assertEqual(model['recorded_speed_ratio'][-1], model['recorded_speed_ratio'][-2])

    def test_streamed_curve_frequency_duration_and_block_boundaries(self):
        with tempfile.TemporaryDirectory() as folder:
            p = Path(folder)
            sr = 8000
            model = dict(input_seconds=[0., 3., 7., 12.], recorded_speed_ratio=[.984, .988, .982, .985])
            curve = PchipInterpolator(model['input_seconds'], model['recorded_speed_ratio'])
            integral = curve.antiderivative()
            t = np.arange(12*sr)/sr
            sf.write(p/'input.wav', .4*np.sin(2*np.pi*2525*(integral(t)-integral(0))), sr, subtype='FLOAT')
            tt, nominal, speed = timing_map(model, step=.01)
            render_stream(p/'input.wav', p/'small.wav', tt, nominal, speed, block_seconds=.37, progress=lambda _:None)
            render_stream(p/'input.wav', p/'large.wav', tt, nominal, speed, block_seconds=4.13, progress=lambda _:None)
            small, _ = sf.read(p/'small.wav')
            large, _ = sf.read(p/'large.wav')
            np.testing.assert_allclose(small, large, atol=1e-7)
            self.assertEqual(len(small), round(nominal[-1]*sr))
            phase = np.unwrap(np.angle(signal.hilbert(small)))[800:-800]
            frequency = np.gradient(phase)*sr/(2*np.pi)
            self.assertLess(abs(np.mean(frequency)-2525), .02)
            self.assertLess(np.std(frequency), .2)
            with self.assertRaises(ValueError):
                timing_map(dict(input_seconds=[0, 2, 1], recorded_speed_ratio=[1,1,1]))

    def test_pipeline_render_marker_conversion_reuse_and_tamper_detection(self):
        with tempfile.TemporaryDirectory() as folder:
            p = Path(folder)
            source = p/'input'
            source.mkdir()
            sr, duration = 8000, 1201.
            x = np.zeros(round(duration*sr), dtype=np.float32)
            events = []
            for j, center in enumerate(np.arange(15., 1190., 15.)):
                nominal = 2525 if j % 2 == 0 else 2475
                frequency = nominal*.987
                first, count = round((center-.125)*sr), round(.25*sr)
                x[first:first+count] = .2*np.sin(2*np.pi*frequency*np.arange(count)/sr)
                events.append(event(center, frequency, nominal))
            path = source/'deflutter.wav'
            sf.write(path, x, sr, subtype='FLOAT')
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            (source/'markers.csv').write_text('start_seconds,end_seconds,kind\n600,601,check\n')
            stat = path.stat()
            inventory = dict(source=str(path.resolve()), bytes=stat.st_size, mtime_ns=stat.st_mtime_ns,
                             frames=len(x), sample_rate=sr, scan_start=0., scan_end=duration, events=events)
            cache = p/'candidates.json'
            cache.write_text(json.dumps(inventory))
            command = [sys.executable, str(ROOT/'scripts/pipeline.py'), 'run', '--plain', '--stages', '05c',
                       '--experimental-single-channel',
                       '--input', str(path), '--output', str(p/'runs'), '--retime-candidates', str(cache)]
            result = subprocess.run(command, capture_output=True, text=True, encoding='utf-8')
            self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
            manifest_path = next((p/'runs').glob('*/05c_retime/retime.json'))
            output = manifest_path.parent
            record = json.loads(manifest_path.read_text())
            self.assertEqual(record['status'], 'complete')
            self.assertEqual(record['audio']['frames'], round(duration*.987*sr))
            self.assertLessEqual(record['audio']['peak'], 10**(-3/20)+1e-7)
            import csv
            with (output/'markers.csv').open() as f:
                marker = next(csv.DictReader(f))
            self.assertAlmostEqual(float(marker['start_seconds']), 600*.987, places=6)
            self.assertEqual(float(marker['input_start_seconds']), 600.)
            stamp = (output/'retimed.wav').stat().st_mtime_ns
            retime(path, output, cache, progress=lambda _:None)
            self.assertEqual((output/'retimed.wav').stat().st_mtime_ns, stamp)
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), digest)
            with (output/'timing_map.npz').open('ab') as f:
                f.write(b'changed')
            with self.assertRaisesRegex(ValueError, 'artifact changed'):
                retime(path, output, cache, progress=lambda _:None)
            inventory['mtime_ns'] -= 1
            cache.write_text(json.dumps(inventory))
            with self.assertRaisesRegex(ValueError, 'exact complete'):
                retime(path, p/'bad', cache)
            self.assertFalse((p/'bad').exists())

    def test_numbering_paths_and_skip_guard(self):
        args, selected = pipeline.parse_args(['plan', '--tape', 'A15_T510083', '--channel', '20'])
        self.assertEqual(selected, {'03', '05a', '05b', '05c'})
        jobs = pipeline.make_jobs(args, selected, Path('fake_run'))
        job = next(j for j in jobs if j['number'] == '05c')
        self.assertIn('05c_retime.py', job['script'])
        self.assertIn('defluttered_retimed_v2', ' '.join(job['command']))
        with self.assertRaises(SystemExit):
            pipeline.parse_args(['plan', '--stages', '05b,05c', '--skip-deflutter'])


if __name__ == '__main__':
    unittest.main()
