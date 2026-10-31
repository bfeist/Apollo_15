import sys
from pathlib import Path
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'research'))
from deflutter_local_acquisition import opportunities, source_amplitude_reference, common_event_acquisition,qualified_support_island


def fixed_events():
    cycle=('calibration','calibration','validation','validation','final')
    return [dict(start=float(t),end=float(t+.25),role=cycle[i%5],synchronized_group_id=f'g{i}')
            for i,t in enumerate(range(4,300,4))]


def test_real_reference_lifetime_is_not_required_to_cover_all_300s_events():
    support=np.zeros(300*200);support[101*200:199*200]=1
    result=opportunities(fixed_events(),support)
    assert result['opportunities']
    p=result['opportunities'][0]
    assert p['physical_support_island']==[101.,199.]
    assert len(p['roles']['calibration'])>=2 and len(p['roles']['validation'])>=2
    assert p['required_support_start']>=101 and p['required_support_end']<=199
    assert not result['thresholds_relaxed'] and not result['correction_qualified']


def test_calibration_and_validation_on_opposite_sides_of_gap_cannot_be_pooled():
    support=np.zeros(30*200);support[4*200:15*200]=1;support[17*200:26*200]=1
    events=[dict(start=t,end=t+.25,role=r,synchronized_group_id=str(t))
            for t,r in [(6,'calibration'),(10,'calibration'),(19,'validation'),(23,'validation')]]
    assert not opportunities(events,support)['opportunities']


def test_role_conflicts_are_refused_even_when_event_counts_look_sufficient():
    events=fixed_events();events[2]['synchronized_group_id']=events[0]['synchronized_group_id']
    try:opportunities(events,np.ones(300*200))
    except ValueError as e:assert 'conflicting roles' in str(e)
    else:raise AssertionError('A response family crossed roles')


def test_source_amplitude_does_not_need_known_support_interval_or_future_audio():
    x=np.full(300*200,.0001);x[80*200:100*200]=.02;x[200*200:]=10
    reference,meta=source_amplitude_reference(x,available_end=120)
    assert reference==.02
    assert meta['selected_window_seconds'][1]<=120
    assert not meta['response_outcomes_accessed']


def test_isolated_loud_source_burst_cannot_set_persistent_reference_amplitude():
    x=np.full(120*200,.01);x[30*200:31*200]=1
    assert source_amplitude_reference(x)[0]==.01


def test_common_attempt_is_frozen_from_event_coordinates_before_source_scores():
    es=[dict(start=t,end=t+.25,role=r,synchronized_group_id=str(t))
        for t,r in [(10,'calibration'),(20,'calibration'),(90,'calibration'),(100,'calibration'),
                    (110,'validation'),(119,'validation'),(250,'validation')]]
    for e in es:e.update(start_seconds=50000+e['start'],end_seconds=50000+e['end'])
    p=common_event_acquisition(es,300)
    assert p['start_seconds']==60 and p['end_seconds']==120
    assert [e['start_seconds'] for e in p['roles']['calibration']]==[90,100]
    assert [e['start_seconds'] for e in p['roles']['validation']]==[110,119]


def test_final_capable_interval_uses_counts_only_without_relaxing_any_split():
    es=[dict(start=t,end=t+.25,role=r,synchronized_group_id=str(t))
        for t,r in [(90,'calibration'),(100,'calibration'),(110,'validation'),(119,'validation'),
                    (130,'final'),(140,'final')]]
    p=common_event_acquisition(es,300,minimum_final_events=2)
    assert p['start_seconds']==60 and p['end_seconds']==180
    assert len(p['roles']['final'])==2
    assert p['required_support_start']==90 and p['required_support_end']==119.25
    assert common_event_acquisition(es[:-1],300,minimum_final_events=2) is None


def test_two_copies_of_one_final_family_cannot_supply_two_reserved_events():
    es=[dict(start=t,end=t+.25,role=r,synchronized_group_id=str(t))
        for t,r in [(90,'calibration'),(100,'calibration'),(110,'validation'),(119,'validation'),
                    (130,'final'),(140,'final')]]
    es[-1]['synchronized_group_id']=es[-2]['synchronized_group_id']
    assert common_event_acquisition(es,300,minimum_final_events=2) is None


def test_map_cannot_correct_an_unqualified_reacquisition_island():
    support=np.zeros(300*200);support[40*200:160*200]=1;support[220*200:225*200]=1
    cal=[dict(start=t,end=t+.25) for t in (60,80)];val=[dict(start=t,end=t+.25) for t in (100,120)]
    kept,info=qualified_support_island(support,cal,val)
    assert np.array_equal(kept[40*200:160*200],support[40*200:160*200])
    assert np.all(kept[220*200:225*200]==0) and info['removed_other_islands']==1


def test_reserved_families_on_different_islands_cannot_be_pooled():
    support=np.zeros(300*200);support[40*200:160*200]=1;support[220*200:225*200]=1
    cal=[dict(start=t,end=t+.25) for t in (60,80)];val=[dict(start=t,end=t+.25) for t in (100,120)]
    final=[dict(start=t,end=t+.25,synchronized_group_id=str(t)) for t in (140,222)]
    kept,info=qualified_support_island(support,cal,val,final,minimum_final_events=2)
    assert kept is None and info['status']=='insufficient_final_families_in_same_island'
