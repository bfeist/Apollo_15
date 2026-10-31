"""Freeze complete passage prefixes while later discovery continues."""
import argparse
import json
from deflutter_campaign import ROOT,read,write,sha,stamp,append


def run(source,destination,count,passages_file=None):
    src=ROOT/'iterations'/source;out=ROOT/'iterations'/destination
    from pathlib import Path
    passages=(read(Path(passages_file))['passages'] if passages_file else read(ROOT/'split_manifest.json')['development_passages'])[:count]
    coordinates={(p['tape'],p['start_seconds']) for p in passages}
    records=[r for r in read(src/'source_records.json') if (r['tape'],r['start']) in coordinates]
    invs={r['tape']:r for r in read(ROOT/'inventory.json')['tapes']}
    expected=sum(2*(len(invs[p['tape']]['channels'])+len(invs[p['tape']].get('unassigned_sources',[]))) for p in passages)
    if len(records)!=expected:raise ValueError(f'Passage prefix incomplete: {len(records)} != {expected}')
    keys={r['key'] for r in records}
    rows=[]
    # Only a later source can have an uncommitted last line. A malformed line
    # belonging to the frozen prefix is always fatal; never trim committed data.
    with (src/'catalog.jsonl').open(encoding='utf-8') as f:
        for line in f:
            try:r=json.loads(line)
            except json.JSONDecodeError:
                if any(key in line for key in keys):raise
                continue
            if r['source_key'] in keys:rows.append(r)
    if sum(r.get('ridge_count',0) for r in records)!=len(rows):raise ValueError('Committed catalog/source count mismatch')
    out.mkdir(parents=True,exist_ok=False)
    with (out/'catalog.jsonl').open('w',encoding='utf-8') as f:
        for r in rows:f.write(json.dumps(r)+'\n')
    write(out/'source_records.json',records)
    write(out/'metrics.json',dict(at=stamp(),kind='immutable completed passage prefix',parent=source,
        passages=passages,source_versions=len(records),hypotheses=len(rows),
        source_experiment_sha256=sha((src/'experiment.json').read_bytes()),
        catalog_sha256=sha((out/'catalog.jsonl').read_bytes()),target_outcomes_accessed=False))
    append(ROOT/'experiment_ledger.jsonl',dict(iteration=destination,outcome='frozen_discovery_prefix',
        reason='Parallelize downstream bounded qualification without reading unfinished discovery or target outcomes',artifacts=str(out)))
    print(out,len(records),len(rows),flush=True)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--source',default='I003');p.add_argument('--destination',default='I003_first4_frozen');p.add_argument('--count',type=int,default=4);p.add_argument('--passages-file')
    a=p.parse_args();run(a.source,a.destination,a.count,a.passages_file)
