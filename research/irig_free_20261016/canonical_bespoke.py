"""Bounded per-tape canonical experiments. Never writes source or production files."""
from broad_discovery import quick_track
from generic_scan import *
import importlib.util
OUT=ROOT/'reports/canonical_bespoke_20261017'
OUT.mkdir(exist_ok=True)
spec=importlib.util.spec_from_file_location('concat_readonly',ROOT/'scripts/01_concatenate.py')
concat=importlib.util.module_from_spec(spec);spec.loader.exec_module(concat)

def inventory():
    rows=[]
    saved=OUT/'inventory.json'
    previous={r['tape']:r for r in json.loads(saved.read_text())} if saved.exists() else {}
    for folder in sorted(Path('D:/Apollo_15_30-Track/orig_concatenated').glob('A15_T*')):
        row=dict(tape=folder.name,channels=[],status='deferred',reasons=[])
        markers=list(folder.glob('.timerange-*'));files=list(folder.glob('*.wav'))
        if len(markers)!=1 or list(folder.glob('*.partial')) or len(files)!=30:
            row['reasons'].append('requires shared completion marker, 30 WAVs and no partial output');rows.append(row);continue
        old=previous.get(folder.name)
        if old and old['status']=='verified_complete_against_local_delivery' and {str(p) for p in files}=={c['path'] for c in old['channels']} and all(Path(c['path']).stat().st_size==c['bytes'] and Path(c['path']).stat().st_mtime_ns==c['mtime_ns'] for c in old['channels']):
            rows.append(old);print('REUSE VERIFIED INVENTORY',folder.name,flush=True);continue
        groups=concat.group_fragment_files(Path('D:/Apollo_15_30-Track/orig_delivery')/folder.name)
        for p in sorted(files):
            try:
                before=stable(p);m=STAMP.search(p.name);ch=int(CHANNEL.search(p.name)[1]);key=concat.extract_base_name(p.name)
                fragments=[x[1] for x in groups[key]]
                if not concat.output_matches_inputs(fragments,p):raise ValueError('canonical does not match delivered frame totals/format')
                # Sample three fragments to check actual ordering/content without copying full tapes.
                offset=0;checks=[]
                chosen={0,len(fragments)//2,len(fragments)-1}
                for i,f in enumerate(fragments):
                    with wave.open(str(f),'rb') as w:
                        n=w.getnframes();rate=w.getframerate()
                    if i in chosen:
                        with sf.SoundFile(f) as a,sf.SoundFile(p) as b:
                            at=min(n//2,max(0,n-rate));a.seek(at);b.seek(offset+at)
                            xa=a.read(min(rate,n-at),dtype='int16');xb=b.read(min(rate,n-at),dtype='int16')
                        if not np.array_equal(xa,xb):raise ValueError('fragment/canonical sample mismatch')
                        checks.append(dict(fragment=str(f),canonical_frame=offset+at,frames=len(xa)))
                    offset+=n
                if before!=stable(p):raise ValueError('source changed while verifying')
                row['channels'].append(dict(channel=ch,path=str(p),head=re.search(r'_(HR\d+[UL])_',p.name)[1],bytes=before[0],mtime_ns=before[1],sample_rate=before[2],frames=before[3],duration=before[3]/before[2],absolute_start=seconds(m[1]),filename_end=seconds(m[2]),fragment_count=len(fragments),sample_identity_checks=checks))
            except Exception as e:row['reasons'].append(dict(path=str(p),reason=str(e)))
        if len(row['channels'])==30 and not row['reasons']:
            row['status']='verified_complete_against_local_delivery';row['origin']=min(c['absolute_start'] for c in row['channels']);row['duration']=min(c['absolute_start']+c['duration'] for c in row['channels'])-row['origin']
        rows.append(row);saved.write_text(json.dumps(rows,indent=2));print('INVENTORY',row['tape'],row['status'],len(row['channels']),flush=True)
    (OUT/'inventory.json').write_text(json.dumps(rows,indent=2));return rows

def get(c,tape,start,duration):
    offset=start+tape['origin']-c['absolute_start'];x,sr,m=read(Path(c['path']),offset,duration)
    if m['bytes']!=c['bytes'] or m['mtime_ns']!=c['mtime_ns']:raise ValueError('changed since inventory')
    return x,sr,dict(m,channel=c['channel'],head=c['head'],tape_start=start)

def survey_tape(tape,extra_starts=None):
    path=OUT/(tape['tape']+'_survey.json')
    if path.exists() and extra_starts is None:return json.loads(path.read_text())
    duration=60;starts=[round(f*(tape['duration']-duration),2) for f in [.15,.35,.55,.85]]
    special={'A15_T510073':31250.,'A15_T510075':52471.35,'A15_T510076':9383.4}
    if tape['tape'] in special:
        prior=special[tape['tape']];starts=[prior]+[s for s in starts if abs(s-prior)>120]
    result=json.loads(path.read_text()) if path.exists() else dict(tape=tape['tape'],duration=duration,windows=[])
    if extra_starts is not None:starts=extra_starts
    starts=[s for s in starts if all(abs(s-w['start'])>120 for w in result['windows'])]
    for wi,start in enumerate(starts):
        candidates=[];events=[];sources=[];curves={}
        for c in tape['channels']:
            x,sr,meta=get(c,tape,start,duration);sources.append(meta)
            for b in beeps(x,sr):events.append(dict(b,channel=c['channel'],head=c['head']))
            for cand in discover(x[:30*sr],sr,4):
                if 2200<cand['carrier_hz']<2800:continue # keep signaling band out of reference search
                row,e=quick_track(x,sr,cand);row.update(channel=c['channel'],head=c['head'],id=len(candidates));curves[row['id']]=e;candidates.append(row)
        # Reference-only ranking is a suggestion, not an exclusion gate for bespoke options.
        for c in candidates:
            peers=[]
            for d in candidates:
                if d['channel']==c['channel'] or abs(d['carrier_hz']-c['carrier_hz'])<80:continue
                ratio=max(c['carrier_hz'],d['carrier_hz'])/min(c['carrier_hz'],d['carrier_hz'])
                if abs(ratio-round(ratio))<.03 or c['near_mains_family'] and d['near_mains_family']:continue
                a=curves[c['id']][400:5600];b=curves[d['id']][400:5600]
                if min(np.std(a),np.std(b))>1e-8:peers.append((float(np.corrcoef(a,b)[0,1]),d['id']))
            c['peer']=max(peers) if peers else None
            c['rank']=c['occupancy']*c['phase_support']*min(c['contrast_db']/30,1)*(.2+max(0,c['peer'][0] if c['peer'] else 0))*min(1,c['modulation_concentration']/.4)
        candidates.sort(key=lambda c:c['rank'],reverse=True)
        row=dict(start=start,duration=duration,sources=sources,candidates=candidates,events=events)
        result['windows'].append(row);path.write_text(json.dumps(result,indent=2))
        print('SURVEY',tape['tape'],start,'events',len(events),'top',[(c['channel'],c['carrier_hz'],round(c['rank'],2)) for c in candidates[:3]],flush=True)
    return result

if __name__=='__main__':
    ap=argparse.ArgumentParser();ap.add_argument('action',choices=['inventory','survey']);a=ap.parse_args()
    if a.action=='inventory':inventory()
    else:
        for tape in json.loads((OUT/'inventory.json').read_text()):
            if tape['status']=='verified_complete_against_local_delivery':survey_tape(tape)
