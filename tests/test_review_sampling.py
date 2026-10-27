"""Representative listening, scope and the actual CLI production gate."""
from copy import deepcopy
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
import soundfile as sf

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from review_sampling import (POLICY, PILOTS, select_samples, assess, fingerprint,
                             immutable_json, read_json, bound, verify_bindings, policy_hash, audio_activity,
                             comparison_change, automatic_no_change)
from review_fixtures import make_demo
from review_evidence import Desk
from flutter_policy import require_production, require_rollout, implementation_digest, policy_digest
from channel_review import render_warp


def candidates(n=80):
    return [dict(id=str(i).zfill(3),tape='A15_T510083',channel=2,start_frame=i*160000,
                 frames=160000,sample_rate=8000,conditions=['early' if i%3 == 0 else 'middle' if i%3 == 1 else 'late',f'cohort:{i%4}'],
                 cohort=str(i%4),eligible=True,risk=4 if i<15 else 0) for i in range(n)]


class SelectionTests(unittest.TestCase):
    def test_comparison_check_skips_only_exact_playback_equality(self):
        a=np.sin(np.arange(8000)*.1)*.05
        check=comparison_change(a,a.copy())
        self.assertEqual(check['status'],'identical')
        self.assertFalse(check['human_review_required'])
        changed=a.copy();changed[100]+=2**-23
        check=comparison_change(a,changed)
        self.assertEqual(check['status'],'changed')
        self.assertTrue(check['human_review_required'])
        self.assertEqual(check['audible_benefit'],'not_assessed')
        self.assertIsNone(comparison_change(np.zeros(100),np.zeros(100))['difference_dbfs'])
        for invalid in (np.zeros(0),np.zeros(5),np.array([np.nan]*8000),np.zeros((8000,1))):
            with self.assertRaises(ValueError):comparison_change(a,invalid)

    def test_phase_difference_is_not_a_quality_or_audibility_verdict(self):
        t=np.arange(8000)/8000
        a=.1*np.sin(2*np.pi*500*t);b=.1*np.sin(2*np.pi*500*t+.8)
        check=comparison_change(a,b)
        self.assertEqual(check['status'],'changed')
        self.assertEqual(check['audible_benefit'],'not_assessed')
        self.assertTrue(check['human_review_required'])

    def test_automatic_completion_needs_matching_bound_audio_and_retains_failures(self):
        sample=dict(id='s',ready=True,excerpt=dict(comparison_check=comparison_change(np.zeros(10),np.zeros(10)),
                                                 audio_hashes={'a.wav':'same','b.wav':'same'}))
        self.assertTrue(automatic_no_change(sample))
        result=assess(dict(samples=[sample]),[])
        self.assertTrue(result['complete'])
        self.assertEqual(result['automated_unchanged'],['s'])
        bad=dict(sample_id='s',judgment='worse',flags={},cohorts=['c'])
        self.assertFalse(assess(dict(samples=[sample]),[],[bad])['accepted_findings'])
        sample['excerpt']['audio_hashes']['b.wav']='different'
        self.assertFalse(automatic_no_change(sample))
        self.assertFalse(assess(dict(samples=[sample]),[])['complete'])

    def test_tape_spot_check_is_random_reproducible_and_bounded(self):
        source=candidates(80)
        a=select_samples(source,3,20,42,tape_random=True)
        b=select_samples(list(reversed(source)),3,20,42,tape_random=True)
        self.assertEqual([s['id'] for s in a['samples']],[s['id'] for s in b['samples']])
        self.assertNotEqual([s['id'] for s in a['samples']],
                            [s['id'] for s in select_samples(source,3,20,43,tape_random=True)['samples']])
        self.assertEqual(a['estimated_listening_seconds'],120)
        self.assertEqual(a['random_selected'],3)
        self.assertTrue(all(s['selection_reason']=='Reproducible random tape spot check' for s in a['samples']))
        for budget in (1,2,6,100):
            with self.assertRaises(ValueError):select_samples(source,budget,tape_random=True)
        short=select_samples(source[:2],3,tape_random=True)
        self.assertTrue(short['unavailable_ordinary_random'])
        self.assertEqual(short['comparisons'],2)

    def test_activity_screen_rejects_silence_hum_and_stationary_noise(self):
        sr=8000
        t=np.arange(20*sr)/sr
        rng=np.random.default_rng(15)
        for x in (np.zeros(len(t)), .02*np.sin(2*np.pi*120*t),
                  rng.normal(0,1e-4,len(t)), rng.normal(0,.02,len(t))):
            self.assertFalse(audio_activity(x,sr)['usable'])
        # Intermittent broadband activity, including quiet but usable passages.
        x=rng.normal(0,.006,len(t))*((t%3)<1)
        result=audio_activity(x,sr)
        self.assertTrue(result['usable'],result)
        self.assertGreater(result['active_seconds'],2)

    def test_reproducible_bounded_risk_random_and_coverage(self):
        source=candidates()
        required=['early','middle','late']+[f'cohort:{i}' for i in range(4)]
        a=select_samples(source,30,20,42,required)
        b=select_samples(list(reversed(source)),30,20,42,required)
        self.assertEqual([s['id'] for s in a['samples']],[s['id'] for s in b['samples']])
        self.assertEqual(a['comparisons'],30)
        self.assertEqual(a['estimated_listening_seconds'],1200)
        self.assertEqual(a['uncovered_conditions'],[])
        self.assertEqual(a['random_selected'],6)
        self.assertEqual(sum(s['selection_reason']=='Highest-risk exception quota' for s in a['samples']),7)
        self.assertNotEqual([s['id'] for s in a['samples']],[s['id'] for s in select_samples(source,30,20,43,required)['samples']])

    def test_uncovered_and_invalid_budget_are_explicit(self):
        result=select_samples(candidates(3),1,20,1,['never_present'])
        self.assertIn('never_present',result['uncovered_conditions'])
        self.assertLessEqual(len(result['samples']),1)
        for budget in (0,101):
            with self.assertRaises(ValueError):select_samples([],budget)
        with self.assertRaises(ValueError):select_samples([],30,100)

    def test_content_lag_is_ignored_and_prior_coordinates_excluded(self):
        source=candidates(4)
        for c in source:c['content_lag_seconds']=3.5
        a=select_samples(source,4,20,1,prior=source[:1])
        self.assertEqual(len(a['samples']),3)
        for c in a['samples']:
            self.assertEqual(c['start_frame'],next(s['start_frame'] for s in source if s['id']==c['id']))

    def test_exact_duplicate_identity_retains_provenance(self):
        source=candidates(2)
        for c in source:c['comparison_sha256']='verified-identical-pair'
        a=select_samples(source,10)
        self.assertEqual(len(a['samples']),1)
        self.assertEqual(len(a['samples'][0]['provenance']),2)

    def test_failed_holdout_cannot_be_erased_by_revised_judgment(self):
        queue=dict(samples=[dict(id='s')])
        bad=dict(sample_id='s',judgment='worse',flags={},cohorts=['c'])
        good=dict(sample_id='s',judgment='improved',flags={},cohorts=['c'])
        result=assess(queue,[bad,good])
        self.assertTrue(result['complete'])
        self.assertFalse(result['accepted_findings'])
        self.assertEqual(result['failed_cohorts'],['c'])

    def test_hash_change_invalidates_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'evidence.json';p.write_text('{}')
            binding=bound(p);verify_bindings([binding]);p.write_text('{"changed":true}')
            with self.assertRaisesRegex(ValueError,'Stale'):verify_bindings([binding])


class DeskTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        cls.root=Path(cls.temp.name)
        cls.config=make_demo(cls.root/'fixture')

    def setUp(self):
        self.config=deepcopy(type(self).config)
        self.config['state']=str(self.root/'sessions'/fingerprint(self.id())[:12])
        self.desk=Desk(self.config)
        self.addCleanup(self.desk.pool.shutdown)
        self.desk.scan(lambda _:None)

    def make_queue(self,budget=30):
        result=self.desk.create_queue(dict(budget=budget),lambda _:None)
        return self.desk.queue(result['queue_id'])

    def judge_all(self,queue):
        for sample in queue['samples']:
            self.assertTrue(sample['ready'],sample.get('error'))
            self.desk.judge(dict(queue_id=queue['id'],sample_id=sample['id'],reviewer='Synthetic tester',judgment='unchanged'))

    def test_three_random_excerpts_complete_tape_listening_without_condition_expansion(self):
        tape=self.desk.catalog['tapes'][0]['tape']
        options=dict(kind='production',tapes=[tape],budget=3,tape_random=True,seed=15)
        queue=self.desk.queue(self.desk.create_queue(options,lambda _:None)['queue_id'])
        self.assertEqual(len(queue['samples']),3)
        self.assertEqual(queue['required_conditions'],['tape:'+tape])
        with self.assertRaisesRegex(ValueError,'cannot qualify'):
            self.desk.preview(dict(queue_id=queue['id'],kind='methodology',reviewer='Synthetic tester'))
        rollout=self.desk.state/'methodology/rollout_approval.json'
        immutable_json(rollout,dict(demo=True,status='accepted'))
        item=self.desk.item(tape,2)
        item['required_conditions'].extend(['unheard_rare_condition','unheard_channel_condition'])
        payload=dict(queue_id=queue['id'],kind='production',reviewer='Synthetic tester')
        self.assertEqual(self.desk.preview(payload)['status'],'blocked')
        self.judge_all(queue)
        preview=self.desk.preview(payload)
        self.assertEqual(preview['status'],'ready',preview['errors'])
        self.assertEqual(preview['scope'][0]['required_conditions'],['tape:'+tape])
        decision=self.desk.accept(preview['id'])
        self.assertEqual(decision['channels'],1)
        self.assertFalse(decision['production_launched'])
        # Passing listening never replaces independent automatic map checks.
        path=self.desk.map_directory(item)/'review.json';review=read_json(path)
        review['joined_map_checks']['status']='insufficient_reserved_events';path.write_text(json.dumps(review))
        preview=self.desk.preview(payload)
        self.assertEqual(preview['status'],'blocked')
        self.assertTrue(any('Final joined-map' in b for b in preview['scope'][0]['blockers']))

    def test_tape_spot_check_rejects_multiple_tapes_and_methodology(self):
        tapes=[t['tape'] for t in self.desk.catalog['tapes']]
        for options in (dict(kind='production',tapes=tapes[:2]),dict(kind='methodology',tapes=tapes[:1])):
            with self.assertRaisesRegex(ValueError,'one tape'):
                self.desk.selection(dict(options,budget=3,tape_random=True))

    def test_demo_inventory_covers_pilots_and_new_tape(self):
        self.assertEqual(len(self.desk.catalog['tapes']),6)
        selection=self.desk.selection({})
        self.assertEqual(selection['uncovered_conditions'],[])
        self.assertTrue(set(PILOTS).issubset(selection['tapes']))
        self.assertIn('A15_T599999',selection['tapes'])
        self.assertTrue(all(t['denoised_channels']==1 for t in self.desk.catalog['tapes']))

    def test_activity_screen_precedes_risk_selection_and_keeps_coverage_gaps(self):
        target=self.desk.catalog['candidates'][0]
        target['risk']=100
        target['conditions'].append('silent_boundary_only')
        self.desk.catalog['required_conditions'].append('silent_boundary_only')
        measure=self.desk.candidate_activity
        def activity(c):
            if c['channel']==target['channel'] and c['tape']==target['tape']:
                return dict(usable=False,status='Insufficient speech-band activity')
            return measure(c)
        with patch.object(self.desk,'candidate_activity',side_effect=activity):
            selection=self.desk.selection(dict(budget=3))
        self.assertEqual(len(selection['samples']),3)
        self.assertTrue(selection['activity_screened_out'])
        self.assertIn('silent_boundary_only',selection['uncovered_conditions'])
        self.assertTrue(all(s['tape']!=target['tape'] for s in selection['samples']))
        self.assertNotIn('faint_speech',selection['samples'][0]['activity'])

    def test_activity_screen_checks_budget_bounds_before_audio_reads(self):
        with patch.object(self.desk,'candidate_activity',side_effect=AssertionError('Audio read')):
            for options in (dict(seconds=0),dict(seconds=1000000),dict(budget=101)):
                with self.assertRaises(ValueError):self.desk.selection(options)

    def test_playback_gain_matches_levels_and_preserves_peaks(self):
        queue=self.make_queue(3)
        for sample in queue['samples']:
            self.assertTrue(sample['ready'],sample.get('error'))
            folder=self.desk.state/'queues'/queue['id']/sample['id']
            a,sr=sf.read(folder/'a.wav');b,_=sf.read(folder/'b.wav')
            rms_a=np.sqrt(np.mean(a*a));rms_b=np.sqrt(np.mean(b*b))
            self.assertAlmostEqual(rms_a,rms_b,places=6)
            self.assertLessEqual(max(np.max(abs(a)),np.max(abs(b))),.950001)
            self.assertGreater(sample['excerpt']['gain_a'],1)
            self.assertAlmostEqual(20*np.log10(rms_a),-24,places=3)
            self.assertEqual(sample['excerpt']['comparison_check']['status'],'changed')

    def test_saving_checks_only_heard_excerpt_and_approval_still_checks_all_evidence(self):
        queue=self.make_queue(3);sample=queue['samples'][0]
        with patch.object(self.desk,'verify',wraps=self.desk.verify) as verify:
            event=self.desk.judge(dict(queue_id=queue['id'],sample_id=sample['id'],reviewer='tester',judgment='improved',notes='Clearer speech'))
        checked=verify.call_args.args[0]
        self.assertEqual({Path(b['path']).name for b in checked},{'a.wav','b.wav','excerpt.json'})
        self.assertTrue(verify.call_args.kwargs['force'])
        updates=self.desk.queue_updates(queue['id'])
        self.assertEqual(updates['events'][0]['id'],event['id'])
        self.assertEqual(updates['events'][0]['notes'],'Clearer speech')
        self.assertNotEqual(updates['labels'][sample['id']],['Version A','Version B'])
        self.assertNotIn('bindings',updates)
        reopened=self.desk.public_queue(queue['id'])
        self.assertEqual(reopened['events'][0]['judgment'],'improved')
        source=Path(self.desk.item(sample['tape'],sample['channel'])['denoised'])/'denoised.wav'
        original=source.read_bytes()
        try:
            source.write_bytes(original+b'changed')
            # A listening observation remains recordable independently of current source freshness.
            self.desk.judge(dict(queue_id=queue['id'],sample_id=sample['id'],reviewer='tester',judgment='unchanged'))
            preview=self.desk.preview(dict(queue_id=queue['id'],kind='production',reviewer='tester'))
            self.assertTrue(any('Stale evidence' in error for error in preview['errors']))
        finally:source.write_bytes(original)

    def test_changed_playback_audio_cannot_be_saved_as_the_reviewed_excerpt(self):
        queue=self.make_queue(3);sample=queue['samples'][0]
        audio=self.desk.state/'queues'/queue['id']/sample['id']/'a.wav'
        audio.write_bytes(audio.read_bytes()+b'changed')
        with self.assertRaisesRegex(ValueError,'Stale evidence'):
            self.desk.judge(dict(queue_id=queue['id'],sample_id=sample['id'],reviewer='tester',judgment='improved'))
        self.assertFalse(self.desk.events(queue['id']))

    def test_findings_and_reopen_use_saved_events(self):
        queue=self.make_queue(3);sample=queue['samples'][0]
        saved=self.desk.judge(dict(queue_id=queue['id'],sample_id=sample['id'],reviewer='tester',
                                  judgment='improved',notes='Saved without a UI'))
        updates=self.desk.queue_updates(queue['id'])
        self.assertEqual(updates['events'][0]['id'],saved['id'])
        self.assertEqual(updates['events'][0]['notes'],'Saved without a UI')
        reopened=self.desk.public_queue(queue['id'])
        self.assertEqual(reopened['events'][0]['id'],saved['id'])
        self.assertNotEqual(reopened['samples'][0]['labels'],['Version A','Version B'])

    def test_identical_prepared_excerpts_require_no_listening_or_fabricated_judgment(self):
        original=np.load
        def identity_map(path,*args,**kwargs):
            value=original(path,*args,**kwargs)
            if Path(path).name=='nominal_seconds.npy':
                value=np.arange(len(value))/200
                value[-1]=(120*8000-1)/8000
            return value
        tape=self.desk.catalog['tapes'][0]['tape']
        with patch('review_evidence.np.load',side_effect=identity_map):
            q=self.desk.queue(self.desk.create_queue(dict(kind='production',tapes=[tape],budget=3,tape_random=True),lambda _:None)['queue_id'])
        self.assertEqual(q['automated_unchanged'],3)
        self.assertEqual(q['estimated_listening_seconds'],0)
        self.assertTrue(all(automatic_no_change(s) for s in q['samples']))
        self.assertTrue(assess(q,[])['complete'])
        self.assertEqual(self.desk.events(q['id']),[])
        # Exact equality is no substitute for methodology or numeric approval.
        preview=self.desk.preview(dict(queue_id=q['id'],kind='production',reviewer='tester'))
        self.assertEqual(preview['status'],'blocked')
        self.assertTrue(any('Methodology acceptance required' in e for e in preview['errors']))

    def test_excerpts_equal_slice_of_monolithic_production_map(self):
        queue=self.make_queue(5)
        sample=next(s for s in queue['samples'] if s['ready'])
        item=self.desk.item(sample['tape'],sample['channel'])
        review_path=self.desk.map_directory(item)/'review.json'
        nominal=np.load(review_path.parent/'nominal_seconds.npy')
        tt=np.arange(len(nominal))/200.;tt[-1]=(item['source']['frames']-1)/8000
        whole=self.root/'monolithic.wav'
        render_warp(Path(item['denoised'])/'denoised.wav',whole,tt,nominal,progress=lambda _:None)
        a,_=sf.read(whole,start=sample['start_frame'],frames=sample['frames'])
        excerpt=self.desk.state/'queues'/queue['id']/sample['id']/'b.wav'
        b,_=sf.read(excerpt)
        np.testing.assert_allclose(b,a*sample['excerpt']['gain_b'],atol=2e-7)
        self.assertNotEqual(sample['start_frame'],0)
        original=self.desk.item(sample['tape'],sample['channel'])['source']
        self.assertEqual(Path(original['path']).stat().st_mtime_ns,original['mtime_ns'])

    def test_preview_blocks_incomplete_listening_then_accepts_demo_lifecycle(self):
        queue=self.make_queue()
        payload=dict(queue_id=queue['id'],reviewer='Synthetic tester',kind='methodology')
        preview=self.desk.preview(payload)
        self.assertEqual(preview['status'],'blocked')
        with self.assertRaises(ValueError):self.desk.accept(preview['id'])
        self.judge_all(queue)
        preview=self.desk.preview(payload)
        self.assertEqual(preview['status'],'ready',preview['errors'])
        decision=self.desk.accept(preview['id'])
        self.assertFalse(decision['production_launched'])
        self.assertEqual(read_json(decision['certificate'])['kind'],'methodology')
        self.assertNotIn('cross_tape_pilots',read_json(self.desk.state/'methodology/rollout_approval.json'))
        with self.assertRaisesRegex(ValueError,'Synthetic'):
            require_rollout(self.desk.state/'methodology/rollout_approval.json')
        production=self.desk.preview(dict(payload,kind='production'))
        self.assertEqual(production['status'],'ready',production['errors'])
        decision=self.desk.accept(production['id'])
        acceptance=self.desk.state/'approvals'/queue['tapes'][0]/'production_acceptance.json'
        self.assertTrue(acceptance.exists())
        item=self.desk.item(queue['tapes'][0],2)
        with self.assertRaisesRegex(ValueError,'Synthetic'):
            require_production(acceptance,item['plan_path'],2)

    def test_adverse_finding_excludes_cohort_and_followup_is_small(self):
        queue=self.make_queue()
        self.judge_all(queue)
        sample=queue['samples'][0]
        self.desk.judge(dict(queue_id=queue['id'],sample_id=sample['id'],reviewer='tester',judgment='worse',flags={'transition_problems':True}))
        result=self.desk.preview(dict(queue_id=queue['id'],reviewer='tester',kind='methodology'))
        self.assertEqual(result['status'],'blocked')
        affected=[s for s in result['scope'] if sample['cohort'] in s['cohorts']]
        self.assertTrue(affected)
        self.assertTrue(all(not s['accepted'] for s in affected))
        followup=self.desk.selection(dict(budget=5),followup=queue['id'])
        self.assertLessEqual(len(followup['samples']),5)
        prior={(s['tape'],s['channel'],s['start_frame']) for s in queue['samples']}
        self.assertTrue(all((s['tape'],s['channel'],s['start_frame']) not in prior for s in followup['samples']))

    def test_failed_map_and_stale_audio_cannot_be_approved(self):
        queue=self.make_queue(5)
        sample=next(s for s in queue['samples'] if s['ready'])
        item=self.desk.item(sample['tape'],sample['channel'])
        folder=self.desk.map_directory(item)
        path=folder/'review.json';review=read_json(path)
        review['joined_map_checks']['status']='insufficient_reserved_events'
        path.write_text(json.dumps(review))
        preview=self.desk.preview(dict(queue_id=queue['id'],reviewer='tester',kind='production'))
        scope=next(s for s in preview['scope'] if s['tape']==sample['tape'])
        self.assertFalse(scope['accepted'])
        self.assertTrue(any('Final joined-map' in b for b in scope['blockers']))
        audio=Path(item['denoised'])/'denoised.wav'
        original=audio.read_bytes()
        try:
            audio.write_bytes(original+b'changed')
            preview=self.desk.preview(dict(queue_id=queue['id'],reviewer='tester',kind='production'))
            self.assertTrue(any('Stale evidence' in b for b in preview['errors']))
        finally:
            audio.write_bytes(original)

    def test_bounded_pilots_use_full_channel_map_and_stay_production_excluded(self):
        item=self.desk.catalog['channels'][0]
        source=Path(item['plan_path'])
        partial=read_json(source)
        partial['full_tape']=False
        partial['regions']=partial['regions'][1:2]
        audit=source.parent/'bounded_fixture.json'
        immutable_json(audit,partial)
        derived=self.desk.pilot_map_plan(audit,partial)
        plan=read_json(derived)
        self.assertTrue(plan['review_only'])
        self.assertEqual(plan['regions'][0]['start_seconds'],0)
        self.assertEqual(plan['regions'][-1]['end_seconds'],120)
        self.assertEqual(sum(r['end_seconds']-r['start_seconds'] for r in plan['regions']),120)
        from channel_review import prepare, continuous_map
        _,_,row,regions,start,_,frames=prepare(derived,2,0)
        tt,nominal,speed,*_=continuous_map(derived.parent,regions,2,start,frames,8000,progress=lambda _:None)
        self.assertAlmostEqual(nominal[-1],(frames-1)/8000)
        self.assertTrue(np.all(speed[tt<35]==0))
        self.assertTrue(np.all(speed[tt>85]==0))
        self.assertGreater(float(np.std(speed[(tt>45)&(tt<75)])),.001)
        with patch('flutter_policy.require_rollout'):
            acceptance=self.desk.state/'pilot_acceptance.json'
            immutable_json(acceptance,dict(rollout_approval='unused'))
            with self.assertRaisesRegex(ValueError,'pilot-only'):
                require_production(acceptance,derived,2)

    def proposal_pilot(self):
        item=self.desk.catalog['channels'][0]
        source=Path(item['plan_path'])
        plan=read_json(source)
        folder=source.parent/'audits'/self.id().split('.')[-1]
        folder.mkdir(parents=True)
        summary=plan['regions'][1]
        detail=read_json(source.parent/summary['detail_file'])
        detail['search_budget']=dict(exhausted=True,limit=4,eligible_candidates=100)
        for target in detail['targets']:
            target.update(action='validate_transfer',numeric_transfer_passed=False,
                          reason='Insufficient separate calibration and holdout events')
        shutil.copyfile(source.parent/detail['maps']['file'],folder/detail['maps']['file'])
        immutable_json(folder/summary['detail_file'],detail)
        plan.update(full_tape=False,regions=[dict(detail,detail_file=summary['detail_file'],
                                                detail_sha256=bound(folder/summary['detail_file'])['sha256'])])
        path=folder/'plan.json'
        immutable_json(path,plan)
        self.config['plan_overrides']={item['tape']:str(path)}
        self.desk.scan(lambda _:None)
        return self.desk.item(item['tape'],item['channel'])

    def test_diagnostic_pilot_plays_proposals_but_cannot_authorize_processing(self):
        item=self.proposal_pilot()
        options=dict(tapes=[item['tape']],budget=1,kind='methodology')
        blocked=self.desk.queue(self.desk.create_queue(options,lambda _:None)['queue_id'])
        self.assertEqual(blocked['samples'][0]['error'],'Reference search budget exhausted')
        options['kind']='diagnostic'
        queue=self.desk.queue(self.desk.create_queue(options,lambda _:None)['queue_id'])
        sample=queue['samples'][0]
        self.assertTrue(sample['ready'],sample.get('error'))
        self.assertTrue(queue['diagnostic_only'])
        self.assertTrue(sample['excerpt']['diagnostic_only'])
        self.assertNotEqual(sample['id'],blocked['samples'][0]['id'])
        folder=self.desk.map_directory(item,diagnostic=True)
        review=read_json(folder/'review.json')
        self.assertTrue(review['experimental_transfer'])
        self.assertEqual(review['joined_map_checks']['status'],'diagnostic_only')
        speed=np.load(folder/'speed_error.npy')
        self.assertGreater(float(np.max(abs(speed))),.001)
        self.assertFalse(self.desk.map_directory(item).exists())
        audio=self.desk.state/'queues'/queue['id']/sample['id']
        a,_=sf.read(audio/'a.wav'); b,_=sf.read(audio/'b.wav')
        self.assertGreater(float(np.max(abs(a-b))),.001)
        # Diagnostic excerpts still use the joined full-duration map, with
        # identity outside the trial and no local re-anchoring.
        nominal=np.load(folder/'nominal_seconds.npy')
        tt=np.arange(len(nominal))/200.;tt[-1]=(item['source']['frames']-1)/8000
        whole=folder/'test_monolithic.wav'
        render_warp(Path(item['denoised'])/'denoised.wav',whole,tt,nominal,progress=lambda _:None)
        rendered,_=sf.read(whole,start=sample['start_frame'],frames=sample['frames'])
        np.testing.assert_allclose(b,rendered*sample['excerpt']['gain_b'],atol=2e-7)
        self.judge_all(queue)
        self.assertTrue(self.desk.events(queue['id'])[0]['diagnostic_only'])
        # Matching implementation hashes do not turn explored passages into
        # fresh acceptance evidence.
        with patch.dict(item,blockers=[]):
            qualified=self.desk.queue(self.desk.create_queue(dict(options,kind='methodology'),lambda _:None)['queue_id'])
        preview=self.desk.preview(dict(queue_id=qualified['id'],reviewer='tester',kind='methodology'))
        self.assertIn('Fresh evaluation overlaps diagnostic pilot listening',preview['errors'])
        for kind in ('diagnostic','methodology','production'):
            with self.assertRaisesRegex(ValueError,'cannot authorize'):
                self.desk.preview(dict(queue_id=queue['id'],kind=kind,reviewer='tester'))
        from review_sampling import validate_map_review
        with self.assertRaisesRegex(ValueError,'conservative'):
            validate_map_review(folder/'review.json',item['plan_path'],item['channel'])
        with self.assertRaisesRegex(ValueError,'search budget exhausted'):
            self.desk.build_map(item,lambda _:None)

    def test_diagnostic_playback_preserves_input_and_clock_blockers(self):
        item=self.proposal_pilot()
        self.desk.build_map(item,lambda _:None,diagnostic=True)
        self.config['clock_conflicts']={item['tape']:True}
        self.desk.scan(lambda _:None)
        item=self.desk.item(item['tape'],item['channel'])
        with self.assertRaisesRegex(ValueError,'clock conflict'):
            self.desk.build_map(item,lambda _:None,diagnostic=True)
        self.config.pop('clock_conflicts')
        with patch('review_evidence.completed_denoised',return_value=False):
            self.desk.scan(lambda _:None)
        item=self.desk.item(item['tape'],item['channel'])
        with self.assertRaisesRegex(ValueError,'stage-03'):
            self.desk.build_map(item,lambda _:None,diagnostic=True)

    def test_diagnostic_selection_requires_bounded_pilot(self):
        self.assertEqual(self.desk.selection(dict(kind='diagnostic'))['samples'],[])
        item=self.desk.catalog['channels'][0]
        with self.assertRaisesRegex(ValueError,'bounded pilot'):
            self.desk.build_map(item,lambda _:None,diagnostic=True)

    def test_method_change_requires_fresh_nonoverlapping_evaluation(self):
        queue=self.make_queue(5)
        sample=queue['samples'][0]
        event=dict(id='old',queue_id='old',sample_id='old',reviewer='tester',reviewed_at='2026-10-20',
                   judgment='improved',flags={},cohorts=[sample['cohort']],coordinates=sample['provenance'],implementation_sha256='previous-method')
        immutable_json(self.desk.state/'events'/'old.json',event)
        preview=self.desk.preview(dict(queue_id=queue['id'],reviewer='tester',kind='methodology'))
        self.assertTrue(any('overlaps a holdout' in e for e in preview['errors']))

    def test_filesystem_roots(self):
        with self.assertRaisesRegex(ValueError,'outside configured'):
            self.desk.guard.check(self.root/'outside.json')

    def test_existing_production_gate_dispatches_sampling_and_scopes_channels(self):
        queue=self.make_queue()
        self.judge_all(queue)
        # Build a non-demo gate fixture to isolate dispatch, scope and stale checks.
        # Mission evidence is not generated or approved by this test.
        item=self.desk.catalog['channels'][0]
        cert=dict(status='accepted',kind='production',policy_version=POLICY,sampling_policy_sha256=policy_hash(),
                  demo=False,reviewer='test',reviewed_at='2026-10-24',implementation_sha256=implementation_digest(),policy_sha256=policy_digest(),
                  bindings=queue['bindings'],queue=bound(self.desk.state/'queues'/queue['id']/'queue.json'),
                  events=[bound(e['event_path']) for e in self.desk.events(queue['id'])],ledger_root=str(self.desk.state/'events'),
                  scope=[dict(plan_path=item['plan_path'],plan_sha256=item['plan_sha256'],channel=item['channel'],accepted=True,
                              cohorts=item['cohorts'],required_conditions=item['required_conditions'],
                              continuous_review=bound(self.desk.map_directory(item)/'review.json'))])
        certificate=self.desk.state/'certificate.json';immutable_json(certificate,cert)
        acceptance=self.desk.state/'production_acceptance.json'
        immutable_json(acceptance,dict(status='accepted',rollout_approval='unused.json',plan_sha256=item['plan_sha256'],
                                      acceptance_policy=POLICY,sampling_certificate=bound(certificate)))
        original_loads=json.loads
        with patch('flutter_policy.require_rollout'),patch('flutter_policy.json.loads',wraps=original_loads) as loads:
            # Only remove the plan's explicit demo marker for this isolated dispatch fixture.
            def parse(raw,*args,**kwargs):
                value=original_loads(raw,*args,**kwargs)
                if isinstance(value,dict) and value.get('schema')==1 and 'inventory' in value:value.pop('demo',None)
                return value
            loads.side_effect=parse
            self.assertEqual(require_production(acceptance,item['plan_path'],2)['acceptance_policy'],POLICY)
            with self.assertRaisesRegex(ValueError,'excluded'):
                require_production(acceptance,item['plan_path'],3)
            sample=next(s for s in queue['samples'] if s['cohort'] in item['cohorts'])
            self.desk.judge(dict(queue_id=queue['id'],sample_id=sample['id'],reviewer='tester',judgment='worse'))
            with self.assertRaisesRegex(ValueError,'holdout'):
                require_production(acceptance,item['plan_path'],2)


if __name__=='__main__':unittest.main()
