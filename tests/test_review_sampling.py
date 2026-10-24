"""Representative listening, scope and the actual CLI production gate."""
from copy import deepcopy
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import numpy as np
import soundfile as sf

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from review_sampling import (POLICY, PILOTS, select_samples, assess, fingerprint,
                             immutable_json, read_json, bound, verify_bindings, policy_hash)
from review_demo import make_demo
from deflutter_review import Desk, Handler
from flutter_policy import require_production, require_rollout, implementation_digest, policy_digest
from channel_review import render_warp


def candidates(n=80):
    return [dict(id=str(i).zfill(3),tape='A15_T510083',channel=2,start_frame=i*160000,
                 frames=160000,sample_rate=8000,conditions=['early' if i%3 == 0 else 'middle' if i%3 == 1 else 'late',f'cohort:{i%4}'],
                 cohort=str(i%4),eligible=True,risk=4 if i<15 else 0) for i in range(n)]


class SelectionTests(unittest.TestCase):
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

    def test_demo_inventory_covers_pilots_and_new_tape(self):
        self.assertEqual(len(self.desk.catalog['tapes']),6)
        selection=self.desk.selection({})
        self.assertEqual(selection['uncovered_conditions'],[])
        self.assertTrue(set(PILOTS).issubset(selection['tapes']))
        self.assertIn('A15_T599999',selection['tapes'])
        self.assertTrue(all(t['denoised_channels']==1 for t in self.desk.catalog['tapes']))

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

    def test_method_change_requires_fresh_nonoverlapping_evaluation(self):
        queue=self.make_queue(5)
        sample=queue['samples'][0]
        event=dict(id='old',queue_id='old',sample_id='old',reviewer='tester',reviewed_at='2026-10-20',
                   judgment='improved',flags={},cohorts=[sample['cohort']],coordinates=sample['provenance'],implementation_sha256='previous-method')
        immutable_json(self.desk.state/'events'/'old.json',event)
        preview=self.desk.preview(dict(queue_id=queue['id'],reviewer='tester',kind='methodology'))
        self.assertTrue(any('overlaps a holdout' in e for e in preview['errors']))

    def test_filesystem_roots_and_host_csrf_controls(self):
        with self.assertRaisesRegex(ValueError,'outside configured'):
            self.desk.guard.check(self.root/'outside.json')
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler);server.desk=self.desk
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
        base=f'http://127.0.0.1:{server.server_port}'
        with urlopen(base) as response:
            self.assertEqual(response.status,200)
            self.assertIn(b'Deflutter evidence desk',response.read())
        with urlopen(base+'/api/overview') as response:self.assertIn('tapes',json.load(response))
        for headers in ({},{'X-Review-Token':self.desk.token,'Origin':'https://other.example'}):
            with self.assertRaises(HTTPError):urlopen(Request(base+'/api/scan',data=b'{}',headers=headers))
        with self.assertRaises(HTTPError):urlopen(Request(base+'/api/overview',headers={'Host':'attacker.example'}))
        with self.assertRaises(HTTPError):urlopen(base+'/../flutter_policy.py')

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
