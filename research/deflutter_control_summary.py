"""Derive count summaries from committed metrics, no hand-copied denominators."""
from deflutter_campaign import ROOT,read,write

rows=[]
for prefix in ('I043_','I047_','I050_','I051_'):
    for folder in sorted((ROOT/'iterations').glob(prefix+'*')):
        path=folder/'metrics.json'
        if not path.exists():continue
        m=read(path);s=m['summary']
        rows.append(dict(iteration=folder.name,cases=sum(v['cases'] for v in s.values()),
            withheld=sum(v['withheld'] for v in s.values()),
            true_complete_passes=sum(v['final_pass'] for k,v in s.items() if k.startswith('true_motion:')),
            available_negatives=sum(v['cases']-v['withheld'] for k,v in s.items() if not k.startswith('true_motion:')),
            false_complete_accepts=m['false_complete_accepts'],negative_validation_accepts=m['negative_validation_accepts'],
            evidence_scope='Feature-conditioned development' if prefix in ('I043_','I047_') else 'Sealed-eligibility synthetic development; no real authentication'))
write(ROOT/'control_count_summary.json',rows)
for row in rows:print(row,flush=True)
