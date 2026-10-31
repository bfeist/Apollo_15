"""Select additional development passages using check-event coordinates only.

This adapter exposes no saved correction outcome, selected reference, or score.
Historical final-event coordinates are already exposed v5 metadata, not a new
sealed audit. Entire new passages must lie in existing development block cores.
"""
import json
from collections import defaultdict
from deflutter_campaign import ROOT,SAVED,SEED,read,write,sha,exposure,stamp


def run(generation='v1',output_name='event_rich_development_passages.json',exclude_manifest=None,required_span=300,new_tapes=False):
    split=read(ROOT/'split_manifest.json');invs={i['tape']:i for i in read(ROOT/'inventory.json')['tapes']}
    roles={b['block']:b['role'] for b in split['blocks']}
    excluded={p['block'] for p in split['development_passages']}
    if exclude_manifest:
        from pathlib import Path
        excluded.update(p['block'] for p in read(Path(exclude_manifest))['passages'])
    tapes=sorted({p['tape'] for p in split['development_passages']})
    if new_tapes:
        old=set(tapes);available=sorted(k for k,v in invs.items() if 'channels' in v)
        strata=[available[i*len(available)//3:(i+1)*len(available)//3] for i in range(3)]
        tapes=[min((t for t in stratum if t not in old),key=lambda t:sha(f'{SEED}:{generation}:new-tape:{t}'.encode())) for stratum in strata]
    chosen=[];evidence=[];counts={}
    for tape in tapes:
        base=SAVED/tape/'plan';planraw=(base/'plan.json').read_bytes();plan=json.loads(planraw)
        path=base/plan['regions'][0]['detail_file'];raw=path.read_bytes()
        # Strict whitelist at the adapter boundary. Nothing about target
        # decisions, selected references or final response metrics is returned.
        metadata=json.loads(raw).get('final_holdouts',{})
        events={int(ch):[dict(start_seconds=float(e['start_seconds']),end_seconds=float(e['end_seconds']))
                         for e in es] for ch,es in metadata.items()}
        del metadata,raw,plan
        proposals={}
        inv=invs[tape];origin=inv['origin_filename_seconds']
        for ch,es in events.items():
            for e in es:
                start=max(0,int(e['start_seconds']//60)*60-120);end=start+300
                absolute=origin+start;block=int(absolute//1800)
                if block in excluded or roles.get(block)!='development':continue
                if not block*1800+360<=absolute or not absolute+required_span<=(block+1)*1800-360:continue
                if end>inv['duration_seconds']:continue
                n=sum(start+3<=x['start_seconds'] and x['end_seconds']<=end-3 for x in es)
                if n<2:continue
                proposals.setdefault(start,dict(tape=tape,start_seconds=start,duration_seconds=300.,
                    absolute_start=absolute,block=block,role='development',
                    prior_exposure='v5 final-event timing metadata; no saved response outcomes used',
                    dependence_group=f'filename_clock_block_{block}',availability_channels=[]))['availability_channels'].append(dict(channel=ch,historical_coordinate_count=n))
        counts[tape]=len(proposals)
        if proposals:
            selection_seed=SEED if generation=='v1' else SEED+':'+generation
            selected=min(proposals.values(),key=lambda r:sha(f'{selection_seed}:event-availability:{tape}:{r["start_seconds"]}'.encode()))
            selected['availability_channels']=list({r['channel']:r for r in selected['availability_channels']}.values())
            chosen.append(selected)
        evidence.append(dict(tape=tape,plan_sha256=sha(planraw),detail_path=str(path),
            fields_used=['final_holdouts.channel','final_holdouts.start_seconds','final_holdouts.end_seconds']))
    destination=ROOT/output_name
    if destination.exists():raise ValueError('Selection is already frozen')
    write(destination,dict(at=stamp(),generation=generation,selected_tapes=tapes,new_tape_metadata_stratification=new_tapes,passages=chosen,eligible_window_counts=counts,
        excluded_blocks=sorted(excluded),required_geometric_span_seconds=required_span,
        selection='Fixed SHA256 minimum among 300s windows with >=2 historical final-event coordinates in one channel; current development block core only; exclude six initial blocks',
        limitation='Timing availability conditional development sample, not representative archive coverage or untouched validation',
        saved_correction_outcomes_used=False,evidence=evidence))
    exposure('event_availability_selection',evidence,'historical_event_coordinates_only','Additional development passage availability; no correction scores or successes')
    print(json.dumps(dict(path=str(destination),passages=chosen,counts=counts)),flush=True)


if __name__=='__main__':
    import argparse
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--generation',default='v1');p.add_argument('--output-name',default='event_rich_development_passages.json');p.add_argument('--exclude-manifest');p.add_argument('--required-span',type=int,default=300);p.add_argument('--new-tapes',action='store_true')
    a=p.parse_args();run(a.generation,a.output_name,a.exclude_manifest,a.required_span,a.new_tapes)
