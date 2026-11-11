"""Shared evidence must enable correction on channels without local check tones."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
import numpy as np

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
from _bootstrap import install as _install
_install()
from tape_shared_plan import regional_choice, build_shared_plan, digest
from channel_review import continuous_map
from test_channel_review import fixture


class TapeSharedPlanTests(unittest.TestCase):
    def test_selection_does_not_vote_by_channel_count_or_order(self):
        r=dict(targets=[dict(channel=20,action='leave_uncorrected',selected=None),
                        dict(channel=17,action='render_and_review',selected='better'),
                        dict(channel=14,action='render_and_review',selected='worse')],
               variants=[dict(key='better',reference_rank=.9,tracking=dict(support=1)),
                         dict(key='worse',reference_rank=.2,tracking=dict(support=1))])
        self.assertEqual(regional_choice(r)[0],'better')
        r['targets'] += [dict(channel=n,action='render_and_review',selected='worse') for n in range(2,10)]
        r['targets'].reverse()
        self.assertEqual(regional_choice(r)[0],'better')
        self.assertEqual(regional_choice(r,'worse')[0],'worse')

    def test_missing_evidence_is_not_promoted_by_preference(self):
        r=dict(targets=[dict(channel=20,action='validate_transfer',selected='x')])
        self.assertEqual(regional_choice(r,'x'),(None,[]))

    def test_evidence_on_one_track_produces_identical_maps_on_both(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            path,plan,row=fixture(root,actions=['render_and_review','leave_uncorrected','render_and_review'])
            other=dict(row,channel=20)
            plan['inventory']['channels'].append(other)
            for summary in plan['regions']:
                detail=json.loads((path.parent/summary['detail_file']).read_text())
                detail['targets'].append(dict(channel=20,action='leave_uncorrected',selected=None,reason='No check tones'))
                (path.parent/summary['detail_file']).write_text(json.dumps(detail))
                summary['targets']=detail['targets']
                summary['detail_sha256']=digest(path.parent/summary['detail_file'])
            path.write_text(json.dumps(plan))
            cp=root/'continuous';cp.mkdir()
            detail=dict(id='continuous',kind='continuous_reference',status='analyzed',
                start_seconds=0,end_seconds=60,analysis_start=0,analysis_end=60,
                targets=[],variants=[],maps=dict(rate=200,streams={}))
            (cp/'detail.json').write_text(json.dumps(detail))
            summary=dict(detail,detail_file='detail.json',detail_sha256=digest(cp/'detail.json'))
            cplan=dict(schema=2,kind='continuous_reference',complete=True,source_validation='verified',
                       inventory=plan['inventory'],regions=[summary])
            (cp/'plan.json').write_text(json.dumps(cplan))
            (cp/'assessment.json').write_text(json.dumps(dict(plan_sha256=digest(cp/'plan.json'),channels=[])))
            shared,decisions=build_shared_plan(path,cp/'plan.json',cp/'assessment.json',root/'shared',0,60)
            maps=[continuous_map(root/'shared',shared['regions'],ch,0,row['frames'],8000,progress=lambda _:None)
                  for ch in (2,20)]
            np.testing.assert_array_equal(maps[0][1],maps[1][1])
            np.testing.assert_array_equal(maps[0][2],maps[1][2])
            tt,speed=maps[1][0],maps[1][2]
            self.assertGreater(np.std(speed[(tt>5)&(tt<15)]),.005)
            self.assertTrue(np.all(speed[(tt>21)&(tt<39)]==0))
            self.assertTrue(np.all(np.diff(maps[1][1])>0))
            self.assertEqual(sum(d['end_seconds']-d['start_seconds'] for d in decisions if d['selected']),40)

            # Existing continuous evidence fills a regional gap even when the
            # destination track has no check events. Explicit failed evidence cannot.
            times=np.arange(60*200)/200
            data=np.column_stack((.01*np.sin(2*np.pi*6.5*times),np.ones(len(times))))
            data.astype('<f8').tofile(cp/'stream.f64')
            detail['maps']['streams']={'s0':dict(file='stream.f64',sha256=digest(cp/'stream.f64'),
                frames=len(times),start_seconds=0,dtype='<f8')}
            detail['variants']=[dict(key='s0',rank=.9)]
            detail['targets']=[dict(channel=2,reference_spans=[dict(start_seconds=20,end_seconds=30,
                action='use_reference',selected='s0')])]
            (cp/'detail.json').write_text(json.dumps(detail))
            cplan['regions']=[dict(detail,detail_file='detail.json',detail_sha256=digest(cp/'detail.json'))]
            (cp/'plan.json').write_text(json.dumps(cplan))
            assessment=dict(plan_sha256=digest(cp/'plan.json'),channels=[dict(channel=2,spans=[
                dict(start_seconds=20,end_seconds=30,outcome='insufficient_evidence',event_metrics=[])])])
            (cp/'assessment.json').write_text(json.dumps(assessment))
            combined,decisions=build_shared_plan(path,cp/'plan.json',cp/'assessment.json',root/'combined',0,60)
            combined_maps=[continuous_map(root/'combined',combined['regions'],ch,0,row['frames'],8000,progress=lambda _:None)
                           for ch in (2,20)]
            np.testing.assert_array_equal(combined_maps[0][1],combined_maps[1][1])
            self.assertGreater(np.std(combined_maps[1][2][(tt>22)&(tt<28)]),.005)
            self.assertEqual(sum(d['end_seconds']-d['start_seconds'] for d in decisions if d['selected']),50)
            assessment['channels'][0]['spans'][0]['event_metrics']=[dict(status='failed')]
            (cp/'assessment.json').write_text(json.dumps(assessment))
            _,decisions=build_shared_plan(path,cp/'plan.json',cp/'assessment.json',root/'failed_excluded',0,60)
            self.assertEqual(sum(d['end_seconds']-d['start_seconds'] for d in decisions if d['selected']),40)


if __name__=='__main__':
    unittest.main()
