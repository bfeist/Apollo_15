"""Paired exact-output regression and cost test of ridge-link optimization."""
import importlib.util
import sys
import time
from deflutter_campaign import ROOT,begin,finish,read,write,load_excerpt,sha,synthetic_cases,memory_peak
from deflutter_catalog import discover_ridges


def run():
    folder=begin('I002c','Sorted local frequency search preserves every ridge while reducing all-pairs work',
        'Frozen I002b detector versus exact-neighborhood implementation, identical inputs',
        'Bit-identical rows and accounting on all declared fixtures; no eligibility/threshold change',
        fixtures='First 16 seeded development synthetics plus original/denoised CH03 and CH14, first fixed 180s passage truncated to first 60s')
    path=ROOT/'iterations/I002b/implementation/research/deflutter_catalog.py'
    spec=importlib.util.spec_from_file_location('frozen_catalog_v2',path);old=importlib.util.module_from_spec(spec)
    sys.modules[spec.name]=old;spec.loader.exec_module(old)
    results=[]
    def compare(key,x,sr):
        tick=time.perf_counter();a=old.discover_ridges(x,sr);before=time.perf_counter()-tick
        tick=time.perf_counter();b=discover_ridges(x,sr);after=time.perf_counter()-tick
        results.append(dict(key=key,samples=len(x),sr=sr,source_sha256=sha(x.tobytes()),
            exact_equal=a==b,before_seconds=before,after_seconds=after,ridges=len(b[0])))
        print(key,results[-1],flush=True)
    for c in synthetic_cases(count=16):compare('synthetic_'+str(c['index']),c['x'],c['sr'])
    p=read(ROOT/'split_manifest.json')['development_passages'][0]
    inv=next(r for r in read(ROOT/'inventory.json')['tapes'] if r['tape']==p['tape'])
    for ch in (3,14):
        row=next(r for r in inv['channels'] if r['channel']==ch)
        for version in ('original','denoised'):
            x,sr,_=load_excerpt(inv,row,p['start_seconds'],p['start_seconds']+60,version)
            compare(f'{p["tape"]}_CH{ch}_{version}',x,sr)
    write(folder/'paired.json',results)
    metrics=dict(fixtures=len(results),all_exact_equal=all(r['exact_equal'] for r in results),
        before_seconds=sum(r['before_seconds'] for r in results),after_seconds=sum(r['after_seconds'] for r in results),
        peak_working_set_bytes=memory_peak())
    finish(folder,metrics,'retain_exact_optimization' if metrics['all_exact_equal'] else 'reject',
        f"Exact output match {sum(r['exact_equal'] for r in results)}/{len(results)}; paired cost {metrics['before_seconds']:.2f}s -> {metrics['after_seconds']:.2f}s. Cache effects do not establish archive throughput.",
        'Use frozen exact-neighborhood detector in a fresh event-rich development catalog; preserve original I003 outputs unchanged.')


if __name__=='__main__':run()
