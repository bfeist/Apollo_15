"""Reproduce I020 exact source-derived map rejection without target outcomes."""
import os
for _name in ('OPENBLAS_NUM_THREADS','OMP_NUM_THREADS','MKL_NUM_THREADS'):
    os.environ[_name]='1'
import argparse
import numpy as np
from deflutter_campaign import ROOT,read,load_excerpt,sha
from deflutter_map_preflight import preflight
from flutter_tracking import RATE,trajectory
from continuous_tracking import track_array


def run(parent='I016_gate0'):
    folder=ROOT/'iterations'/parent
    marker_path=next(folder.glob('*validation_consumed.json'));selected=read(marker_path)['selected']
    parts=marker_path.name.split('_');tape='_'.join(parts[:2]);start=float(parts[2])
    experiment=read(folder/'experiment.json')
    passage=next(p for p in experiment['passages'] if p['tape']==tape and p['start_seconds']==start)
    proposal=read(folder/'acquisition_0.json')['proposal']
    inv=next(i for i in read(ROOT/'inventory.json')['tapes'] if i['tape']==tape)
    row=next(r for r in inv['channels'] if r['channel']==selected['reference_channel'])
    if selected['extraction_version']!='original':raise ValueError('This bounded replay explicitly supports original extraction only')
    x,sr,source=load_excerpt(inv,row,start,start+passage['duration_seconds'],'original')
    if source['decoded_sha256']!=selected['source']['decoded_sha256']:raise ValueError('Source identity changed')
    trace=trajectory(x,sr,selected['carrier_hz'],selected['capture_hz'])
    curve,support=track_array(x,sr,selected['carrier_hz'],selected['capture_hz'],selected['amplitude_reference'])
    times=np.arange(len(support))/RATE
    support*=((times>=proposal['start_seconds'])&(times<proposal['end_seconds']))
    result=preflight(curve,support,trace[3],sr,len(x))
    result['frozen_main_map_hash_matches']=result['maps']['main']['positions_sha256']==selected['map_stats']['positions_sha256']
    if not result['frozen_main_map_hash_matches']:raise ValueError('Exact frozen main map was not reproduced')
    print(result)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--parent',default='I016_gate0')
    run(p.parse_args().parent)
