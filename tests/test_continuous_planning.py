"""Persistent reference lifetimes, fresh revalidation and actual rendered quality."""
import contextlib
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
import continuous_planning
from continuous_planning import plan_tape
from continuous_tracking import track_array
from flutter_plan_store import load_saved_plan, is_continuous_plan
from channel_review import prepare, continuous_map, joined_map_checks, render_warp, review_page
from flutter_tracking import burst_track


def fixture(root, duration=180, dropout=None, deterioration=False, group=False, target_offset=0):
    source = root/'originals'/'A15_T001'
    source.mkdir(parents=True)
    sr = 8000
    t = np.arange(duration*sr)/sr
    displacement = -.01/(2*np.pi*6.5)*np.cos(2*np.pi*6.5*t)
    tau = t+displacement
    reference_tau = np.where(t >= 70, t+.01/(2*np.pi*6.5)*np.cos(2*np.pi*6.5*t), tau) if deterioration else tau
    ref = .2*np.sin(2*np.pi*777*reference_tau)
    if dropout:
        ref[(t >= dropout[0]) & (t < dropout[1])] = 0.
    end = f'207-00-{duration//60:02d}-{duration%60:02d}'
    sf.write(source/f'A15_T001_HR1U_CH1_IRIG_207-00-00-00_{end}.wav', ref, sr, subtype='FLOAT')
    target_channel = 2
    if deterioration:
        # Initially unsuitable alternative becomes correct on a fresh passage.
        alt_tau = np.where(t < 120, t+.01/(2*np.pi*9)*np.cos(2*np.pi*9*t), tau)
        sf.write(source/f'A15_T001_HR1U_CH2_IRIG_207-00-00-00_{end}.wav', .15*np.sin(2*np.pi*1100*alt_tau), sr, subtype='FLOAT')
        target_channel = 3
    rng = np.random.default_rng(150)
    beep_mask = np.zeros(len(t))
    for second in range(5, duration-2, 4):
        beep_mask[(t >= second) & (t < second+.25)] = 1.
    voice = .1*np.sin(2*np.pi*2500*tau)*beep_mask+rng.normal(0., .0001, len(t))
    begin = f'207-00-00-{target_offset:02d}'
    sf.write(source/f'A15_T001_HR1U_CH{target_channel}_VOICE_{begin}_{end}.wav', voice[target_offset*sr:], sr, subtype='FLOAT')
    if group:
        sf.write(source/f'A15_T001_HR1U_CH3_VOICE_207-00-00-00_{end}.wav',
                 .1*np.sin(2*np.pi*1000*tau)+rng.normal(0., .0001, len(t)), sr, subtype='FLOAT')
    return source, target_channel


class ContinuousPlanningTests(unittest.TestCase):
    def setUp(self):
        self.silent = contextlib.redirect_stdout(io.StringIO())
        self.silent.__enter__()
        self.addCleanup(self.silent.__exit__, None, None, None)

    def map(self, path, channel, start=0):
        p, plan, row, regions, start, offset, frames = prepare(path, channel, start)
        result = continuous_map(p.parent, regions, channel, start, frames, row['sample_rate'])
        checks = joined_map_checks(p.parent, regions, row, channel, start, *result[:3])
        return result, checks, row

    def test_tracker_is_chunk_invariant_through_drift_loss_and_clipping(self):
        sr = 8000
        t = np.arange(32*sr+17)/sr
        tau = t+.00002*t*t-.01/(2*np.pi*6.5)*np.cos(2*np.pi*6.5*t)
        x = .2*np.sin(2*np.pi*777*tau)
        x[(t >= 13.2) & (t < 13.8)] = 0.
        x[(t >= 21.2) & (t < 21.3)] = 1.
        a, ga = track_array(x, sr, 777, 70, .1, chunk_frames=len(x))
        b, gb = track_array(x, sr, 777, 70, .1, chunk_frames=12347)
        np.testing.assert_allclose(a, b, atol=2e-14, rtol=0)
        np.testing.assert_array_equal(ga, gb)
        self.assertTrue(np.all(ga[round(13.2*200):round(13.8*200)] == 0))
        self.assertTrue(np.all(ga[round(21.2*200):round(21.3*200)] == 0))

    def test_one_reference_survives_monitor_and_io_boundaries_and_improves_rendered_tones(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source, ch = fixture(root)
            with patch('continuous_planning.acquire', wraps=continuous_planning.acquire) as acquire:
                plan = plan_tape(source, root/'plan', chunk_seconds=1.543375)
            self.assertEqual(acquire.call_count, 1)
            self.assertTrue(is_continuous_plan(plan))
            self.assertNotIn('carrier_survey', plan['inventory'])
            self.assertEqual(plan['summary']['reference_sessions'], 1)
            target = plan['regions'][0]['targets'][0]
            supported = [s for s in target['reference_spans'] if s['selected']]
            self.assertEqual(len(supported), 1)
            self.assertLessEqual(supported[0]['start_seconds'], 2.1)
            self.assertGreaterEqual(supported[0]['end_seconds'], 178.)
            (tt, nominal, speed, stats, markers, evidence), checks, row = self.map(root/'plan', ch)
            self.assertEqual(checks['status'], 'passed')
            self.assertEqual(stats['reference_switches'], 0)
            for boundary in (60., 120.):
                self.assertGreater(float(np.std(speed[(tt > boundary-.2) & (tt < boundary+.2)])), .004)
            rendered = root/'rendered.wav'
            render_warp(Path(row['path']), rendered, tt, nominal, progress=lambda _:None)
            before, _ = sf.read(row['path'])
            after, _ = sf.read(rendered)
            _, fb = burst_track(before, 8000)
            _, fa = burst_track(after, 8000)
            ratios = []
            for second in (25, 65, 125, 165):
                core = slice(round((second+.06)*1000), round((second+.19)*1000))
                ratios.append(np.std(fa[core])/np.std(fb[core]))
            self.assertLess(max(ratios), .10)

    def test_plan_maps_are_independent_of_io_chunk_size(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source, ch = fixture(root, duration=120)
            a = plan_tape(source, root/'a', chunk_seconds=7.)
            b = plan_tape(source, root/'b', chunk_seconds=.771625)
            self.assertEqual(a['regions'][0]['targets'], b['regions'][0]['targets'])
            ma, _, _ = self.map(root/'a', ch)
            mb, _, _ = self.map(root/'b', ch)
            np.testing.assert_allclose(ma[2], mb[2], atol=2e-14, rtol=0)
            self.assertEqual(ma[4], mb[4])

    def test_sparse_acquisition_backfills_waiting_audio_and_keeps_reserved_roles(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source, ch = fixture(root, duration=300)
            t = np.arange(300*8000)/8000
            tau = t-.01/(2*np.pi*6.5)*np.cos(2*np.pi*6.5*t)
            mask = sum(((t >= second) & (t < second+.25)).astype(float)
                       for second in (10,50,70,110,130,170,190,230,250,290))
            target = next(source.glob('*CH2_VOICE*'))
            sf.write(target, .1*np.sin(2*np.pi*2500*tau)*mask+np.random.default_rng(151).normal(0,.0001,len(t)), 8000, subtype='FLOAT')
            plan = plan_tape(source, root/'plan')
            supported = [s for s in plan['regions'][0]['targets'][0]['reference_spans'] if s['selected']]
            self.assertEqual(len(supported), 1)
            self.assertLess(supported[0]['start_seconds'], 10.)
            self.assertGreater(supported[0]['end_seconds'], 298.)
            monitor = json.loads((root/'plan/monitor_000001.json').read_text())
            self.assertEqual(monitor['search_budget']['acquisition_horizons']['2'], 120.)
            _, checks, _ = self.map(root/'plan', ch)
            self.assertEqual(checks['status'], 'passed')
            self.assertEqual([round(e['original_start_seconds']) for e in checks['reserved_events']], [130,290])

    def test_supported_improvement_persists_above_goal_and_final_map_still_must_improve(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source, ch = fixture(root)
            t = np.arange(180*8000)/8000
            # A distinct source motion remains after genuine tape correction.
            # An absolute restoration goal must not erase a strong verified gain.
            tau = (t-.01/(2*np.pi*6.5)*np.cos(2*np.pi*6.5*t)
                   -.004/(2*np.pi*11)*np.cos(2*np.pi*11*t))
            mask = sum(((t >= second) & (t < second+.25)).astype(float) for second in range(5,178,4))
            target = next(source.glob('*CH2_VOICE*'))
            sf.write(target, .1*np.sin(2*np.pi*2500*tau)*mask+np.random.default_rng(152).normal(0,.0001,len(t)), 8000, subtype='FLOAT')
            plan = plan_tape(source, root/'plan')
            spans = [s for s in plan['regions'][0]['targets'][0]['reference_spans'] if s['selected']]
            self.assertEqual(len(spans), 1)
            self.assertEqual(spans[0]['quality_level'], 'supported_improvement')
            self.assertFalse(spans[0]['restoration_target_met'])
            result, checks, row = self.map(root/'plan', ch)
            self.assertEqual(checks['status'], 'passed')
            self.assertFalse(checks['restoration_target_met'])
            review_page(root, 'Partial correction evidence', result[4], 0.)
            self.assertIn('supported_improvement', (root/'markers.csv').read_text())
            self.assertIn('supported_improvement', (root/'index.html').read_text())
            self.assertTrue(any(m['after_rms_hz'] > 6 for e in checks['reserved_events'] for b in e['metrics'] for m in b['margins']))
            path, _, _, regions, start, _, _ = prepare(root/'plan', ch)
            failed = joined_map_checks(path.parent, regions, row, ch, start, result[0], result[0], np.zeros_like(result[2]))
            self.assertEqual(failed['status'], 'failed')

    def test_short_dropout_ends_lifetime_and_requires_fresh_reacquisition(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source, ch = fixture(root, dropout=(75., 78.))
            plan = plan_tape(source, root/'plan')
            spans = plan['regions'][0]['targets'][0]['reference_spans']
            supported = [s for s in spans if s['selected']]
            self.assertEqual(len(supported), 2)
            self.assertNotEqual(supported[0]['selected'], supported[1]['selected'])
            self.assertLess(supported[0]['end_seconds'], 75.)
            self.assertGreaterEqual(supported[1]['start_seconds'], 120.)
            self.assertFalse(any(s['selected'] and s['start_seconds'] < 110 and s['end_seconds'] > 80 for s in spans))
            (tt, nominal, speed, *_), checks, row = self.map(root/'plan', ch)
            self.assertTrue(np.all(speed[(tt > 80) & (tt < 119)] == 0))
            self.assertTrue(np.all(np.diff(nominal) > 0))
            self.assertEqual(checks['status'], 'passed')

    def test_live_but_wrong_reference_triggers_quality_failure_and_fresh_alternative(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source, ch = fixture(root, deterioration=True)
            plan = plan_tape(source, root/'plan')
            spans = plan['regions'][0]['targets'][0]['reference_spans']
            supported = [s for s in spans if s['selected']]
            self.assertEqual([s['reference_channel'] for s in supported], [1, 2])
            self.assertTrue(any('deterioration' in s['reason'] and not s['selected'] for s in spans))
            self.assertGreaterEqual(supported[1]['start_seconds'], 120.)
            monitor = json.loads((root/'plan/monitor_000001.json').read_text())
            self.assertTrue(any(o['action'] == 'stop' for o in monitor['observations']))
            _, checks, _ = self.map(root/'plan', ch)
            self.assertEqual(checks['status'], 'passed')

    def test_beep_free_target_uses_independent_carrier_group_without_reference_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source, ch = fixture(root, group=True)
            plan = plan_tape(source, root/'plan')
            group = next(t for t in plan['regions'][0]['targets'] if t['channel'] == 3)
            self.assertEqual(group['action'], 'group_render_and_review')
            supported = [s for s in group['reference_spans'] if s['selected']]
            self.assertEqual(len(supported), 1)
            self.assertGreater(supported[0]['end_seconds'], 178.)
            self.assertEqual(supported[0]['evidence_kind'], 'transfer_group')
            self.assertTrue(supported[0]['transfer_group']['passed'])
            _, checks, _ = self.map(root/'plan', 3)
            self.assertEqual(checks['status'], 'passed')

    def test_nonzero_channel_origin_uses_same_tape_clock_and_fractional_last_grid(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source, ch = fixture(root, duration=120, target_offset=5)
            plan_tape(source, root/'plan')
            result, checks, row = self.map(root/'plan', ch, start=5.)
            tt, nominal, speed = result[:3]
            self.assertEqual(tt[-1], (row['frames']-1)/row['sample_rate'])
            self.assertEqual(nominal[-1], tt[-1])
            self.assertEqual(checks['status'], 'passed')

    def test_group_membership_loss_is_explicit_and_does_not_restart_healthy_donor(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source, ch = fixture(root, group=True)
            path = next(source.glob('*CH3_*'))
            sr = 8000
            t = np.arange(180*sr)/sr
            displacement = -.01/(2*np.pi*6.5)*np.cos(2*np.pi*6.5*t)
            tau = t+np.where(t < 70, displacement, -displacement)
            sf.write(path, .1*np.sin(2*np.pi*1000*tau)+np.random.default_rng(3).normal(0,.0001,len(t)), sr, subtype='FLOAT')
            plan = plan_tape(source, root/'plan')
            donor, target = plan['regions'][0]['targets']
            donor_spans = [s for s in donor['reference_spans'] if s['selected']]
            self.assertEqual(len(donor_spans), 1)
            supported = [s for s in target['reference_spans'] if s['selected']]
            self.assertEqual(len(supported), 1)
            self.assertEqual(supported[0]['end_seconds'], 60.)
            self.assertTrue(any(not s['selected'] and 'group membership lost' in s['reason'].lower()
                                for s in target['reference_spans']))

    def test_final_holdout_failure_remains_failed_without_refitting(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source, ch = fixture(root, duration=120)
            path = next(source.glob('*CH2_*'))
            sr = 8000
            t = np.arange(120*sr)/sr
            displacement = -.01/(2*np.pi*6.5)*np.cos(2*np.pi*6.5*t)
            tau = t+displacement
            mask, reserved = np.zeros(len(t)), np.zeros(len(t), dtype=bool)
            # Event roles restart at each monitoring passage. Build the exact
            # chronological fifth-event reservations without fitting the map.
            for low in (0, 60):
                seconds = [s for s in range(5,118,4) if low <= s < low+60]
                for index, second in enumerate(seconds):
                    keep = (t >= second) & (t < second+.25)
                    mask[keep] = 1.
                    if (index+1) % 5 == 0:
                        reserved |= keep
            warped = np.where(reserved, t-displacement, tau)
            sf.write(path, .1*np.sin(2*np.pi*2500*warped)*mask+
                     np.random.default_rng(4).normal(0,.0001,len(t)), sr, subtype='FLOAT')
            plan = plan_tape(source, root/'plan')
            self.assertEqual(plan['summary']['reference_sessions'], 1)
            _, checks, _ = self.map(root/'plan', ch)
            self.assertEqual(checks['status'], 'failed')
            self.assertFalse(checks['failed_choice_refitted'])


if __name__ == '__main__':
    unittest.main()
