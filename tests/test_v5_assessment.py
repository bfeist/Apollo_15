"""Final assessment does not count identity or duplicated checks as success."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from unittest.mock import patch
import numpy as np
import v5_assessment as assessment


def test_identity_without_measurement_is_insufficient(tmp_path):
    (tmp_path/'plan.json').write_text('{}')
    row = dict(channel=2, start_seconds=0., end_seconds=10., sample_rate=8000)
    summary = dict(start_seconds=0., end_seconds=10.)
    plan = dict(methodology='test', implementation_sha256='test', regions=[summary], inventory=dict(channels=[row]))
    record = dict(targets=[dict(channel=2, reference_spans=[dict(start_seconds=0., end_seconds=10., selected=None, reason='No evidence')])])
    tt = np.arange(2000)/200
    with patch.object(assessment, 'load_saved_plan', return_value=plan), \
         patch.object(assessment, 'detail', return_value=record), \
         patch.object(assessment, 'saved_continuous_map', return_value=(tt, tt, tt*0, {}, [], [])), \
         patch.object(assessment, 'continuous_holdout_checks', return_value=dict(reserved_events=[], no_motion=True, status='passed')):
        report = assessment.assess_plan(tmp_path)
    assert report['channels'][0]['status'] == 'insufficient_evidence'
    assert report['spans_with_reserved_improvement_channel_seconds'] == 0


def test_saved_v5_survey_tampering_is_rejected(tmp_path):
    import hashlib
    import pytest
    from flutter_plan_store import checked_v5_survey
    folder = tmp_path/'plan'; folder.mkdir()
    for name in ('survey_manifest.json','survey.jsonl'):
        (tmp_path/name).write_text('frozen')
    signature = {key:hashlib.sha256(b'frozen').hexdigest() for key in ('manifest_sha256','coverage_sha256')}
    plan = dict(methodology='continuous-allchannel-v5', survey_signature=signature)
    assert len(list(checked_v5_survey(folder, plan))) == 2
    (tmp_path/'survey.jsonl').write_text('altered')
    with pytest.raises(ValueError, match='changed'):
        list(checked_v5_survey(folder, plan))
