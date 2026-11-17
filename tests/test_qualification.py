"""Archive assessment, follow-up, qualification, validation and QA selection."""
import importlib.util
import io
import json
import subprocess
from contextlib import redirect_stdout
from pathlib import Path
import sys
import tempfile
import unittest

import numpy as np
from scipy.ndimage import uniform_filter1d

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'scripts'))
sys.path.insert(0, str(ROOT/'tests'))
from _bootstrap import install
install()
import flutter_analysis as analysis
import flutter_census as census
import flutter_detection as detection
import flutter_followup as followup
import flutter_qualification as qualification
import pipeline
import qualification_policy as rules
from census_fixtures import SR, channels, fm_tone, write_tape
from flutter_planning import inventory
from shared_tape import load_tape_plan, select_intervals
from test_census_stage import denoised_like, genus_factory, quiet

TAPE = 'A15_T900'
SETTINGS = census.make_settings(block_seconds=60.)
INVERT_AFTER = 80.


def beeps(t, e, invert_after):
    """2.5 kHz check beeps recorded on tape; after ``invert_after`` their motion is inverted."""
    gate = np.zeros(len(t))
    for start in np.arange(1., t[-1]-1, 2.1):
        gate[(t >= start) & (t < start+.25)] = 1
    gate = uniform_filter1d(gate, 80)
    return gate*np.where(t >= invert_after, fm_tone(t, 2500., -e, .05), fm_tone(t, 2500., e, .05))


def make_tape(root, seconds=150, tape=TAPE, invert_after=INVERT_AFTER):
    audio, motion = channels(seconds)
    t = np.arange(seconds*SR)/SR
    audio[6] = (.004*np.random.default_rng(6).standard_normal(len(t))+beeps(t, motion, invert_after)).astype(np.float32)
    write_tape(root/'originals'/tape, tape, audio)
    return root/'originals'/tape


def plan_cli():
    spec = importlib.util.spec_from_file_location('plan_cli_q', ROOT/'scripts/05_deflutter/05a_plan_deflutter.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class QualificationWorkflowTests(unittest.TestCase):
    """One census; the workflow steps run in order on it."""

    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name).resolve()
        cls.folder = make_tape(cls.root)
        cls.other = make_tape(cls.root, tape='A15_T901', invert_after=0)
        cls.run_dir, cls.config = detection.open_run(root=cls.root/'census', settings=SETTINGS,
                                                     originals=cls.root/'originals')
        session = detection.Session(cls.run_dir, cls.config)
        code = detection.coordinate(session, [cls.folder, cls.other], local_workers=1, genus_workers=0, output=quiet,
                                    worker_factory=genus_factory(cls.root/'scratch', cls.root))
        assert code == 0
        cls.bundle = analysis.assess(cls.run_dir, root=cls.root/'analysis', progress=quiet)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_1_assessment_accounts_for_tape_time_events_and_flags(self):
        bundle = self.bundle
        self.assertEqual(bundle['dir'].parent.name, self.config['run_id'])
        tape = analysis.tape_rows(bundle)[TAPE]
        duration = inventory(self.folder)['duration_seconds']
        intervals = tape['intervals']
        self.assertAlmostEqual(intervals['analysed_seconds']+intervals['unmeasured_seconds'], duration, places=6)
        self.assertLessEqual(intervals['candidate_supported_seconds'], intervals['analysed_seconds'])
        self.assertIsNone(intervals['qualified_seconds'])
        events = analysis.event_rows(bundle, TAPE)
        rows = analysis.window_rows(bundle, TAPE)
        # Overlapping windows measure the same beep; it is one physical event.
        measurements = sum(len((w.get('beep_audit') or {}).get('events', []))
                           for w in detection.load_census(self.run_dir, TAPE)['evidence']['windows'])
        self.assertGreater(measurements, len(events))
        self.assertEqual(len({e['id'] for e in events}), len(events))
        self.assertEqual({e['partition'] for e in events}, {'decision', 'held_out'})
        late = [r for r in rows if r['eligible'] and r['valid'][0] > INVERT_AFTER]
        early = [r for r in rows if r['eligible'] and r['valid'][1] < INVERT_AFTER]
        self.assertTrue(late and early)
        self.assertTrue(all('adverse_audit' in r['flags'] for r in late))
        self.assertTrue(all(not set(r['flags']) & set(rules.CONTRADICTION_FLAGS) for r in early))
        queued = {q['window'] for q in analysis.queue_items(bundle)}
        self.assertTrue({r['id'] for r in late} <= queued)
        summary = analysis.read_json(bundle['dir']/'archive_summary.json')
        self.assertEqual(summary['tapes'], {'complete': 2})
        self.assertIsNone(summary['tape_time']['qualified_seconds'])
        # Reopening reuses the bundle; another policy is another analysis ID.
        self.assertEqual(analysis.assess(self.run_dir, root=self.root/'analysis', progress=quiet)['dir'], bundle['dir'])
        manifest = analysis.read_json(bundle['dir']/'input_manifest.json')
        changed = json.loads(json.dumps(rules.POLICY))
        changed['overlap']['min_r'] = .7
        self.assertNotEqual(analysis.analysis_identity(manifest, changed, analysis.DEFAULT_SEED)[0],
                            bundle['config']['analysis_id'])
        with tempfile.TemporaryDirectory(prefix='A15_T', dir=self.run_dir) as broken:
            (Path(broken)/'inventory.json').write_text('{')
            manifest = analysis.input_manifest(self.run_dir)
            states = {t['tape']: t['status'] for t in manifest['tapes']}
            self.assertEqual(states[Path(broken).name], 'stale')
            self.assertEqual(states[TAPE], 'complete')

    def test_end_to_end_with_colliding_window_ids_on_different_tapes(self):
        with self.assertRaisesRegex(analysis.AnalysisError, 'follow-up pass is not complete'):
            qualification.qualify(self.bundle['dir'], progress=quiet)
        preview = qualification.qualify(self.bundle['dir'], allow_incomplete=True, progress=quiet)
        self.assertFalse(preview['complete'])
        self.assertGreater(preview['seconds']['incomplete'], 0)
        self.assertFalse((self.bundle['dir']/'qualification'/'summary.json').exists())
        command = [sys.executable, str(ROOT/'scripts/run.py'), '05_analyze_flutter.py', 'run',
                   '--census-dir', str(self.run_dir), '--root', str(self.root/'analysis'), '--workers', '2']
        automatic = subprocess.run(command, capture_output=True, text=True)
        self.assertTrue(automatic.stdout.strip(), automatic.stderr)
        run = json.loads(automatic.stdout)
        self.assertEqual(run['analysis_id'], self.bundle['config']['analysis_id'])
        self.assertTrue(run['qualification_complete'])
        self.assertEqual(automatic.returncode, 0 if run['validation_passed'] else 2)
        status = analysis.read_json(self.bundle['dir']/'followup'/'status.json')
        self.assertTrue(status['complete'])
        self.assertEqual(followup.run_followup(self.bundle['dir'], workers=1, progress=quiet)['counts']['complete'],
                         status['jobs'])
        results = followup.load_results(self.bundle, require_complete=True)
        late = [r for (tape, wid), r in results.items() if tape == TAPE and r['eligible']
                and float(wid.split('@')[1]) > INVERT_AFTER-4]
        self.assertTrue(late)
        for r in late:
            self.assertEqual(rules.pooled_beeps(r['beeps'], followup.LAGS)['state'], 'inverted')
            self.assertGreater(r['reproduction_r'], .9)
        summary = qualification.qualify(self.bundle['dir'], progress=quiet)
        self.assertTrue(summary['complete'])
        q = qualification.load_qualified(self.bundle['dir'], TAPE)
        other = qualification.load_qualified(self.bundle['dir'], self.other.name)['decisions']
        self.assertEqual(other['qualified_seconds'], 0)
        self.assertTrue(any((TAPE, wid) in results for (tape, wid) in results if tape == self.other.name))
        decisions = q['decisions']
        contradicted = [s for s in decisions['segments'] if s['decision'] == 'bypass_unresolved_contradiction']
        applied = [s for s in decisions['segments'] if s['decision'] == 'apply']
        self.assertTrue(contradicted and applied)
        self.assertLess(max(s['end_seconds'] for s in applied), min(s['start_seconds'] for s in contradicted)+1e-9)
        self.assertGreater(min(s['start_seconds'] for s in contradicted), INVERT_AFTER-64)
        self.assertGreater(decisions['qualified_fraction'], .2)
        # An applied window overlapping a contradiction never leaks into it.
        stream = np.fromfile(q['stream_path'], '<f8').reshape(-1, 2)
        mask = qualification.mask_on_grid([[s['start_seconds'], s['end_seconds']] for s in contradicted], len(stream))
        self.assertTrue(np.all(stream[mask] == 0))
        self.assertGreater(stream[:, 1].sum(), 0)
        self.assertEqual(qualification.qualify(self.bundle['dir'], progress=quiet)['files'], summary['files'])
        report = qualification.validate(self.bundle['dir'])
        part = report['partitions'][decisions['partition']]
        self.assertEqual(part['join']['support_outside_applied_samples'], 0)
        self.assertLess(part['held_out_applied']['median_ratio'], .2)
        self.assertGreater(part['held_out_contradicted']['median_ratio'], 1.)
        self.assertEqual(qualification.validate(self.bundle['dir']), report)
        resumed = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(resumed.returncode, automatic.returncode, resumed.stderr)
        self.assertEqual(json.loads(resumed.stdout), run)
        self.check_plan_and_qa()
        self.check_changed_artifacts()

    def check_plan_and_qa(self):
        plans = self.root/'deflutter_planning_census'/TAPE
        with redirect_stdout(io.StringIO()) as text:
            self.assertEqual(plan_cli().main(['--input', str(self.folder), '--evidence-root', str(self.root/'none'),
                                              '--qualification-root', str(self.bundle['dir']),
                                              '--output', str(plans)]), 0)
        printed = json.loads(text.getvalue())
        self.assertTrue(printed['qualified'])
        self.assertEqual(printed['analysis_id'], self.bundle['config']['analysis_id'])
        self.assertGreater(printed['excluded_seconds'], 0)
        _, plan, data = load_tape_plan(plans)
        self.assertTrue(plan['qualified'])
        self.assertEqual(plan['census']['kind'], 'qualified')
        decisions = qualification.load_qualified(self.bundle['dir'], TAPE)['decisions']
        for s in decisions['segments']:
            if s['decision'] == 'bypass_unresolved_contradiction':
                inside = (data[:, 0] >= s['start_seconds']) & (data[:, 0] < s['end_seconds'])
                self.assertTrue(np.all(data[inside, 3] == 0))
        self.assertGreater(float(data[:, 3].max()), .99)
        del data
        from deflutter_readiness import readiness
        row = readiness(self.root/'originals', plans.parent, self.root/'none', self.root/'none',
                        qualification=self.bundle['dir'])['tapes'][0]
        self.assertTrue(row['qualification_complete'] and row['qualified_plan'])
        self.assertAlmostEqual(row['qualified_fraction'], decisions['qualified_fraction'])
        # QA selection is frozen once, before any listening.
        audio = denoised_like(self.root, self.folder)
        with redirect_stdout(io.StringIO()):
            raw = self.root/'raw_plans'/TAPE
            self.assertEqual(plan_cli().main(['--input', str(self.folder), '--evidence-root', str(self.root/'none'),
                                              '--raw-census-root', str(self.run_dir), '--output', str(raw)]), 0)
        wrong_plan = readiness(self.root/'originals', raw.parent, audio.parent, self.root/'none',
                               qualification=self.bundle['dir'])['tapes'][0]
        self.assertTrue(wrong_plan['qualification_complete'])
        self.assertEqual(wrong_plan['denoised_channels'], wrong_plan['expected_channels'])
        self.assertFalse(wrong_plan['ready_for_render'])
        speech = lambda path, row, start, seconds: 8. if row['channel'] == 3 else 0.
        selection = qualification.qa_batch(self.bundle['dir'], audio.parent, per_stratum=1, diagnostic=2,
                                           excerpt_seconds=24., speech=speech)
        self.assertEqual(sum(s['diagnostic'] for s in selection['selections']), 2)
        main = [s for s in selection['selections'] if not s['diagnostic']]
        self.assertTrue(main and all(s['decision'] == 'apply' for s in main))
        self.assertEqual(selection['counts']['speech_free'], 0)
        self.assertTrue(all(s['channel'] == 3 and not s['speech_free'] for s in selection['selections'] if s['tape'] == TAPE))
        self.assertTrue(all(s['audio_unavailable'] for s in selection['selections'] if s['tape'] == self.other.name))
        self.assertEqual(qualification.qa_batch(self.bundle['dir'], audio.parent, speech=lambda *a: 0.), selection)
        with self.assertRaisesRegex(ValueError, 'stage-04 crosstalk'):
            qualification.qa_render(self.bundle['dir'], plans.parent, audio.parent, self.root/'qa_render')
        renders = qualification.qa_render(self.bundle['dir'], plans.parent, audio.parent, self.root/'qa_render',
                                          progress=quiet, skip_crosstalk=True)
        self.assertEqual(len({r['map_sha256'] for r in renders if 'map_sha256' in r}), 1)
        self.assertTrue((self.root/'qa_render'/'manifest.json').is_file())
        with self.assertRaisesRegex(ValueError, 'must be new'):
            qualification.qa_render(self.bundle['dir'], plans.parent, audio.parent, self.root/'qa_render')

    def check_changed_artifacts(self):
        result_dir = next((self.bundle['dir'].parent/'followup_cache').glob('*/result.json')).parent
        measurements = result_dir/'result.json'
        saved = measurements.read_bytes()
        try:
            measurements.write_bytes(saved+b' ')
            with self.assertRaisesRegex(analysis.AnalysisError, 'measurements changed'):
                followup.verify_result(result_dir, result_dir.name)
        finally:
            measurements.write_bytes(saved)
        path = self.bundle['dir']/'qualification'/TAPE/'decisions.json'
        saved = path.read_bytes()
        path.write_bytes(saved.replace(b'"apply"', b'"applx"', 1))
        try:
            with self.assertRaisesRegex(analysis.AnalysisError, 'changed'):
                qualification.load_qualified(self.bundle['dir'], TAPE)
        finally:
            path.write_bytes(saved)
        windows = self.bundle['dir']/'windows.jsonl'
        saved = windows.read_bytes()
        windows.write_bytes(saved+b'\n')
        try:
            with self.assertRaisesRegex(analysis.AnalysisError, 'changed'):
                analysis.open_analysis(self.bundle['dir'])
        finally:
            windows.write_bytes(saved)


class IntervalAndEventTests(unittest.TestCase):
    def test_interval_unions_never_double_count_overlaps(self):
        self.assertEqual(analysis.union([[0, 5], [3, 8], [10, 12]]), [[0, 8], [10, 12]])
        self.assertEqual(analysis.total([[0, 5], [3, 8], [3, 4]]), 8)
        self.assertEqual(analysis.subtract([[0, 10]], [[2, 3], [5, 12]]), [[0, 2], [3, 5]])
        self.assertEqual(analysis.intersect([[0, 10], [20, 30]], [[5, 25]]), [[5, 10], [20, 25]])
        self.assertEqual(analysis.clip([[-5, 3], [8, 20]], 0, 10), [[0, 3], [8, 10]])

    def test_beep_copies_collapse_and_delayed_copies_stay_uncertain(self):
        def window(block, start, events):
            return dict(block=block, start_seconds=start, valid_start_seconds=start+4, valid_end_seconds=start+60,
                        beep_audit=dict(events=[dict(channel=c, start=s, identity=50., curve=10., wrong_sign=90.,
                                                     shift=55.) for c, s in events]))
        windows = [window('0000', 0., [(1, 20.), (2, 20.01), (5, 20.02), (7, 21.3)]),
                   window('0000', 42., [(1, 50.)]),
                   window('0001', 10., [(1, 20.), (1, 50.)])]
        events, uncertain = analysis.collapse_events(TAPE, windows, {'0000': [[1, 2]], '0001': []}, [], 1, rules.POLICY)
        self.assertEqual([e['start'] for e in events], [20., 21.3, 50.])
        first = events[0]
        self.assertEqual(first['association'], 'simultaneous')
        self.assertEqual(first['channels'], [1, 2, 5])
        self.assertEqual(first['groups'], [1, 5])
        self.assertEqual(sorted(first['windows']), ['0000@0.000', '0001@10.000'])
        self.assertEqual(first['primary_window'], '0000@0.000')  # Event nearer that window's centre.
        self.assertEqual(first['ratio'], .2)
        self.assertEqual(uncertain, [dict(first=f'{TAPE}@20.000', second=f'{TAPE}@21.300', delay_seconds=1.3,
                                          note='Possible delayed crosstalk copy; not merged')])
        duplicate, _ = analysis.collapse_events(TAPE, [window('0000', 0., [(1, 20.), (2, 20.)])],
                                                {'0000': [[1, 2]]}, [], 1, rules.POLICY)
        self.assertEqual(duplicate[0]['association'], 'duplicate_feed')


class PolicyTests(unittest.TestCase):
    ROW = dict(eligible=True, flags=['adverse_audit'], sign_conflict=False, reasons=[], **{'class': 'needs_analysis'})
    LAGS = followup.LAGS

    def beeps(self, r0):
        zero = self.LAGS.index(0)
        cc = [1.]*len(self.LAGS)
        dc = [0.]*len(self.LAGS)
        dc[zero] = r0
        return [dict(n=100, dd=1., cc=cc, dc=dc)]

    def result(self, r0, hf_concentrates=2, hf_reversed=0):
        hf = [dict(group=i, hz=250.+73*i, concentrates=i < hf_concentrates, reversed_better=i < hf_reversed)
              for i in range(max(2, hf_concentrates))]
        return dict(status='complete', reproduction_r=.97, reproduced=dict(eligible=True), family=dict(min_r=.9, max_group_share=.2),
                    hf=hf, sign_conflict=None, beep_state=rules.pooled_beeps(self.beeps(r0), self.LAGS))

    def test_contradiction_resolution_is_general_and_never_flips_signs(self):
        self.assertEqual(rules.resolve(self.ROW, self.result(-.6), rules.POLICY)['decision'],
                         'bypass_unresolved_contradiction')
        resolved = rules.resolve(self.ROW, self.result(.05), rules.POLICY)
        self.assertEqual(resolved['decision'], 'apply')
        self.assertIn('does not follow', resolved['resolution'])
        for missing in (dict(beep_state={'state': 'insufficient'}), dict(reproduced={'eligible': False})):
            self.assertEqual(rules.resolve(self.ROW, dict(self.result(.05), **missing))['decision'],
                             'bypass_unresolved_contradiction')
        for key, value in [('hz', 500.), ('group', 0)]:
            result = self.result(.05)
            result['hf'][1][key] = value
            self.assertEqual(rules.resolve(self.ROW, result)['decision'], 'bypass_unresolved_contradiction')
        self.assertEqual(rules.resolve(self.ROW, self.result(.05, hf_concentrates=0), rules.POLICY)['decision'],
                         'bypass_unresolved_contradiction')
        self.assertEqual(rules.resolve(self.ROW, None, rules.POLICY)['decision'], 'incomplete')
        candidate = dict(self.ROW, flags=[], **{'class': 'candidate'})
        self.assertEqual(rules.resolve(candidate, None, rules.POLICY)['decision'], 'apply')
        self.assertEqual(rules.resolve(dict(self.ROW, eligible=False), None, rules.POLICY)['decision'],
                         'bypass_unsupported')
        unstable = dict(self.result(.5), family=dict(min_r=.2, max_group_share=.2))
        self.assertEqual(rules.resolve(dict(self.ROW, flags=['near_threshold']), unstable, rules.POLICY)['decision'],
                         'bypass_unresolved_contradiction')
        self.assertEqual(rules.resolve(dict(self.ROW, flags=['near_threshold']), self.result(.5), rules.POLICY)['decision'],
                         'apply')

    def test_missing_beeps_do_not_flag_and_partitions_are_deterministic(self):
        empty = dict(n=0, median_ratio=None, median_wrong_sign_ratio=None)
        self.assertEqual(rules.audit_flags(empty, [], rules.POLICY), [])
        self.assertEqual(rules.tape_partition(1, 'A15_T510073'), 'development')
        values = [rules.event_partition(7, TAPE, f'e{i}') for i in range(200)]
        self.assertEqual(values, [rules.event_partition(7, TAPE, f'e{i}') for i in range(200)])
        self.assertTrue(60 < values.count('decision') < 140)

    def test_decision_beeps_exclude_unmatched_and_held_out_copies(self):
        events = [dict(id='a', start=1., partition='held_out', windows={'w': {'channels': [1]}}),
                  dict(id='b', start=2., partition='decision', windows={'w': {'channels': [2, 3]}})]
        beeps = [dict(channel=c, start=t) for c, t in [(7, 1.01), (3, 2.01), (2, 2.), (9, 4.)]]
        self.assertEqual(qualification.decision_beeps(dict(window='w', beeps=beeps), events), [beeps[2]])

    def test_applied_audit_requires_a_contributing_window(self):
        event = dict(start=4., primary_window='unsupported', windows={'unsupported': {'ratio': .1}, 'ok': {'ratio': 1.2}})
        self.assertEqual(qualification.applied_measurement(event, {'ok': [0, 5]}), {'ratio': 1.2})
        self.assertIsNone(qualification.applied_measurement(event, {'ok': [5, 10]}))

    def test_exclusions_bypass_every_source(self):
        times = np.arange(0, 20*200)/200
        path = Path(tempfile.mkdtemp())/'s.f64'
        np.column_stack((.001*np.sin(times), np.ones_like(times))).astype('<f8').tofile(path)
        source = dict(id='v5', source_key='v5', priority=1, rank=1., start_seconds=0, end_seconds=20,
                      curve=dict(format='stream', path=str(path), rate=200, frames=len(times), start_seconds=0, dtype='<f8'))
        chosen = select_intervals([source], 0, 20, [[5, 8]])
        self.assertEqual([(s['start_seconds'], s['end_seconds'], s['selected'], s.get('excluded', False)) for s in chosen],
                         [(0, 5, 'v5', False), (5, 8, None, True), (8, 20, 'v5', False)])
        self.assertEqual(select_intervals([source], 0, 20), [dict(start_seconds=0, end_seconds=20, selected='v5')])

    def test_regression_rule_on_saved_corpus_layout(self):
        folder = Path(tempfile.mkdtemp())
        for name in ('p1_confirm_v2', 'p1_sweep3', 'p1_sweep3_rerun'):
            (folder/name).mkdir()
        good = dict(eligible=True, start_seconds=100., valid_start_seconds=104., valid_end_seconds=160.,
                    beep_audit=dict(events=[dict(channel=1, start=110.+2*i, identity=50., curve=10., wrong_sign=90.,
                                                 shift=55.) for i in range(12)]))
        bad = dict(good, start_seconds=300., valid_start_seconds=304., valid_end_seconds=360.,
                   beep_audit=dict(events=[dict(channel=21, start=310.+2*i, identity=50.,
                   curve=95., wrong_sign=45., shift=100.) for i in range(12)]))
        overlap = dict(good, start_seconds=101., valid_start_seconds=105., valid_end_seconds=161.)
        for name, w in (('p1_confirm_v2', good), ('p1_sweep3', good), ('p1_sweep3', bad), ('p1_sweep3', overlap)):
            (folder/name/f'A15_T1_{w["start_seconds"]:g}.json').write_text(json.dumps(dict(
                tape='A15_T1', t0=w['start_seconds'], window=w, duplicate_groups=[])))
        report = qualification.regression(folder)
        self.assertEqual(report['sets']['confirmation']['contradicted'], [])
        self.assertEqual(report['sets']['sweep3']['held_out_kept'], report['sets']['confirmation']['held_out_kept'])
        self.assertEqual([c['start_seconds'] for c in report['sets']['sweep3']['contradicted']], [300.])
        self.assertGreater(report['sets']['sweep3']['held_out_removed']['median_ratio'], 1)


class PipelineForwardingTests(unittest.TestCase):
    def test_stage_05a_forwards_the_qualification_bundle_not_the_raw_census(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write_tape(root/'originals'/'A15_T001', 'A15_T001', {1: np.zeros(20*SR, np.float32)})
            bundle = root/'deflutter_analysis/census-0123456789abcdef/analysis-0123456789abcdef'
            args, selected = pipeline.parse_args(['plan', '--concatenated', str(root/'originals'), '--output',
                                                  str(root/'runs'), '--plain', '--tape', 'A15_T001', '--stages', '05a',
                                                  '--flutter-qualification-root', str(bundle),
                                                  '--flutter-detection-root', str(root/'census')])
            job = [j for j in pipeline.make_jobs(args, selected, root/'runs/plan') if j['status'] == 'pending'][0]
            command = job['command']
            self.assertEqual(command[command.index('--qualification-root')+1], str(bundle))
            self.assertNotIn('--detection-root', command)
            self.assertNotIn('--raw-census-root', command)


if __name__ == '__main__':
    unittest.main()
