"""Export the frozen matched results without interpreting missing data as zero."""
import csv,json,statistics
from pathlib import Path
root=Path('reports/research_v5_allchannel_20261027/evaluation')
passages=[('dev_0','A15_T510073',0,180,'v4_dev_0_joined.json'),('dev_10920','A15_T510073',10920,240,'v4_dev_10920_joined.json'),('dev_13080','A15_T510073',13080,180,'v4_dev_13080_joined.json'),('final_073','A15_T510073',49020,120,'v4_final_49020_joined.json'),('final_075','A15_T510075',5300,120,None)]
rows=[]
for name,tape,start,duration,v4 in passages:
 assessment=json.loads((root/f'v5_frozen_{name}_assessment.json').read_text())
 for c in assessment['channels']:
  spans=c.get('spans',[]); rows.append(dict(passage=name,tape=tape,start=start,duration=duration,version='v5',channel=c['channel'],status=c['status'],proposed_seconds=c.get('proposed_correction_seconds'),qualified_span_seconds=c.get('spans_with_reserved_improvement_seconds'),median_before_hz=c.get('median_before_rms_hz'),median_after_hz=c.get('median_after_rms_hz'),worst_ratio=c.get('worst_ratio'),worst_after_hz=c.get('worst_after_rms_hz'),events=sum(s['reserved_event_count'] for s in spans),outcomes=';'.join(sorted({s['outcome'] for s in spans})),note='Span duration is not continuously measured success'))
 if v4:
  data=json.loads((root/v4).read_text())
  for c in data['rows']:
   if c.get('error'):
    rows.append(dict(passage=name,tape=tape,start=start,duration=duration,version='v4',channel=c['channel'],status='audit_error',note=c['error'])); continue
   events=c['checks']['reserved_events']; ms=[m for e in events for v in e.get('metrics',[]) for m in v.get('margins',[])]
   before=[m['before_rms_hz'] for m in ms]; after=[m['after_rms_hz'] for m in ms]; ratios=[m['residual_ratio'] for m in ms]
   rows.append(dict(passage=name,tape=tape,start=start,duration=duration,version='v4',channel=c['channel'],status=c['checks']['status'],proposed_seconds=sum(s['end_seconds']-s['start_seconds'] for s in c['spans'] if s.get('selected')),median_before_hz=statistics.median(before) if before else None,median_after_hz=statistics.median(after) if after else None,worst_after_hz=max(after) if after else None,worst_ratio=max(ratios) if ratios else None,events=len(events),outcomes=';'.join(sorted({s.get('quality_level','unknown') or 'unknown' for s in c['spans'] if s.get('selected')})),note='No-event identity is insufficient evidence; passed can mean map integrity only'))
 else:
  for c in assessment['channels']:
   rows.append(dict(passage=name,tape=tape,start=start,duration=duration,version='v4',channel=c['channel'],status='baseline_failed',note='V4 aborted: input vector x must be greater than padlen 18; correction is unknown, not zero'))
fields=['passage','tape','start','duration','version','channel','status','proposed_seconds','qualified_span_seconds','median_before_hz','median_after_hz','worst_after_hz','worst_ratio','events','outcomes','note']
with (root/'per_channel.csv').open('w',newline='',encoding='utf-8') as f:
 w=csv.DictWriter(f,fieldnames=fields); w.writeheader(); w.writerows(rows)
print(len(rows))
