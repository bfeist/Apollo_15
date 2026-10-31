"""Summarize a saved v5 assessment without treating proposal time as success.

Measurements remain event-level: reported span seconds identify where events
occurred, not seconds independently proven improved.
"""
import argparse, json
from pathlib import Path
p=argparse.ArgumentParser(); p.add_argument('assessment',type=Path); p.add_argument('output',type=Path); a=p.parse_args()
x=json.loads(a.assessment.read_text(encoding='utf-8-sig'))
channels=[]
for c in x['channels']:
    spans=[]
    for s in c.get('spans',[]):
        events=s['event_metrics']; distinct={round(e['original_start_seconds'],6) for e in events}
        valid=(len(distinct)==len(events) and all(e.get('status')=='passed' and e.get('metrics') and all(z.get('margins') for z in e['metrics']) for e in events))
        supported=s['proposed_correction'] and len(distinct)>=2 and valid and s['outcome'] in ('partial_improvement_at_measured_events','full_restoration_at_measured_events')
        spans.append(dict(start=s['start_seconds'],end=s['end_seconds'],seconds=s['seconds'],proposed=s['proposed_correction'],outcome=s['outcome'],distinct_events=len(distinct),qualified_event_span=bool(supported),worst_ratio=s.get('worst_ratio'),remaining_worst_rms_hz=s.get('remaining_worst_rms_hz')))
    channels.append(dict(channel=c['channel'],available_seconds=c.get('available_seconds'), proposed_seconds=sum(s['seconds'] for s in spans if s['proposed']),qualified_event_span_seconds=sum(s['seconds'] for s in spans if s['qualified_event_span']),spans=spans,map_integrity=c.get('map_integrity')))
out=dict(source_assessment=str(a.assessment),plan_sha256=x['plan_sha256'],channels=channels,proposed_channel_seconds=sum(c['proposed_seconds'] for c in channels),qualified_event_span_channel_seconds=sum(c['qualified_event_span_seconds'] for c in channels),interpretation='Qualified span duration is context for at least two distinct passing reserved events, not continuously measured successful correction. Unknown intervals and identity have no measured benefit.')
a.output.write_text(json.dumps(out,indent=2,allow_nan=False),encoding='utf-8'); print(a.output)
