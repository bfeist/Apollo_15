"""Measured lead-in is a tape coordinate transform, while stage-04 files retain acquisition time."""
from pathlib import Path
import sys

import numpy as np
import soundfile as sf

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from _bootstrap import install
install()
import flutter_alignment as audit
from flutter_detection import read_block
from shared_tape import register_saved_candidates
from tape_coordinates import acquisition_row, aligned_inventory


def test_broadband_match_signed_with_inverted_copy(tmp_path):
    rng = np.random.default_rng(45)
    source = rng.standard_normal(8000*48).astype('float32')*.04
    delay = 7828
    rows = []
    for ch, x in enumerate((source, np.r_[np.zeros(delay), -source[:-delay]], rng.normal(0,.04,len(source))),1):
        path = tmp_path/f'{ch}.wav'
        sf.write(path,x,8000,subtype='FLOAT')
        rows.append(dict(channel=ch,path=str(path),sample_rate=8000,start_seconds=0))
    measurement = audit.measurements(rows,0)
    assert len(measurement['edges']) == 1
    a,b,lag,strength = measurement['edges'][0]
    assert (a,b)==(1,2) and abs(lag-delay/8000)<=.0005


def test_measured_clock_and_ambiguous_cases(tmp_path):
    source = np.random.default_rng(12).normal(0,.04,8000*3).astype('float32')
    rows = []
    for ch in range(1,8):
        delay = 7828 if ch==7 else 0
        path = tmp_path/f'CH{ch}.wav'
        sf.write(path,np.r_[np.zeros(delay),source],8000,subtype='FLOAT')
        info=path.stat()
        rows.append(dict(path=str(path),bytes=info.st_size,mtime_ns=info.st_mtime_ns,
                         frames=len(source)+delay,sample_rate=8000,channel=ch,
                         duration_seconds=3+delay/8000,start_seconds=0,end_seconds=3+delay/8000))
    inv=dict(tape='T',channels=rows,duration_seconds=3.9785)
    def window(*_):
        return dict(fingerprints=[str(i) for i in range(7)],edges=[
            [a,b, .9785 if b==7 else 0, 35] for a in range(1,8) for b in range(a+1,8)])
    # The clock is tested with controlled evidence at six independent epochs.
    import json
    from tape_coordinates import report_path
    windows=[window() for _ in range(6)]
    decision=audit.decide(inv,windows)
    assert decision['action']=='retrace'
    path=report_path(tmp_path,inv)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(dict(tape='T',source_key=audit.source_key(inv),
        method_sha256=audit.hashlib.sha256(Path(audit.__file__).read_bytes()).hexdigest(),
        windows=windows,**decision)))
    aligned=aligned_inventory(inv,tmp_path)
    assert [r['start_seconds'] for r in aligned['channels']]==[.9785]*6+[0]
    assert [acquisition_row(r) for r in aligned['channels']]==rows
    read,_=read_block(aligned['channels'],1,2,8000)
    assert np.array_equal(read(1)[0],read(7)[0])
    candidate=dict(reference_channel=1,id='line',witnesses=[dict(channel=2),dict(channel=7)],
                   start_seconds=1,end_seconds=2,curve=dict(start_seconds=0))
    rejected=[]
    c,=register_saved_candidates([candidate],rejected,aligned)
    assert c['curve']['start_seconds']==.9785 and c['start_seconds']==1.9785
    assert c['witnesses']==[dict(channel=2)] and rejected[0]['channel']==7
    # An echo lag unsupported by acquisition boundaries or a lag that steps halfway stays unresolved.
    equal_ends=dict(inv,channels=[dict(r,end_seconds=3) for r in rows])
    assert audit.decide(equal_ends,windows)['action']=='review'
    for w in windows[3:]:
        for edge in w['edges']:
            edge[2]=0
    assert audit.decide(inv,windows)['action']=='review'


def test_one_shared_map_handles_staggered_file_edges(tmp_path):
    from shared_tape import build_tape_plan, unavailable_endpoints
    from tests.test_shared_tape import fixture
    from denoise_archive import sha256

    plan, assessment, inv = fixture(tmp_path)
    receipt = tmp_path/'alignment.json'
    receipt.write_text('{}')
    rows = []
    for row in inv['channels']:
        start = 14.083 if row['channel'] == 20 else row['start_seconds']
        rows.append(dict(row, acquisition_start_seconds=row['start_seconds'], start_seconds=start,
                         end_seconds=start+row['duration_seconds']))
    aligned = dict(inv, channels=rows, duration_seconds=max(r['end_seconds'] for r in rows),
                   alignment=dict(path=str(receipt), sha256=sha256(receipt), action='retrace'))
    output = tmp_path/'map'
    result = build_tape_plan(plan, assessment, output, inventory=aligned)
    assert not unavailable_endpoints(output/'tape_map.npy', rows)
    assert result['map']['supported_seconds'] > 10
    assert result['map']['capture_edge_bypass'][0][1] > rows[-1]['start_seconds']


def test_publish_current_tape_verifies_and_preserves_previous_edition(tmp_path, monkeypatch):
    import json
    import pytest
    import recover_capture_alignment as recovery
    from denoise_archive import atomic_json, sha256

    tape, root = 'A15_T510073', tmp_path/'recovery'
    monkeypatch.setattr(recovery, 'DATA', tmp_path)
    monkeypatch.setattr(recovery, 'OLD_PLANS', tmp_path/'old_plans')
    old = recovery.OLD_PLANS/tape/'plan.json'
    old.parent.mkdir(parents=True)
    atomic_json(old, dict(edition='old'))
    current = tmp_path/'defluttered_shared'/tape/'CH21'
    current.mkdir(parents=True)
    atomic_json(current/'review.json', dict(plan_sha256=sha256(old)))
    (current/'deflutter.pkf').write_bytes(b'old peaks')
    source = dict(channel=21, acquisition_start_seconds=0., sample_rate=8000)
    plan = root/'plans'/tape/'plan.json'
    plan.parent.mkdir(parents=True)
    atomic_json(plan, dict(qualified=True, full_tape=True, map=dict(sha256='new-map'),
                           inventory=dict(tape=tape, alignment=dict(action='retrace'), channels=[source])))
    release = root/'stage04.json'
    atomic_json(release, dict(status='complete', results=[dict(tape=tape, channel=21, frames=80, sha256='stage04')]))
    staged = root/'defluttered_shared'/tape/'CH21'
    staged.mkdir(parents=True)
    wav = staged/'deflutter.wav'
    sf.write(wav, np.zeros(80), 8000, subtype='FLOAT')
    review = dict(tape=tape, channel=21, frames=80, sample_rate=8000, source=source, status='complete',
                  crosstalk_removed=True, plan_sha256=sha256(plan), map_sha256='new-map',
                  source_audio_sha256='stage04', audio=dict(deflutter=dict(sha256='corrupted')))
    atomic_json(staged/'review.json', review)
    assert not recovery.publish_current_tape(tape, root, release, check_existing=True)
    with pytest.raises(ValueError, match='Invalid completed WAV'):
        recovery.publish_current_tape(tape, root, release)
    assert (current/'deflutter.pkf').is_file()
    review['audio']['deflutter']['sha256'] = sha256(wav)
    atomic_json(staged/'review.json', review)
    if sys.platform == 'win32':
        with (current/'deflutter.pkf').open('rb'):
            with pytest.raises(recovery.PublicationBlocked, match='Windows denied the rename'):
                recovery.publish_current_tape(tape, root, release)
            assert wav.is_file()
            assert not (root/'previous_defluttered_shared'/tape).exists()
    assert recovery.publish_current_tape(tape, root, release)
    assert json.loads((current/'review.json').read_bytes()) == review
    assert (root/'previous_defluttered_shared'/tape/'CH21/deflutter.pkf').read_bytes() == b'old peaks'
    assert recovery.publish_current_tape(tape, root, release, check_existing=True)


def test_blocked_publication_is_retried_after_other_tapes(tmp_path, monkeypatch):
    import recover_capture_alignment as recovery

    t1, t2, clean = 'A15_T510073', 'A15_T510088', 'A15_T510075'
    stages = []
    def run_stage(name, *args):
        if name == '05b_render_archive.py':
            stages.append(('render', args[args.index('--tape')+1]))

    def publish(tape, folder, release, *, check_existing=False):
        if check_existing:
            return False
        stages.append(('publish', tape))
        if tape == t1 and stages.count(('publish', tape)) == 1:
            raise recovery.PublicationBlocked('WAV open in editor')
        return True

    monkeypatch.setattr(recovery, 'inventory', lambda path: {})
    monkeypatch.setattr(recovery, 'aligned_inventory', lambda inv, root: inv)
    monkeypatch.setattr(recovery, 'find_trace', lambda tape, inv: tmp_path/'trace')
    monkeypatch.setattr(recovery, 'qualify_selected', lambda *args: tmp_path/'qualified')
    monkeypatch.setattr(recovery, 'validate_release', lambda *args: None)
    monkeypatch.setattr(recovery, 'recover_stage04', lambda *args: tmp_path/'release')
    monkeypatch.setattr(recovery, 'run_stage', run_stage)
    monkeypatch.setattr(recovery, 'publish_current_tape', publish)
    for tape in (t1, t2):
        (tmp_path/'plans'/tape).mkdir(parents=True)
    rows = {t:dict(action='reuse' if t == clean else 'retrace') for t in (t1, t2, clean)}
    recovery.recover(rows, tmp_path, 1, 0, True)
    assert stages == [('render', t1), ('publish', t1), ('render', t2), ('publish', t2),
                      ('render', clean), ('publish', t1)]
