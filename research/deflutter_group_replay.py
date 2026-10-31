"""Metadata-only replay of fixed lag-aware response grouping."""
from collections import Counter
import time
from deflutter_campaign import ROOT,begin,finish,read,write,exposure
from deflutter_event_groups import assign_roles


def run():
    folder=begin('I010','Delayed response copies must share a frozen role or be withheld',
        'Old .5s coincidence roles versus lag-aware timestamp-pattern groups on already exposed event metadata',
        'Zero role conflicts among every assigned dependency group; ambiguous periodic matches withheld, no correction metrics accessed',
        input='I005c2 roles ledger coordinates only',lag_domain_seconds=[-20,20],delay_tolerance_seconds=.06,
        minimum_pattern_events=3,selection_outcomes_used=False)
    old=read(ROOT/'iterations/I005c2/roles_0.json')['ledger'];batches=[]
    for ch,row in old['channels'].items():
        batches.append((int(ch),[dict(start_seconds=e['start_seconds'],end_seconds=e['end_seconds']) for e in row['events']],0))
    assigned,state=assign_roles(batches);write(folder/'grouped_metadata.json',dict(assigned=assigned,state=state))
    lookup={e['event_id']:e for es in assigned.values() for e in es}
    conflicts=sum(len({lookup[i]['role'] for i in g['members']})>1 for g in state['groups'])
    counts={str(ch):dict(Counter(e['role'] for e in es)) for ch,es in assigned.items()}
    metrics=dict(raw_events=state['raw_events'],response_groups=state['response_groups'],usable_groups=state['usable_response_groups'],
        withheld_groups=state['withheld_groups'],potential_copy_models=len(state['models']),group_role_conflicts=conflicts,
        per_channel_roles=counts,old_known_pair_role_conflicts=5,new_known_pair_roles={str(ch):[(e['start_seconds'],e['role'],e['synchronized_group_id']) for e in assigned[ch]] for ch in (14,17)})
    exposure('I010',['I005c2/roles_0.json coordinates only'],'response_dependency_metadata','Role grouping replay; no new correction outcome or fresh-gate claim')
    finish(folder,metrics,'retain_for_new_gate' if conflicts==0 else 'reject',
        f"{state['raw_events']} raw events -> {state['response_groups']} potential groups; {state['withheld_groups']} withheld; {conflicts} within-group role conflicts. Timing grouping is not proof of shared transport.",
        'Freeze lag-aware roles before new unused block scoring; use one target per gate initially, preserve ambiguous-dependency withholding.')


if __name__=='__main__':run()
