"""One bounded availability-only retry; never selects on signal outcomes."""
from broad_discovery import *
manifest=json.loads((BASE/'broad_manifest.json').read_text());out=BASE/'broad_discovery';audit=[]
for tape in manifest['tapes']:
    path=out/f"{tape['tape']}.json"
    if not path.exists():continue
    r=json.loads(path.read_text());bych={c['channel']:c for c in tape['channels']};changed=False
    for c in r['channels']:
        used=[w['start'] for w in c['windows'] if w['status'] in ['analyzed_audio','reused_audio_analysis']]
        for i,old in enumerate(c['windows']):
            if old['status']!='skipped_unavailable_or_incomplete':continue
            x,sr,new=try_window(bych[c['channel']],old['start'],old['duration'],used)
            audit.append(dict(tape=tape['tape'],channel=c['channel'],old=old,new_status=new['status']))
            if x is None:continue
            new['previous_skipped_record']=old;new['analysis']='quick_phase_screening_not_accepted_estimator';new['candidates']=[]
            for cand in discover(x[:len(x)//2],sr,2):
                meta,_=quick_track(x,sr,cand);meta.update(channel=c['channel'],window_start=new['start'],window_duration=new['duration'],best_peer=None,rank_score=0,status='unvalidated_fallback_no_peer_analysis')
                new['candidates'].append(meta)
            c['windows'][i]=new;used.append(new['start']);changed=True
    if changed:path.write_text(json.dumps(r,indent=2))
(out/'availability_retry.json').write_text(json.dumps(audit,indent=2));print('Availability retries',len(audit),'resolved',sum(x['new_status']=='analyzed_audio' for x in audit))
