"""Carrier survey cache integrity, tracking support masks and clock separation."""
import contextlib
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
import soundfile as sf

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from _bootstrap import install as _install
_install()
from carrier_survey import scan_file, survey_tape, load_survey, screen
from flutter_planning import inventory
from flutter_tracking import trajectory
from irig_clock import sequential_runs, promote_run, carry_anchors, clock_constraints


class CarrierPipelineTests(unittest.TestCase):
    def test_short_island_checkpoint_and_tamper(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            sr = 8000
            t = np.arange(12*sr)/sr
            x = np.random.default_rng(1).normal(0, .0001, len(t))
            x += .1*np.sin(2*np.pi*961*t)*((t >= 6.37) & (t < 8.57))
            original = root/'original.wav'
            sf.write(original, x, sr, subtype='FLOAT')
            before = original.read_bytes()
            cache = root/'cache'/'source.json'
            partial = scan_file(original, cache, 3)
            self.assertFalse(partial['complete'])
            complete = scan_file(original, cache)
            self.assertTrue(complete['complete'])
            self.assertEqual(complete['window_count'], 12)
            self.assertTrue(any(r['start_sample'] <= 6.37*sr < r['end_sample'] for r in complete['islands']))
            self.assertTrue(all(r['clock_status'] == 'not_scanned' for r in complete['islands']))
            stamp = cache.stat().st_mtime_ns
            self.assertEqual(scan_file(original, cache), complete)
            self.assertEqual(cache.stat().st_mtime_ns, stamp)
            self.assertEqual(original.read_bytes(), before)
            original.touch()
            with self.assertRaisesRegex(ValueError, 'changed'):
                scan_file(original, cache)

    def test_timing_and_unlabelled_survey_not_speech_targets(self):
        with tempfile.TemporaryDirectory() as folder, contextlib.redirect_stdout(io.StringIO()):
            root = Path(folder)
            source = root/'A15_T510078'
            source.mkdir()
            t = np.arange(20*8000)/8000
            tau = t-.01/(2*np.pi*6.5)*np.cos(2*np.pi*6.5*t)
            ref = .1*np.sin(2*np.pi*961*tau)
            sf.write(source/'A15_T510078_HR1U_CH1_IRIG_207-00-00-00_207-00-00-20.wav', ref, 8000)
            sf.write(source/'A15_T510078_HR1U_CH2_VOICE_207-00-00-00_207-00-00-20.wav', np.zeros(len(t)), 8000)
            sf.write(source/'A15_T510078_HR1U__207-00-00-00_207-00-00-20.wav', ref, 8000)
            output = root/'survey'
            index = survey_tape(source, output)
            self.assertEqual(len(index['files']), 3)
            with self.assertWarns(UserWarning):
                inv = inventory(source)
            self.assertEqual(len(inv['excluded_files']), 1)
            cache = json.loads((output/index['files'][0]['cache']).read_text())
            feature = output/cache['feature_blocks'][0]['file']
            feature.write_bytes(feature.read_bytes()+b' ')
            with self.assertRaisesRegex(ValueError, 'feature cache changed'):
                load_survey(output)
            with self.assertRaisesRegex(ValueError, 'feature cache changed'):
                survey_tape(source, output)

    def test_local_carrier_mask_never_bridges_gap_or_rejects_am_bits(self):
        sr = 8000
        t = np.arange(20*sr)/sr
        tau = t-.01/(2*np.pi*6.5)*np.cos(2*np.pi*6.5*t)
        am = np.where((t % .01) < .005, 1., .3)
        x = .2*am*np.sin(2*np.pi*1000*tau)
        mask = (np.arange(4000)/200 > 4) & (np.arange(4000)/200 < 16)
        mask[1800:2200] = False
        trace = trajectory(x, sr, 1000, 90, mask)
        self.assertEqual(float(trace[5][1800:2200].max()), 0.)
        self.assertGreater(float(trace[5][1200:1600].mean()), .9)


    def test_sequential_clock_authentication_and_actual_map_coordinates(self):
        frames = [dict(day_of_year=207, hours=13, minutes=34, seconds=i, original_sample=8000*i,
                       valid_fields=True) for i in range(4)]
        with self.assertRaisesRegex(ValueError, 'authenticated'):
            promote_run(frames, 8000, {})
        anchors = promote_run(frames, 8000, dict(recorded_clock_basis='GMT', reviewer='fixture', evidence='fixture', status='accepted'))
        self.assertEqual(anchors[0]['get_seconds'], 0.)
        repeated = frames[:2]+[frames[1]]+frames[2:]
        runs, breaks = sequential_runs(repeated, 8000)
        self.assertEqual(breaks[0]['reason'], 'repeated_frame')
        with self.assertRaisesRegex(ValueError, 'sequential'):
            promote_run(repeated, 8000, dict(recorded_clock_basis='GMT', reviewer='fixture', evidence='fixture', status='accepted'))
        tt = np.linspace(0, 4, 801)
        nominal = tt+.01*np.sin(np.pi*tt/4)
        mapped = carry_anchors(anchors, 8000, 10., 10., tt, nominal)
        self.assertAlmostEqual(mapped[1]['defluttered_seconds'], 1.+.01*np.sin(np.pi/4))
        model = dict(input_seconds=[0., 4.], recorded_speed_ratio=[1., 1.])
        for r in mapped:
            r['run_id'] = 'fixture'
        self.assertEqual(clock_constraints(mapped, model)['status'], 'consistent')
        mapped[2]['get_seconds'] += .5
        self.assertEqual(clock_constraints(mapped, model)['status'], 'conflict')


if __name__ == '__main__':
    unittest.main()
