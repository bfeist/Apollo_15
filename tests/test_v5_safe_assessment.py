"""A rejected map remains explicit while other channels finish assessment."""
import json
from contextlib import ExitStack
from pathlib import Path
import sys
from unittest.mock import patch

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from _bootstrap import install as _install
_install()
import v5_safe_assessment as assessment
import v5_assessment as legacy


def fixture(tmp_path):
    (tmp_path/'plan.json').write_text('{}')
    rows=[dict(channel=ch,start_seconds=0.,end_seconds=10.,sample_rate=8000) for ch in (2,22)]
    summary=dict(start_seconds=0.,end_seconds=10.)
    plan=dict(methodology='continuous-allchannel-v5',implementation_sha256='unchanged',
        regions=[summary],inventory=dict(channels=rows))
    record=dict(targets=[dict(channel=ch,reference_spans=[dict(start_seconds=0.,end_seconds=10.,
        selected='reference',reason='Previously validated reference')]) for ch in (2,22)])
    return plan,record


def test_unsafe_map_is_reported_and_other_channels_are_assessed(tmp_path):
    plan,record=fixture(tmp_path)
    def map_for_channel(folder,summary,channel,*args):
        if channel==22:
            raise ValueError('Continuous duration anchoring exceeds speed bound')
        tt=np.arange(2000)/200
        return tt,tt,tt*0,{},[],[]
    with patch.object(assessment,'load_saved_plan',return_value=plan), \
         patch.object(assessment,'detail',return_value=record), \
         patch.object(assessment,'saved_continuous_map',side_effect=map_for_channel), \
         patch.object(assessment,'continuous_holdout_checks',return_value=dict(
             reserved_events=[],no_motion=True,status='passed')) as checks:
        report=assessment.assess_plan(tmp_path,tmp_path/'assessment.json')
    assert report['assessment_status']=='completed_with_invalid_maps'
    assert report['invalid_map_channels']==[22]
    assert checks.call_count==1
    channel=report['channels'][1]
    assert channel['status']=='invalid_map'
    assert channel['recommended_action']=='leave_uncorrected'
    assert channel['spans'][0]['outcome']=='rejected_invalid_map'
    assert channel['spans_with_reserved_improvement_seconds']==0
    assert channel['map_integrity']['monotonic'] is None
    assert report['maps_refitted_after_final'] is False
    assert report['production_accepted'] is False
    assert json.loads((tmp_path/'assessment.json').read_text())['invalid_map_channels']==[22]


def test_unexpected_errors_and_corrupt_artifacts_remain_fatal(tmp_path):
    plan,record=fixture(tmp_path)
    with patch.object(assessment,'load_saved_plan',return_value=plan), \
         patch.object(assessment,'detail',return_value=record), \
         patch.object(assessment,'saved_continuous_map',side_effect=ValueError('Continuous map stream changed')):
        with pytest.raises(ValueError,match='stream changed'):
            assessment.assess_plan(tmp_path)
    with patch.object(assessment,'load_saved_plan',side_effect=ValueError('Saved plan artifact changed')):
        with pytest.raises(ValueError,match='artifact changed'):
            assessment.assess_plan(tmp_path)


def test_valid_map_assessments_match_the_frozen_evaluator(tmp_path):
    plan,record=fixture(tmp_path)
    tt=np.arange(2000)/200
    reports=[]
    for module in (legacy,assessment):
        with ExitStack() as stack:
            for name,value in dict(load_saved_plan=plan,detail=record,
                saved_continuous_map=(tt,tt,tt*0,{},[],[]),
                continuous_holdout_checks=dict(reserved_events=[],no_motion=True,status='passed')).items():
                stack.enter_context(patch.object(module,name,return_value=value))
            reports.append(module.assess_plan(tmp_path))
    for key in ('assessment_implementation_sha256','assessment_complete','assessment_status','invalid_map_channels'):
        reports[1].pop(key)
    assert reports[0]==reports[1]
