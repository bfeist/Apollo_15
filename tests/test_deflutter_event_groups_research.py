import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'research'))
from deflutter_event_groups import assign_roles,fit_delay


def events(starts):return [dict(start=t,end=t+.25) for t in starts]


def test_delayed_copies_cannot_cross_response_roles():
    times=[2.,7.2,19.8,27.1,35.5,38.9,50.1,57.7]
    a=events(times);b=events([t+1.4+(i%3-1)*.006 for i,t in enumerate(times)])
    assigned,state=assign_roles([(14,a,0),(17,b,0)])
    assert state['response_groups']==len(times)
    assert [e['role'] for e in assigned[14]]==[e['role'] for e in assigned[17]]
    assert [e['synchronized_group_id'] for e in assigned[14]]==[e['synchronized_group_id'] for e in assigned[17]]


def test_ambiguous_periodic_matches_are_withheld_not_false_independence():
    assigned,state=assign_roles([(1,events(list(range(0,80,4))),0),(2,events([t+1.4 for t in range(0,80,4)]),0)])
    assert any(m['status']=='ambiguous_periodic_dependency' for m in state['models'])
    assert all(e['role']=='withheld_ambiguous_dependency' for es in assigned.values() for e in es)


def test_input_order_does_not_change_groups_or_roles():
    a=events([2.,7.2,19.8,27.1,35.5]);b=events([3.4,8.6,21.2,28.5,36.9])
    first,state=assign_roles([(1,a,0),(2,b,0)])
    second,other=assign_roles([(2,list(reversed(b)),0),(1,list(reversed(a)),0)])
    assert first==second and state==other


def test_two_coincidental_timing_matches_do_not_fit_a_delayed_feed():
    result=fit_delay([0.,8.,27.,44.],[1.4,9.4,33.,58.])
    assert result['status']=='no_delayed_model'
