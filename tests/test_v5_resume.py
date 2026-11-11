import json
from pathlib import Path
import sys

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from _bootstrap import install as _install
_install()
import v5_allchannel as engine
import v5_resume as store


@pytest.fixture
def survey(tmp_path, monkeypatch):
    source = tmp_path/'source'
    source.mkdir()
    inv = dict(tape='A15_TEST', channels=[dict(channel=1, start_seconds=0., end_seconds=65., sample_rate=1000)],
        missing_channels=list(range(2,31)), duration_seconds=65., origin_filename_seconds=0)
    monkeypatch.setattr(engine, 'inventory', lambda _:inv)
    monkeypatch.setattr(store, 'inventory', lambda _:inv)
    calls = []
    def read(row, low, high):
        calls.append((low,high))
        return np.ones(round((high-low)*1000)), 1000, 'hash'
    monkeypatch.setattr(engine, 'read_channel', read)
    monkeypatch.setattr(engine, 'discover', lambda *args:[])
    return source, calls


def test_resume_has_identical_coverage_and_candidates_and_discards_uncommitted_tails(tmp_path, survey):
    source,calls = survey
    expected = engine.survey_tape(source,tmp_path/'expected',monitor_seconds=20.)
    partial = store.survey_tape(source,tmp_path/'resumed',monitor_seconds=20.,max_windows=2)
    assert not partial['complete']
    calls.clear()
    for name in ('survey.jsonl',store.JOURNAL):
        with (tmp_path/'resumed'/name).open('ab') as stream:
            stream.write(b'power loss left an incomplete tail')
    actual = store.survey_tape(source,tmp_path/'resumed',monitor_seconds=20.,resume=True)
    assert actual == expected
    assert (tmp_path/'resumed'/'survey.jsonl').read_bytes() == (tmp_path/'expected'/'survey.jsonl').read_bytes()
    assert len(calls) == 2
    assert calls[0][0] == 36.


def test_changed_settings_and_committed_bytes_refuse_resume(tmp_path, survey):
    source,_ = survey
    output=tmp_path/'out'
    store.survey_tape(source,output,monitor_seconds=20.,max_windows=1)
    with pytest.raises(ValueError,match='settings'):
        store.survey_tape(source,output,monitor_seconds=21.,resume=True)
    data=(output/'survey.jsonl').read_bytes()
    (output/'survey.jsonl').write_bytes(b'X'+data[1:])
    with pytest.raises(ValueError,match='changed'):
        store.survey_tape(source,output,monitor_seconds=20.,resume=True)
    assert (output/'survey.jsonl').read_bytes().startswith(b'X')


def test_final_checkpoint_recovers_missing_manifest_without_audio_reads(tmp_path,survey):
    source,calls=survey
    output=tmp_path/'out'
    expected=store.survey_tape(source,output,monitor_seconds=20.)
    (output/'survey_manifest.json').unlink()
    calls.clear()
    actual=store.survey_tape(source,output,monitor_seconds=20.,resume=True)
    assert actual==expected
    assert calls==[]


def test_import_live_legacy_snapshot_preserves_complete_windows(tmp_path,survey):
    source,calls=survey
    output=tmp_path/'out'
    expected=engine.survey_tape(source,output,monitor_seconds=20.)
    raw=(output/'survey.jsonl').read_bytes()
    (output/'survey_manifest.json').unlink()
    snapshot=dict(inv=expected['inventory'],unassigned=[],start=0.,end=65.,source_end=65.,
        monitor_seconds=20.,context_seconds=4.,candidates_per_channel=8,denoised_root=None,
        windows=expected['windows'][:2],cursor=40.)
    (output/'survey_live_snapshot.json').write_text(json.dumps(snapshot))
    state=store.import_live_snapshot(source,output,monitor_seconds=20.)
    assert state['cursor']==40.
    calls.clear()
    actual=store.survey_tape(source,output,monitor_seconds=20.,resume=True)
    assert actual==expected
    assert (output/'survey.jsonl').read_bytes()==raw
    assert len(calls)==2


def test_planner_recovery_trims_only_verified_uncommitted_stream_data(tmp_path):
    folder=tmp_path/'plan'
    folder.mkdir()
    committed=b'0123456789abcdef'
    stream=folder/'motion.bin'
    stream.write_bytes(committed)
    descriptor=dict(file=stream.name,frames=1,sha256=store.digest(stream))
    state=dict(sessions=[dict(key='reference',descriptor=descriptor)])
    state_path=folder/'checkpoint.json'
    state_path.write_text(json.dumps(state))
    plan=dict(complete=False,checkpoint_file=state_path.name,checkpoint_sha256=store.digest(state_path),
        regions=[dict(maps=dict(streams=dict(reference=descriptor)))])
    (folder/'plan.json').write_text(json.dumps(plan))
    stream.write_bytes(committed+b'part of an uncommitted window')
    assert store.repair_planner_tails(tmp_path)==[str(stream.resolve())]
    assert stream.read_bytes()==committed
    stream.write_bytes(b'X'+committed[1:]+b'uncommitted')
    with pytest.raises(ValueError,match='Committed planning stream changed'):
        store.repair_planner_tails(tmp_path)
    assert stream.read_bytes().endswith(b'uncommitted')


def test_preexisting_v5_planning_checkpoint_resumes_with_same_engine_signature(tmp_path):
    from test_continuous_planning import fixture
    source,_=fixture(tmp_path,duration=120)
    partial=engine.plan_tape(source,tmp_path/'v5',max_windows=1)
    assert not partial['complete']
    resumed=store.plan_tape(source,tmp_path/'v5',resume=True)
    assert resumed['complete']
    assert resumed['implementation_sha256']==partial['implementation_sha256']
