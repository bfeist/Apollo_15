"""Stage 06c SG retiming (docs/20_2026-11-25_sg_retime_30track.md): curve, vote, stagger, audio, inputs."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
import unittest.mock

import numpy as np
import soundfile as sf
from scipy.interpolate import BSpline

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from _bootstrap import install as _install  # noqa: E402
_install()
import sg_audio  # noqa: E402
import sg_curve  # noqa: E402
import sg_inputs  # noqa: E402
import sg_match  # noqa: E402


def true_phi(t, cut=None, jump=0.0):
    t = np.asarray(t, float)
    out = 800000.0 + 0.975 * t + 6.0 * np.sin(2 * np.pi * t / 30000.0)
    if cut is not None:
        out = out + np.where(t >= cut, jump, 0.0)
    return out


class CurveTests(unittest.TestCase):
    def test_basis_derivative_matches_bspline(self):
        basis = sg_curve.Basis(3600.0, 120.0)
        coef = np.random.default_rng(1).normal(size=basis.n)
        x = np.linspace(0, 3600, 97)
        spl = BSpline(basis.knots, coef, 3)
        np.testing.assert_allclose(basis.value(x) @ coef, spl(x), atol=1e-9)
        np.testing.assert_allclose(basis.deriv(x) @ coef, spl.derivative()(x), atol=1e-9)

    def evidence(self, rng, n=3000, cut=30000.0, jump=-45.0):
        t = np.sort(rng.uniform(500, 59000, n))
        ia = (t > 20000) & (t < 26000)
        g = true_phi(t, cut, jump) + rng.normal(0, 0.05, n) + np.where(ia, 2.0, 0.0)  # the IA map places audio 2 s late
        g[rng.choice(n, 30, replace=False)] += 400.0  # gross outliers
        return sg_curve.TapeEvidence(tape='T', duration=60000.0, t=t, g=g, sigma=np.full(n, 0.3),
                                     bkey=np.where(ia, 'ia:R:0', None), ref_kind=np.where(ia, 'ia', 'nara'),
                                     kind=['text'] * n, cuts=[cut])

    def test_joint_solve_recovers_curve_segment_jump_and_ia_offset(self):
        ev = self.evidence(np.random.default_rng(2))
        sol = sg_curve.solve([ev], {'T': 100.0}, {'ia:R:0': 1.0})
        curve = sol.curves['T']
        grid = np.linspace(1000, 58000, 400)
        self.assertLess(np.max(np.abs(curve.get(grid) - true_phi(grid, 30000.0, -45.0))), 0.1)
        self.assertAlmostEqual(sol.b['ia:R:0'], 2.0, delta=0.1)
        self.assertGreater((sol.weights['T'] == 0).sum(), 25)  # outliers rejected

    def test_rate_witnesses_constrain_a_gap(self):
        rng = np.random.default_rng(3)
        t = np.r_[np.linspace(100, 9000, 300), np.linspace(41000, 59000, 300)]
        g = true_phi(t)
        rt = np.linspace(9500, 40500, 200)
        rate = 0.975 + 6.0 * 2 * np.pi / 30000.0 * np.cos(2 * np.pi * rt / 30000.0)
        base = dict(tape='T', duration=60000.0, t=t, g=g, sigma=np.full(len(t), 0.05), bkey=[None] * len(t),
                    ref_kind=['nara'] * len(t), kind=['text'] * len(t))
        without = sg_curve.solve([sg_curve.TapeEvidence(**base)], {'T': 1e4}, {}).curves['T']
        with_rate = sg_curve.solve([sg_curve.TapeEvidence(**base, rate_t=rt, rate_r=rate + rng.normal(0, 1e-4, len(rt)),
                                                          rate_sigma=np.full(len(rt), 6e-4))], {'T': 1e4}, {}).curves['T']
        mid = np.linspace(12000, 38000, 50)
        self.assertLess(np.max(np.abs(with_rate.get(mid) - true_phi(mid))),
                        np.max(np.abs(without.get(mid) - true_phi(mid))))

    def test_output_map_segments_reproduce_get_and_hold_the_rate_outside(self):
        ev = self.evidence(np.random.default_rng(4))
        curve = sg_curve.solve([ev], {'T': 100.0}, {'ia:R:0': 1.0}).curves['T']
        grid, out, speed = curve.output_map(60000.0, step=1.0)
        segments = curve.segments(60000.0)
        self.assertEqual(len(segments), 2)
        for t in (5000.0, 29000.0, 31000.0, 55000.0):
            seg = segments[int(t >= 30000.0)]
            self.assertAlmostEqual(np.interp(t, grid, out) + seg['get_equals_output_plus_s'], float(curve.get(np.array([t]))[0]),
                                   delta=0.01)
        self.assertAlmostEqual(speed[0], float(curve.rate(np.array([curve.first_s]))[0]), places=9)
        self.assertAlmostEqual(speed[-1], float(curve.rate(np.array([curve.last_s]))[0]), places=9)

    def test_ia_only_tape_takes_the_given_penalty(self):
        rng = np.random.default_rng(5)
        t = np.sort(rng.uniform(0, 20000, 400))
        ev = sg_curve.TapeEvidence(tape='T', duration=20000.0, t=t, g=true_phi(t), sigma=np.full(400, 0.3),
                                   bkey=['ia:R:0'] * 400, ref_kind=['ia'] * 400, kind=['text'] * 400)
        lam, _, validation, _ = sg_curve.choose_lambda(ev, {'ia:R:0': 1.0}, fixed=1234.0)
        self.assertEqual(lam, 1234.0)
        self.assertEqual(validation['lambda_basis'], 'NARA tapes choice')


class VoteTests(unittest.TestCase):
    def test_rate_change_kept_isolated_mode_dropped_and_stop_starts_segment(self):
        rng = np.random.default_rng(6)
        t = np.sort(rng.uniform(0, 36000, 4000))
        d = -780.0 - 0.01 * np.maximum(t - 12000, 0) + rng.normal(0, 0.3, len(t))  # 1 % rate change after 12000 s
        d[t > 24000] += 300.0                                                     # recorder stop: GET jumps forward
        t = np.r_[t, np.full(12, 15100.0) + np.arange(12) * 40.0]
        d = np.r_[d, np.full(12, 1500.0)]
        pairs = dict(t=t, d=d)
        modes, keep, cuts = sg_match.vote(pairs, 36000.0)
        self.assertEqual(len(cuts), 1)
        self.assertAlmostEqual(cuts[0], 24000.0, delta=600.0)
        self.assertFalse(keep[-12:].any())
        self.assertGreater(keep[:-12].mean(), 0.97)


    def test_mode_between_neighbours_that_continue_each_other_is_rejected(self):
        rng = np.random.default_rng(10)
        t = np.sort(rng.uniform(0, 12000, 3000))
        d = -330.0 + rng.normal(0, 0.3, len(t))
        replay = (t > 6000) & (t < 7200) & (rng.uniform(size=len(t)) < 0.6)
        d[replay] = -263.0 + rng.normal(0, 0.3, replay.sum())  # a strong second loop in one block
        modes, keep, cuts = sg_match.vote(dict(t=t, d=d), 12000.0)
        self.assertEqual(cuts, [])
        self.assertFalse(keep[replay].any())
        self.assertTrue(any(m.get('sandwiched') or m.get('switched') for m in modes))


    def test_block_outvoted_by_a_second_loop_takes_its_runner_up(self):
        rng = np.random.default_rng(12)
        t = np.sort(rng.uniform(0, 12000, 4000))
        d = -330.0 + rng.normal(0, 0.3, len(t))
        block = (t >= 6000) & (t < 7200)
        loop = block & (rng.uniform(size=len(t)) < 0.55)
        d[loop] = -265.0 - 0.014 * (t[loop] - 6600) + rng.normal(0, 0.3, loop.sum())
        modes, keep, cuts = sg_match.vote(dict(t=t, d=d), 12000.0)
        self.assertEqual(cuts, [])
        m = next(m for m in modes if m['block'] == 5)
        self.assertTrue(m['switched'])
        self.assertAlmostEqual(m['offset'], -330.0, delta=1.0)
        self.assertFalse(keep[loop].any())
        self.assertGreater(keep[block & ~loop].mean(), 0.95)

    def test_backward_step_is_not_a_stop(self):
        rng = np.random.default_rng(11)
        t = np.sort(rng.uniform(0, 24000, 3000))
        d = -700.0 + rng.normal(0, 0.3, len(t))
        late = t > 12000
        d[late & (t < 13200)] = -890.0  # one strong block 190 s 'earlier': the same GET later on tape
        modes, keep, cuts = sg_match.vote(dict(t=t, d=d), 24000.0)
        self.assertEqual(cuts, [])
        self.assertFalse(keep[late & (t < 13200)].any())
        self.assertGreater(keep[~(late & (t < 13200))].mean(), 0.99)


class HeldoutTests(unittest.TestCase):
    def test_segment_without_training_events_is_not_predicted(self):
        t = np.r_[np.linspace(100, 9000, 300), np.linspace(9100, 9700, 10)]   # the second segment sits in one block
        g = true_phi(t) + np.where(t > 9050, 300.0, 0.0)
        ev = sg_curve.TapeEvidence(tape='T', duration=10000.0, t=t, g=g, sigma=np.full(len(t), 0.1), bkey=[None] * len(t),
                                   ref_kind=['nara'] * len(t), kind=['text'] * len(t), cuts=[9050.0])
        res, _ = sg_curve.heldout(ev, 100.0, {})
        self.assertTrue(np.isnan(res[t > 9050]).all())
        self.assertLess(np.nanmax(np.abs(res)), 5.0)


class StaggerTests(unittest.TestCase):
    def pairs(self, rng, curve_error=0.0):
        rows = dict(channel=[], t=[], g=[], ref=[], seg=[], p=[], aligned=[])
        for r, tr in enumerate(np.sort(rng.uniform(0, 7200, 600))):
            g = true_phi(tr) + curve_error * np.sin(tr / 300.0)
            for ch, st in ((8, 0.0), (15, 1.37), (19, 0.0 if tr < 3600 else 1.37)):
                rows['channel'].append(ch)
                rows['t'].append(tr + st + rng.normal(0, 0.02))
                rows['g'].append(g)
                rows['ref'].append(r)
                rows['seg'].append(0)
                rows['p'].append(0.9)
                rows['aligned'].append(True)
        return {k: np.asarray(v) for k, v in rows.items()}

    def test_curve_free_stagger_chains_from_the_reference_channel(self):
        p = self.pairs(np.random.default_rng(7), curve_error=3.0)  # curve error must not leak into stagger
        keep = np.ones(len(p['t']), bool)
        level, rows = sg_match.relative_stagger(p, keep, 8)
        self.assertAlmostEqual(level[15]['stagger_s'], 1.37, delta=0.01)
        self.assertTrue(level[19]['mixed'])
        by = {(r['channel'], r['interval']): r['stagger_s'] for r in rows}
        self.assertAlmostEqual(by[19, 0], 0.0, delta=0.01)
        self.assertAlmostEqual(by[19, 3], 1.37, delta=0.01)  # 1800-s intervals; the copy changes at 3600 s
        groups = sg_match.cluster_groups(rows)
        self.assertEqual(len(groups), 2)
        st, basis = sg_match.assign_stagger(p, keep, rows, level, groups)
        events = sg_match.tape_events(p, keep, st, [dict(kind='nara', key='nara:X:0', source='X')])
        self.assertEqual(len(events), 600)
        self.assertTrue(all(e['copies'] == 3 and e['spread_s'] < 0.15 for e in events))
        self.assertTrue(all(e['sigma_s'] == 0.15 and e['aligned'] for e in events))


class CaptureOffsetFlagTests(unittest.TestCase):
    def test_channel_on_the_wrong_clock_is_flagged(self):
        import sg_stage
        tr = np.linspace(100, 7000, 200)
        g = true_phi(tr)
        n = len(tr)
        pairs = dict(channel=np.r_[np.full(n, 1), np.full(n, 3), np.full(n, 2)], t=np.r_[tr, tr + 1.37, tr + 11.867],
                     g=np.r_[g, g, g], ref=np.r_[np.arange(n), np.arange(n), np.arange(n)])
        level = {1: dict(stagger_s=0.0, mixed=False), 3: dict(stagger_s=1.37, mixed=False)}
        flags = sg_stage.capture_offset_flags(pairs, {1: 0.0, 2: 11.867, 3: 11.867}, [0.0, 1.37], level)
        self.assertEqual([f['channel'] for f in flags], [2])
        self.assertAlmostEqual(flags[0]['median_residual_s'], 11.867, delta=0.05)


class TextPairTests(unittest.TestCase):
    def test_distinctive_grams_only(self):
        words = 'roger the shadow is four degrees left and we copy that'.split()
        ref_tok = words + ['common', 'phrase', 'here', 'now'] * 5
        ref = dict(tok=np.array(ref_tok), get=1000.0 + np.arange(len(ref_tok)) * 0.4, s=np.arange(len(ref_tok)) * 0.4,
                   src=np.zeros(len(ref_tok), int), seg=np.zeros(len(ref_tok), int), p=np.full(len(ref_tok), 0.9),
                   aligned=np.ones(len(ref_tok), bool))
        chan = words + ['common', 'phrase', 'here', 'now']
        w = sg_inputs.Words(np.array(chan), 10.0 + np.arange(len(chan)) * 0.4, np.full(len(chan), 0.9),
                            np.ones(len(chan), bool))
        prior = dict(get_at_zero_s=1000.0 - 10.0 - 60.0, rate=1.0)
        pairs, stats = sg_match.text_pairs([(3, 5.0, w)], ref, prior, 100.0)
        # Grams inside the utterance and across into the phrase; the phrase itself is in the
        # reference five times and is not distinctive.
        self.assertEqual(len(pairs['t']), len(words) - 3 + 3)
        np.testing.assert_allclose(pairs['g'] - pairs['t'], 1000.0 - 15.0)


def write_wav(path, x, sr=8000):
    sf.write(path, x.astype('float32'), sr, subtype='FLOAT')


class AudioTests(unittest.TestCase):
    def test_gcc_phat_sign(self):
        rng = np.random.default_rng(8)
        sr, lag = 8000, 5.0
        y = rng.normal(size=int(50 * sr))
        delta = 0.137
        x = y[int((lag + delta) * sr):int((lag + delta) * sr) + 40 * sr]  # x(G) = y(G + delta)
        self.assertAlmostEqual(sg_audio.gcc_phat(x, y, sr, lag)['lag_s'], delta, delta=1e-3)

    def test_refine_window_recovers_curve_error_in_the_ia_frame(self):
        rng = np.random.default_rng(9)
        sr = 8000
        with tempfile.TemporaryDirectory() as tmp:
            speech = rng.normal(size=300 * sr) * np.repeat(rng.uniform(0, 1, 300) > 0.4, sr)
            # SG reel: reel seconds = GET - constant (no drift), and the map places it b = 0.8 s late.
            constant, b = 1000.0, 0.8
            write_wav(Path(tmp) / 'reel.wav', speech)
            source = dict(kind='ia', name='R', audio=str(Path(tmp) / 'reel.wav'),
                          reel=dict(drift_coefficients_s=[0.0], duration_s=300.0),
                          segments=[dict(key='ia:R:0', source_start_s=0.0, source_end_s=300.0, constant_s=constant + b,
                                         get_start_s=constant + b, get_end_s=constant + b + 300.0)])
            # Channel: canonical origin 7 s, stagger +1.37 s, rate 1; true Phi(t_ref) = constant + 20 + t_ref.
            origin, stagger = 7.0, 1.37
            t_local = np.arange(260 * sr) / sr
            true_get = constant + 20.0 + (origin + t_local - stagger)
            write_wav(Path(tmp) / 'ch.wav', np.interp(true_get - constant, np.arange(len(speech)) / sr, speech))
            channel = dict(channel=5, wav=str(Path(tmp) / 'ch.wav'), sample_rate=sr, origin_s=origin)
            error = 0.05
            knots = sg_curve.Basis(400.0, 120.0)
            coef = np.linalg.lstsq(knots.value(np.linspace(0, 400, 200)).toarray(),
                                   constant + 20.0 + np.linspace(0, 400, 200) - error, rcond=None)[0]
            curve = sg_curve.Curve('T', knots.knots, coef, np.array([0.0]), [], 0.0, 400.0, 1.0)
            ref = sg_audio.ReferenceAudio(source)
            res = sg_audio.refine_window(channel, curve, stagger, 150.0, ref, b={'ia:R:0': b})
            self.assertTrue(res['accepted'])
            self.assertAlmostEqual(res['lag_s'], error, delta=0.003)
            events, _ = sg_audio.audio_events([dict(res, group=0)], 0)
            # The event is in the map frame: Phi(t_ref) + b.
            self.assertAlmostEqual(events[0]['g'], constant + 20.0 + 150.0 + b, delta=0.003)

    def test_quindar_match_uses_the_curve_frame_for_ia(self):
        knots = sg_curve.Basis(400.0, 120.0)
        coef = np.linalg.lstsq(knots.value(np.linspace(0, 400, 200)).toarray(), 1000.0 + np.linspace(0, 400, 200), rcond=None)[0]
        curve = sg_curve.Curve('T', knots.knots, coef, np.array([0.0]), [], 0.0, 400.0, 1.0)
        bursts = [dict(channel=1, t=100.0, frequency_hz=2525.0, residual_fm_sd_hz=1.0)]
        refs = [dict(get=1100.0 + 0.6 + 0.01, frequency_hz=2525.0, family=1, source='R', kind='ia', segment=0)]
        out, witnesses = sg_audio.match_quindar(bursts, curve, [0.0], refs, dict(key_hz=2525.0, unkey_hz=2475.0),
                                                b={'ia:R:0': 0.6})
        self.assertAlmostEqual(out[0]['residual_s'], 0.01, places=6)
        self.assertEqual(witnesses[0]['basis'], 'calibrated_generator')
        self.assertAlmostEqual(witnesses[0]['rate'], 1.0, places=9)


class InputTests(unittest.TestCase):
    def store(self, tmp):
        root = Path(tmp) / 'tx'
        return sg_inputs.TranscriptStore('x', root, Path(tmp) / 'ev', 'asr-abc'), root

    def chunk(self, folder, index, sha):
        folder.mkdir(parents=True, exist_ok=True)
        (folder / 'chunk.json').write_text(json.dumps(dict(status='complete', index=index, segments_sha256=sha,
                                                           identity_id='id1')))

    def test_state_binds_alignment_and_running_sessions_make_editions_provisional(self):
        import sg_stage
        from mission_align import align_id
        with tempfile.TemporaryDirectory() as tmp:
            store, root = self.store(tmp)
            ident = root / 'asr-abc' / 'sources' / 'tape' / 'T' / 'CH01' / 'id1'
            self.chunk(ident / 'chunks' / 'c00000', 0, 'a')
            before = sg_inputs.transcript_state(ident)
            self.assertFalse(before['aligned'])
            # Aligning the chunk in place replaces its rows and receipt.
            path = ident / 'chunks' / 'c00000' / 'chunk.json'
            path.write_text(json.dumps(dict(json.loads(path.read_text()), segments_sha256='b',
                                            alignment=dict(align_id=align_id()))))
            after = sg_inputs.transcript_state(ident)
            self.assertNotEqual(before['sha256'], after['sha256'])
            self.assertTrue(after['aligned'])
            self.assertEqual(sg_inputs.store_state(store)['running'], [])
            session = store.evidence_root / 'sessions' / 's1'
            session.mkdir(parents=True)
            (session / 'session.json').write_text(json.dumps(dict(kind='first_pass', settings_id='asr-abc',
                                                                  status='running', pid=os.getpid(), session='s1')))
            self.assertEqual(sg_inputs.store_state(store)['running'], ['s1'])
            with unittest.mock.patch.dict(sg_stage.STORES, {'x': store}):
                self.assertEqual(sg_stage.readiness(('x',), [('CH01', after)]), ['x: transcription session s1 running'])
                (session / 'session.json').write_text(json.dumps(dict(kind='first_pass', status='complete', session='s1')))
                self.assertEqual(sg_stage.readiness(('x',), [('CH01', after)]), [])
                self.assertIn('not fully aligned', sg_stage.readiness(('x',), [('CH01', before)])[0])
                with self.assertRaises(sg_stage.Provisional):
                    sg_stage.check_ready(sg_stage.readiness(('x',), [('CH01', before)]), False)

    def test_words_carry_alignment(self):
        rows = [dict(sf=8000, flags=['aligned'], words=[dict(w=' Roger', sf=8000, ef=12000, p=0.9, ap=0.4, flags=[])]),
                dict(sf=16000, flags=[], words=[dict(w=' copy', sf=16000, ef=20000, p=0.9, flags=[])]),
                dict(sf=24000, flags=['align_failed'], words=[dict(w=' out', sf=24000, ef=26000, p=0.9, flags=[])])]
        w = sg_inputs.words_from_rows(rows, 8000)
        self.assertEqual(list(w.tok), ['roger', 'copy'])
        self.assertEqual(list(w.aligned), [True, False])

    def test_channel_refused_when_origins_disagree(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, root = self.store(tmp)
            data = Path(tmp) / 'data'
            maps = data / 'deflutter_maps' / 'T'
            maps.mkdir(parents=True)
            plan = dict(inventory=dict(channels=[dict(channel=1, start_seconds=7.0), dict(channel=2, start_seconds=0.0)]))
            (maps / 'plan.json').write_text(json.dumps(plan))
            plan_sha = sg_inputs.sha256_file(maps / 'plan.json')
            for ch, start, origin in ((1, 7.0, 7.0), (2, 0.0, 7.0)):
                folder = data / 'defluttered_shared' / 'T' / f'CH{ch:02d}'
                folder.mkdir(parents=True)
                (folder / 'deflutter.wav').write_bytes(b'')
                review = dict(status='complete', channel=ch, start_seconds=start, frames=8000, sample_rate=8000,
                              source=dict(acquisition_start_seconds=0), map_sha256='m', plan_sha256=plan_sha,
                              shared_map=str(Path(tmp) / 'gone' / 'tape_map.npy'))
                (folder / 'review.json').write_text(json.dumps(review))
                ident = root / 'asr-abc' / 'sources' / 'tape' / 'T' / f'CH{ch:02d}' / 'id'
                ident.mkdir(parents=True)
                (ident / 'source.json').write_text(json.dumps(dict(identity=dict(
                    review_sha256=sg_inputs.sha256_file(folder / 'review.json'), canonical_origin_s=origin, map_sha256='m'))))
            rows = {r['channel']: r for r in sg_inputs.tape_channels('T', store, data / 'defluttered_shared')}
            self.assertIsNone(rows[1]['problem'])
            self.assertEqual(rows[1]['origin_s'], 7.0)
            self.assertIn('06a origin', rows[2]['problem'])


if __name__ == '__main__':
    unittest.main()
