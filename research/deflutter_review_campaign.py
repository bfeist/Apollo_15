"""Local coverage accounting and blinded review of frozen research maps."""
import os
for _name in ('OPENBLAS_NUM_THREADS','OMP_NUM_THREADS','MKL_NUM_THREADS'):
    os.environ[_name]='1'
import argparse
from collections import Counter,defaultdict
import csv
import html
import json
from pathlib import Path
import time

import numpy as np
import soundfile as sf

from deflutter_campaign import ROOT,SEED,begin,finish,read,write,load_excerpt,sha,exposure
from deflutter_real_experiment import merge_catalog
from flutter import resample_at


def coverage(catalog_iteration):
    names=catalog_iteration.split(',')
    records=[r for name in names for r in read(ROOT/'iterations'/name/'source_records.json')]
    invs=read(ROOT/'inventory.json')['tapes']
    columns=['tape','channel','source_version','source_role','head','available_seconds',
        'analyzed_seconds','not_searched_seconds','error_seconds','status','analysis_intervals']
    rows=[]
    for inv in invs:
        if 'channels' not in inv:
            rows.append(dict(tape=inv['tape'],status='inventory_error'));continue
        for source in inv['channels']+inv.get('unassigned_sources',[]):
            ch=source['channel']
            for version in ('original','denoised'):
                available=source['end_seconds']-source['start_seconds'] if version=='original' or source.get('denoised_available') else 0.
                selected=[r for r in records if r['tape']==inv['tape'] and r['channel']==ch and r['version']==version]
                def union_seconds(intervals):
                    merged=[]
                    for lo,hi in sorted(intervals):
                        if hi<=lo:continue
                        if merged and lo<=merged[-1][1]+1e-8:merged[-1][1]=max(merged[-1][1],hi)
                        else:merged.append([lo,hi])
                    return sum(b-a for a,b in merged)
                analyzed_intervals=[[r['start'],r['end']] for r in selected if r['status']=='analyzed']
                error_intervals=[[r['start'],r['end']] for r in selected if r['status']=='read_error']
                analyzed=union_seconds(analyzed_intervals)
                # A successful later retry supersedes an errored overlap.
                errored=union_seconds(analyzed_intervals+error_intervals)-analyzed
                rows.append(dict(tape=inv['tape'],channel=ch,source_version=version,source_role=source['role'],
                    head=source.get('head'),available_seconds=available,analyzed_seconds=analyzed,
                    not_searched_seconds=max(0,available-analyzed-errored),error_seconds=errored,
                    status='missing_source' if not available else 'partially_analyzed' if analyzed else 'not_searched',
                    analysis_intervals=json.dumps([[r['start'],r['end']] for r in selected if r['status']=='analyzed'])))
        for ch in inv['missing_channels']:
            for version in ('original','denoised'):
                rows.append(dict(tape=inv['tape'],channel=ch,source_version=version,status='missing_source',available_seconds=0,analyzed_seconds=0,not_searched_seconds=0,error_seconds=0))
    with (ROOT/'archive_coverage.csv').open('w',newline='',encoding='utf-8') as stream:
        writer=csv.DictWriter(stream,fieldnames=columns);writer.writeheader();writer.writerows(rows)
    summary=dict(catalog_iterations=names,source_versions=len(rows),
        available_source_seconds=sum(r.get('available_seconds',0) for r in rows),
        analyzed_source_seconds=sum(r.get('analyzed_seconds',0) for r in rows),
        not_searched_source_seconds=sum(r.get('not_searched_seconds',0) for r in rows),
        errored_source_seconds=sum(r.get('error_seconds',0) for r in rows),
        statuses=dict(Counter(r['status'] for r in rows)),
        caveat='Analyzed source time is detector execution, not tone recall or validated correction. Response band 2150-2850Hz withheld; DC/edge and resolution-specific contamination masks retained in catalog accounting.')
    write(ROOT/'archive_coverage.json',summary)
    print(json.dumps(summary),flush=True)


def crop_render(x,positions,sr,lo,hi):
    positions=positions[round(lo*sr):round(hi*sr)]
    left=max(0,int(np.floor(positions.min()))-sr//4)
    right=min(len(x),int(np.ceil(positions.max()))+sr//4)
    if positions.min()<0 or positions.max()>len(x)-1:
        raise ValueError('Review crop requires source context outside the loaded passage')
    return resample_at(x[left:right],positions-left)


def render(iteration,result_iteration):
    sourcefolder=ROOT/'iterations'/result_iteration
    results=read(sourcefolder/'results.json')
    folder=begin(iteration,'Matched blinded excerpts expose perceptual effects of exact saved maps',
        'Identity, frozen full-duration baseline, and frozen full-passage challenger; common cached-denoised source and unit gain',
        'No re-denoising, no per-alternative normalization, no short-crop reanchoring; listening remains pending',
        result_sha256=sha((sourcefolder/'results.json').read_bytes()),
        sampling='Every evaluated target: worst validation event plus fixed seeded random interior excerpt; failed numeric choices retained as explicit diagnostic reviews',
        approval='Research review only; failed choices cannot become production by being rendered')
    invs={r['tape']:r for r in read(ROOT/'inventory.json')['tapes']}
    queue=[];keys=[];tick=time.perf_counter()
    for index,result in enumerate(results):
        if not result.get('selected'):continue
        tape=result['tape'];ch=result['target_channel'];start=result['start'];end=result['end']
        targetfolder=sourcefolder/f'{tape}_{start}_CH{ch:02d}'
        inv=invs[tape];row=next(r for r in inv['channels'] if r['channel']==ch)
        if not row.get('denoised_available'):
            queue.append(dict(tape=tape,channel=ch,status='missing_cached_denoised'));continue
        x,sr,source=load_excerpt(inv,row,start,end,'denoised')
        frames=len(x);identity=np.arange(frames,dtype=float)
        saved=np.load(targetfolder/'challenger_map.npz')
        cp=np.interp(np.arange(frames)/sr,saved['nominal'],saved['tt'])*sr
        alternatives=dict(identity=identity,challenger=cp)
        bp=targetfolder/'baseline_positions.npz'
        if bp.exists():alternatives['baseline']=np.load(bp)['positions']
        validations=result.get('validation',[])
        worst=max(validations,key=lambda e:max(m['residual_ratio'] for m in e['margins']),default=None)
        random_start=5+int(sha(f'{SEED}:review:{tape}:{start}:{ch}'.encode())[:8],16)%max(1,int(end-start-25))
        starts=[('seeded_random',float(random_start))]
        if worst:starts.append(('worst_validation',max(2.,min(end-start-14,worst['start']-5))))
        for kind,lo in starts:
            hi=min(end-start-2,lo+12)
            rid=f'R{len(keys)+1:03d}'
            order=sorted(alternatives,key=lambda name:sha(f'{SEED}:{rid}:{name}'.encode()))
            files=[];mapping={}
            for n,name in enumerate(order):
                label=chr(ord('A')+n);filename=f'{rid}_{label}.wav'
                y=crop_render(x,alternatives[name],sr,lo,hi)
                sf.write(folder/filename,y,sr,subtype='FLOAT')
                files.append(dict(label=label,path=filename,sha256=sha((folder/filename).read_bytes()),
                    peak=float(np.max(abs(y))),sample_rate=sr,frames=len(y)))
                mapping[label]=name
            queue.append(dict(review_id=rid,tape=tape,channel=ch,source_start_seconds=start+lo,
                duration_seconds=hi-lo,sampling_kind=kind,files=files,status='listening_pending',
                questions=['Which has steadier pitch and less warble?','Any new clicks, smeared consonants, or transients?',
                    'Is speech clearer or worse?','Are alternatives indistinguishable?'],
                common_gain=1.,normalization=False,cleanup_rerun=False))
            keys.append(dict(review_id=rid,mapping=mapping,numeric_status=result['status'],
                scientific_selection=result['selected'],source=source,
                challenger_map_sha256=sha((targetfolder/'challenger_map.npz').read_bytes()),
                baseline_positions_sha256=sha(bp.read_bytes()) if bp.exists() else None,
                map_scope=dict(challenger=[start,end],baseline=result.get('baseline',{}).get('metadata',{}).get('map_scope')),
                full_map_evaluated_before_crop=True,diagnostic_failed_choice=result['status']=='validation_failed'))
        print('rendered',tape,ch,len(starts),'excerpts',flush=True)
    write(folder/'review_queue.json',queue);write(folder/'blinding_key.json',keys)
    cards=[]
    for r in queue:
        if 'review_id' not in r:continue
        players=''.join(f'<p>{html.escape(f["label"])} <audio controls preload="none" src="{html.escape(f["path"])}"></audio></p>' for f in r['files'])
        cards.append(f'<article><h2>{r["review_id"]}</h2><p>{html.escape(r["tape"])} CH{r["channel"]:02d}, {r["source_start_seconds"]:.1f}s; {r["sampling_kind"]}</p>{players}<p>Pitch stability, warble, transients, clarity, or no audible difference?</p></article>')
    (folder/'index.html').write_text('<!doctype html><html><head><meta charset="utf-8"><title>Apollo deflutter review</title><style>body{max-width:850px;margin:40px auto;font:18px system-ui;color:#20242c}article{border-top:1px solid #bbb;padding:20px 0}audio{vertical-align:middle;width:75%}</style></head><body><h1>Apollo deflutter: blinded research review</h1><p>Matched cached-denoised source, common unit gain. These are experimental comparisons, including numerical failures. Human review is pending. Record preference and artifacts by review ID and letter before opening the blinding key.</p>'+''.join(cards)+'</body></html>',encoding='utf-8')
    exposure(iteration,[dict(review_id=k['review_id'],source=k['source']) for k in keys],
        'rendered_cached_denoised_excerpts','Local listening queue; no listener accessed audio or supplied approval')
    finish(folder,dict(review_items=len(keys),audio_files=sum(len(r.get('files',[])) for r in queue),
        wall_seconds=time.perf_counter()-tick,artifact_bytes=sum(p.stat().st_size for p in folder.glob('*') if p.is_file()),
        listening_status='pending',human_listening_performed=False,model_audio_listening_performed=False),
        'listening_pending','Matched blinded research artifacts generated with exact full-map crops; no listening assessment has been performed.',
        'Continue independent motion/transition/held-out diagnostics. Human listener can review index.html and record judgments; this is not production approval.')


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);s=p.add_subparsers(dest='command',required=True)
    c=s.add_parser('coverage');c.add_argument('--catalog-iteration',default='I003')
    r=s.add_parser('render');r.add_argument('--iteration',default='I006_review');r.add_argument('--result-iteration',default='I005')
    a=p.parse_args()
    if a.command=='coverage':coverage(a.catalog_iteration)
    else:render(a.iteration,a.result_iteration)
