import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from _bootstrap import install as _install
_install()
import json
import numpy as np
import v5_allchannel as v5


def test_survey_accounts_for_missing_unavailable_and_read_errors(tmp_path, monkeypatch):
    rows = [dict(channel=1, start_seconds=0., end_seconds=25., sample_rate=1000),
            dict(channel=2, start_seconds=3., end_seconds=22., sample_rate=1000)]
    monkeypatch.setattr(v5, 'inventory', lambda _: dict(channels=rows, missing_channels=[3], duration_seconds=25.))
    def read(row, a, b):
        if row['channel'] == 2:
            raise OSError('unreadable')
        return np.ones(round((b-a)*1000)), 1000, 'hash'
    monkeypatch.setattr(v5, 'read_channel', read)
    monkeypatch.setattr(v5, 'discover', lambda x, sr, limit: [])
    result = v5.survey_tape('source', tmp_path/'survey', start=.5, duration=24., monitor_seconds=20.)
    cells = [json.loads(line) for line in (tmp_path/'survey'/'survey.jsonl').read_text().splitlines()]
    assert result['complete'] and len(cells) == 3 * 26
    assert sum(c['end_seconds']-c['start_seconds'] for c in cells if c['channel'] == 1) == 24
    assert any(c['status'] == 'unavailable' for c in cells if c['channel'] == 2)
    assert any(c['status'] == 'read_error' for c in cells if c['channel'] == 2)
    assert all(c['status'] == 'missing_channel' for c in cells if c['channel'] == 3)
    assert cells[0]['context_start'] == 0


def test_fresh_destination_is_enforced(tmp_path):
    import pytest
    with pytest.raises(ValueError, match='checkpoint'):
        v5.plan_tape(tmp_path/'source', tmp_path/'out', resume=True)

def test_known_distinct_motion_and_dropout_are_not_shared_evidence():
    from flutter_tracking import trajectory, signed_map, RATE
    sr, duration = 6000, 24
    t = np.arange(sr * duration) / sr
    motion_a = .0005 * np.sin(2*np.pi*5*t)
    motion_b = .0005 * np.sin(2*np.pi*7*t+.4)
    # A louder stationary common line and a dropout on one carrier.
    a = .4*np.cos(2*np.pi*500*(t+motion_a)) + .8*np.cos(2*np.pi*120*t)
    b = .4*np.cos(2*np.pi*700*(t+motion_b)) + .8*np.cos(2*np.pi*120*t)
    a[(t >= 10) & (t < 12)] = 0
    ta, tb = trajectory(a, sr, 500, 50), trajectory(b, sr, 700, 50)
    ma, mb = signed_map(ta), signed_map(tb)
    tt = np.arange(len(ma))/RATE
    valid = (tt > 3) & (tt < 20) & ((tt < 9) | (tt > 13))
    expected = np.cos(2*np.pi*5*tt)
    assert abs(np.corrcoef(ma[valid], expected[valid])[0, 1]) > .8
    assert abs(np.corrcoef(ma[valid], mb[valid])[0, 1]) < .2
    assert np.mean(ta[5][(tt > 10.3) & (tt < 11.7)]) < .1



def test_v5_joined_map_known_motion_and_resume(tmp_path):
    from test_continuous_planning import fixture
    from channel_review import prepare, continuous_map, joined_map_checks, render_warp
    from flutter_tracking import burst_track
    import soundfile as sf
    source, ch = fixture(tmp_path, duration=120)
    partial = v5.plan_tape(source, tmp_path/'v5', max_windows=1)
    assert not partial['complete']
    plan = v5.plan_tape(source, tmp_path/'v5', resume=True)
    assert plan['complete']
    p, _, row, regions, start, _, frames = prepare(tmp_path/'v5'/'plan', ch, 0)
    result = continuous_map(p.parent, regions, ch, start, frames, row['sample_rate'])
    checks = joined_map_checks(p.parent, regions, row, ch, start, *result[:3])
    assert checks['status'] == 'passed'
    tt, nominal = result[:2]
    rendered = tmp_path/'rendered.wav'
    render_warp(Path(row['path']), rendered, tt, nominal, progress=lambda _: None)
    before, _ = sf.read(row['path']); after, _ = sf.read(rendered)
    _, fb = burst_track(before, 8000); _, fa = burst_track(after, 8000)
    ratios = [np.std(fa[int((sec+.06)*1000):int((sec+.19)*1000)]) /
              np.std(fb[int((sec+.06)*1000):int((sec+.19)*1000)]) for sec in (25,65,105)]
    assert max(ratios) < .15

def test_v5_switch_rejects_wrong_adjacent_motion_and_joins_safely(tmp_path):
    from test_continuous_planning import fixture
    from channel_review import prepare, continuous_map, joined_map_checks, render_warp
    from flutter_tracking import burst_track
    import soundfile as sf
    source, ch = fixture(tmp_path, deterioration=True)
    plan = v5.plan_tape(source, tmp_path/'v5')
    spans = plan['regions'][0]['targets'][0]['reference_spans']
    used = [s for s in spans if s['selected']]
    assert [s['reference_channel'] for s in used] == [1, 2]
    assert any('deterioration' in s['reason'] and not s['selected'] for s in spans)
    p, _, row, regions, start, _, frames = prepare(tmp_path/'v5'/'plan', ch, 0)
    result = continuous_map(p.parent, regions, ch, start, frames, row['sample_rate'])
    checks = joined_map_checks(p.parent, regions, row, ch, start, *result[:3])
    assert checks['status'] == 'passed'
    tt, nominal, speed = result[:3]
    assert np.all(np.diff(nominal) > 0)
    assert np.all(speed[(tt > 80) & (tt < 119)] == 0)
    rendered = tmp_path/'switch.wav'
    render_warp(Path(row['path']), rendered, tt, nominal, progress=lambda _: None)
    before, _ = sf.read(row['path']); after, _ = sf.read(rendered)
    _, fb = burst_track(before, 8000); _, fa = burst_track(after, 8000)
    for sec in (25, 145, 165):
        sl = slice(int((sec+.06)*1000), int((sec+.19)*1000))
        assert np.std(fa[sl]) / np.std(fb[sl]) < .15

def test_v5_dropout_does_not_bridge_and_reacquires(tmp_path):
    from test_continuous_planning import fixture
    from channel_review import prepare, continuous_map, joined_map_checks
    source, ch = fixture(tmp_path, dropout=(75., 78.))
    plan = v5.plan_tape(source, tmp_path/'v5')
    spans = plan['regions'][0]['targets'][0]['reference_spans']
    used = [s for s in spans if s['selected']]
    assert len(used) == 2 and used[0]['selected'] != used[1]['selected']
    assert used[0]['end_seconds'] < 75 and used[1]['start_seconds'] >= 120
    p, _, row, regions, start, _, frames = prepare(tmp_path/'v5'/'plan', ch, 0)
    result = continuous_map(p.parent, regions, ch, start, frames, row['sample_rate'])
    tt, nominal, speed = result[:3]
    assert np.all(speed[(tt > 80) & (tt < 119)] == 0)
    assert np.all(np.diff(nominal) > 0)
    assert joined_map_checks(p.parent, regions, row, ch, start, *result[:3])['status'] == 'passed'

def test_clean_discovery_union_and_mains_exclusion(tmp_path, monkeypatch):
    import soundfile as sf
    source = tmp_path/'source.wav'; clean_root = tmp_path/'den'; folder = clean_root/'CH01'
    folder.mkdir(parents=True)
    sr = 6000; x = np.full(12*sr, .1)
    sf.write(source, x, sr, subtype='FLOAT'); sf.write(folder/'denoised.wav', x*2, sr, subtype='FLOAT')
    stat = source.stat()
    row = dict(channel=1, path=str(source), bytes=stat.st_size, mtime_ns=stat.st_mtime_ns,
               frames=len(x), sample_rate=sr, start_seconds=0., end_seconds=12.)
    metadata = dict(status='complete', signature=dict(source={k:row[k] for k in
        ('path','bytes','mtime_ns','frames','sample_rate','channel')}, offset_frames=0, frames=len(x)))
    (folder/'denoised.json').write_text(json.dumps(metadata))
    monkeypatch.setattr(v5, 'inventory', lambda _: dict(channels=[row], missing_channels=[], duration_seconds=12.))
    monkeypatch.setattr(v5, 'discover', lambda data, sr, limit: [dict(carrier_hz=120., contrast_db=20., occupancy=1., spectral_score_db=20., near_mains_family=True)] if np.mean(data)<.15 else [dict(carrier_hz=777., contrast_db=20., occupancy=1., spectral_score_db=20., near_mains_family=False)])
    result = v5.survey_tape('source', tmp_path/'out', monitor_seconds=20, denoised_root=clean_root)
    candidates = result['windows'][0]['channels']['1']['candidates']
    assert candidates[0]['tracking_status'] == 'excluded_digitization_mains'
    assert candidates[1]['provenance'] == ['denoised']
    assert result['windows'][0]['channels']['1']['discovery_sources']['denoised']['status'] == 'analyzed'

def test_provider_uses_only_completed_windows_and_first_measurement():
    def row(start, end, support, hz=777.):
        return dict(start_seconds=start, end_seconds=end, channels={'1': {'candidates': [dict(carrier_hz=hz, tracking_status='measured', support_fraction=support)]}})
    manifest = dict(windows=[row(0,60,.2), row(60,120,.99), row(120,180,.8, 888.)])
    result = v5.surveyed_candidates(manifest, 1, 0, 130)
    assert len(result) == 1 and result[0]['support_fraction'] == .2
    assert not v5.surveyed_candidates(manifest, 1, 0, 59)


def test_unassigned_source_is_surveyed(tmp_path, monkeypatch):
    import soundfile as sf
    source = tmp_path/'A15_T001_HR1U__207-00-00-00_207-00-00-12.wav'
    sf.write(source, np.ones(12*6000)*.1, 6000, subtype='FLOAT')
    stat = source.stat()
    inv = dict(channels=[], missing_channels=[1], duration_seconds=12., origin_filename_seconds=207*86400,
        excluded_files=[dict(path=str(source), bytes=stat.st_size, mtime_ns=stat.st_mtime_ns, reason='unassigned')])
    monkeypatch.setattr(v5, 'inventory', lambda _: inv)
    monkeypatch.setattr(v5, 'discover', lambda x, sr, limit: [])
    result = v5.survey_tape('source', tmp_path/'out', monitor_seconds=20)
    assert result['all_recorded_sources_covered']
    cells = [json.loads(line) for line in (tmp_path/'out'/'survey.jsonl').read_text().splitlines()]
    assert len([c for c in cells if c['channel'] == 'unassigned_1' and c['status'] == 'analyzed']) == 12

def test_synchronized_roles_survive_missing_detections_and_resume():
    from continuous_acquisition import SynchronizedEvidence
    ev = lambda t: dict(start=t, end=t+.2)
    ledger = SynchronizedEvidence()
    first = ledger.ingest_batch([(1, [ev(t) for t in (1, 5, 9, 13, 17)], 0),
                                 (2, [ev(t+.1) for t in (1, 9, 17)], 0)])
    assert [e['role'] for e in first[1]] == ['calibration','calibration','validation','validation','final']
    assert [e['role'] for e in first[2]] == ['calibration','validation','final']
    restored = SynchronizedEvidence.restore(ledger.state())
    second = restored.ingest_batch([(1, [ev(21)], 0), (2, [ev(21.1)], 0)])
    assert second[1][0]['role'] == second[2][0]['role'] == 'calibration'
    assert second[1][0]['synchronized_event_index'] == second[2][0]['synchronized_event_index']

def test_short_tail_gets_available_context(tmp_path, monkeypatch):
    row = dict(channel=1, start_seconds=0., end_seconds=61., sample_rate=1000)
    monkeypatch.setattr(v5, 'inventory', lambda _: dict(channels=[row], missing_channels=[], duration_seconds=61.))
    monkeypatch.setattr(v5, 'read_channel', lambda r,a,b: (np.ones(round((b-a)*1000)),1000,'hash'))
    monkeypatch.setattr(v5, 'discover', lambda x,sr,limit: [])
    result = v5.survey_tape('source', tmp_path/'out', monitor_seconds=60)
    tail = result['windows'][-1]['channels']['1']
    assert tail['status'] == 'analyzed' and tail['context_end']-tail['context_start'] >= 10
    assert result['all_recorded_sources_covered']

def test_v5_planner_local_data_expands_tail_context(tmp_path, monkeypatch):
    import continuous_planning as cp
    row = dict(channel=1, start_seconds=0., end_seconds=61.)
    monkeypatch.setattr(cp, 'read_channel', lambda row, a, b: (np.ones(1000), 1000, 'hash'))
    data = cp.local_data(dict(channels=[row]), 56, 61, tolerate_errors=True)
    assert data[1][2:] == (51., 61.)
    assert not cp.local_data(dict(channels=[row]), 56, 61)
