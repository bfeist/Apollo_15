"""Fresh randomized dependency controls; no real correction outcomes."""
from collections import defaultdict
import numpy as np
from deflutter_campaign import ROOT,SEED,begin,finish,write,sha
from deflutter_event_groups import assign_roles


def run():
    folder=begin('I010p','Per-pair lag grouping protects delayed copies across varied timebases',
        'Known synthetic response families versus fixed lag-aware grouping, including unrelated feeds',
        dict(unprotected_copy_role_conflicts=0,mean_usable_true_family_fraction_min=.8,
             ambiguity='Periodic cases separately unit-tested to withhold rather than invent independence'),
        synthetic=dict(seed=SEED+'dependency-promotion-v1',cases=64,core_events=30,
            copies=3,independent_extra_feed_events=10,relative_delay_domain_seconds=[-18,18],
            copy_jitter_seconds=[-.02,.02],dropout_probability=.15,
            intervals_seconds=[3.5,12],minimum_pattern_events=3,max_model_lag_seconds=20),
        qualifications='Fixed-passage event grouping only; no transport, audio correction or online-role-resume claim')
    rng=np.random.default_rng(int(sha((SEED+'dependency-promotion-v1').encode())[:16],16));results=[]
    for index in range(64):
        times=np.cumsum(rng.uniform(3.5,12,30))+20
        delays=[0.,float(rng.uniform(-9,9)),float(rng.uniform(-9,9))];batches=[]
        for channel,delay in enumerate(delays):
            events=[]
            for n,t in enumerate(times):
                if rng.random()<.15:continue
                start=float(t+delay+rng.uniform(-.02,.02))
                events.append(dict(start=start,end=start+.25,truth_family='core_'+str(n)))
            batches.append((channel,events,0))
        extra=[dict(start=float(t),end=float(t+.25),truth_family='independent_'+str(n)) for n,t in enumerate(np.sort(rng.uniform(20,float(times[-1]),10)))]
        batches.append((99,extra,0))
        assigned,state=assign_roles(batches);families=defaultdict(list)
        for es in assigned.values():
            for e in es:families[e['truth_family']].append(e)
        conflicts=0;usable=0;truthcopies=0
        for name,es in families.items():
            if not name.startswith('core_'):continue
            active=[e for e in es if e['role']!='withheld_ambiguous_dependency']
            if len(es)>1:truthcopies+=1
            if len({e['role'] for e in active})>1:conflicts+=1
            if active:usable+=1
        results.append(dict(case=index,delays=delays,unprotected_copy_role_conflicts=conflicts,
            true_copy_families=truthcopies,usable_true_families=usable,usable_fraction=usable/30,
            raw_events=state['raw_events'],groups=state['response_groups'],withheld_groups=state['withheld_groups']))
    write(folder/'results.json',results)
    total=sum(r['unprotected_copy_role_conflicts'] for r in results);mean=float(np.mean([r['usable_fraction'] for r in results]))
    metrics=dict(cases=64,unprotected_copy_role_conflicts=total,mean_usable_true_family_fraction=mean,
        worst_usable_true_family_fraction=min(r['usable_fraction'] for r in results))
    finish(folder,metrics,'retain_for_fixed_passage_gate' if total==0 and mean>=.8 else 'reject',
        f'Fresh dependency draws: {total} unprotected copy-role conflicts; mean usable fraction {mean:.3f}. Domain and worst case retained.',
        'Only a passing frozen grouping contract may be used on unused real blocks; keep all old event families exposed and ineligible as fresh gates.')


if __name__=='__main__':run()
