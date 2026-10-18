"""Bounded search for validation events in additional windows of surveyed fragments."""
from survey import *
OUT=ROOT/'reports/irig_free_research_20261016'

def main():
    rows=json.loads((OUT/'summary.json').read_text())
    saved=OUT/'additional_event_search.json'
    out=json.loads(saved.read_text()) if saved.exists() else []
    seen={r['tape'] for r in out}
    for r in rows:
        if r['tape'] in seen or 'sources' not in r:continue
        seen.add(r['tape']);bych={m['channel']:m for m in r['sources']}
        rec=dict(tape=r['tape'],group=r['group'],scans=[],events=[])
        # Upper and lower head channel examples; no adaptation of estimator to target.
        for start in [200,350,500,650,950,1100,1250,1400,1550]:
            for ch in [12,15]:
                if ch not in bych:continue
                m=bych[ch]
                if start+50>m['frames']/m['sample_rate']:continue
                x,sr,meta=read(Path(m['path']),start,50);qs=beeps(x,sr)
                rec['scans'].append(dict(source=meta,channel=ch,count=len(qs)))
                if qs:rec['events'].append(dict(channel=ch,start=start,bursts=qs))
            if len({q['start'] for q in rec['events']})>=2:break
        out.append(rec);(OUT/'additional_event_search.json').write_text(json.dumps(out,indent=2))
        print(r['tape'],'extra scans',len(rec['scans']),'event windows',[(e['channel'],e['start'],len(e['bursts'])) for e in rec['events']],flush=True)

if __name__=='__main__':main()
