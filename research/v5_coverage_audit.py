"""Stream and audit a v5 survey partition; no audio reads."""
import argparse,json,math
from pathlib import Path

EPS=1e-7

def audit(bundle):
 bundle=Path(bundle); manifest=json.loads((bundle/'survey_manifest.json').read_text(encoding='utf-8'))
 start,end=manifest['start_seconds'],manifest['requested_end_seconds']; rows={str(r['channel']):r for r in manifest['inventory']['channels']}
 expected={str(i) for i in range(1,31)} | {f'unassigned_{i+1}' for i,_ in enumerate(manifest['inventory'].get('excluded_files',[]))}
 states={ch:dict(channel=ch,requested_seconds=end-start,analyzed_seconds=0.,unavailable_seconds=0.,missing_seconds=0.,error_seconds=0.,gap_seconds=0.,insufficient_context_seconds=0.,other_seconds=0.,overlap_seconds=0.,partition_gap_seconds=0.,denoised_analyzed_seconds=0.,denoised_gap_seconds=0.,last_end=start,records=0) for ch in expected}
 unexpected=[]
 with (bundle/manifest.get('coverage_file','survey.jsonl')).open(encoding='utf-8') as f:
  for line_no,line in enumerate(f,1):
   try: r=json.loads(line); ch=str(r['channel']); lo=float(r['start_seconds']); hi=float(r['end_seconds'])
   except (ValueError,KeyError,TypeError) as e: unexpected.append(dict(line=line_no,error=str(e))); continue
   if ch not in states: unexpected.append(dict(line=line_no,error='unexpected channel',channel=ch)); continue
   s=states[ch]; s['records']+=1
   if not math.isfinite(lo) or not math.isfinite(hi) or hi<=lo or lo<start-EPS or hi>end+EPS:
    unexpected.append(dict(line=line_no,error='invalid interval',channel=ch,start=lo,end=hi)); continue
   if lo>s['last_end']+EPS:s['partition_gap_seconds']+=lo-s['last_end']
   if lo<s['last_end']-EPS:s['overlap_seconds']+=min(hi,s['last_end'])-lo
   s['last_end']=max(s['last_end'],hi)
   status=r['status']; n=hi-lo
   if status in ('analyzed','read_error','insufficient_context'):
    avlo=float(r.get('available_start',lo)); avhi=float(r.get('available_end',hi))
    if not lo-EPS<=avlo<=avhi<=hi+EPS: unexpected.append(dict(line=line_no,error='invalid availability',channel=ch)); continue
    s['unavailable_seconds']+=n-(avhi-avlo); n=avhi-avlo
    field={'analyzed':'analyzed_seconds','read_error':'error_seconds','insufficient_context':'insufficient_context_seconds'}[status]; s[field]+=n
   else:
    field={'unavailable':'unavailable_seconds','missing_channel':'missing_seconds','analysis_gap':'gap_seconds'}.get(status,'other_seconds'); s[field]+=n
 for ch,s in states.items():
  s['partition_gap_seconds']+=max(0.,end-s['last_end']); del s['last_end']
 for w in manifest['windows']:
  lo=max(start,w['start_seconds']); hi=min(end,w['end_seconds'])
  for ch,row in rows.items():
   a=max(lo,row['start_seconds']); b=min(hi,row['end_seconds']); n=max(0.,b-a)
   c=w['channels'].get(ch,{}); den=c.get('discovery_sources',{}).get('denoised',{})
   if isinstance(den,dict) and den.get('status')=='analyzed': states[ch]['denoised_analyzed_seconds']+=n
   elif manifest.get('denoised_root') is not None: states[ch]['denoised_gap_seconds']+=n
 valid=not unexpected and all(s['partition_gap_seconds']<EPS and s['overlap_seconds']<EPS for s in states.values())
 return dict(bundle=str(bundle),start_seconds=start,end_seconds=end,partition_valid=valid,unexpected=unexpected,channels=list(states.values()),manifest_complete=manifest.get('complete'),manifest_all_recorded_sources_covered=manifest.get('all_recorded_sources_covered'),denoised_requested=manifest.get('denoised_root') is not None)

if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('bundle');p.add_argument('output');a=p.parse_args()
 result=audit(a.bundle);Path(a.output).write_text(json.dumps(result,indent=2),encoding='utf-8');print(json.dumps(dict(partition_valid=result['partition_valid'],channels=len(result['channels']),unexpected=len(result['unexpected']))))
