"""Posthoc check of already-consumed V events on actual cached review audio.

No new candidate, refit, cleanup or reserved outcome. This is output assessment,
not a replacement qualification contract or another attempt on validation.
"""
import os
for _name in ('OPENBLAS_NUM_THREADS','OMP_NUM_THREADS','MKL_NUM_THREADS'):
    os.environ[_name]='1'
import numpy as np
from deflutter_campaign import ROOT,begin,finish,read,write,load_excerpt,sha,exposure
from flutter_tracking import evaluate_events


def summarize(rows):
    margins=[m for e in rows for m in e['margins']]
    return dict(events=len(rows),median_ratio=float(np.median([m['residual_ratio'] for m in margins])),
        worst_ratio=max(m['residual_ratio'] for m in margins),maximum_residual_hz=max(m['after_rms_hz'] for m in margins),
        minimum_before_hz=min(m['before_rms_hz'] for m in margins))


def run():
    parent=ROOT/'iterations/I016_gate1R';result=read(parent/'results.json')[0]
    events=[{k:v for k,v in e.items() if k!='margins'} for e in result['validation']]
    if any(e.get('role')!='validation' for e in events):raise ValueError('Only already-consumed V events may enter this diagnostic')
    folder=begin('I025_output_beeps','The frozen local map also reduces tone jitter in actual cached-denoised output',
        'Identity, exact frozen baseline positions, frozen challenger grid on one unchanged cached target; consumed validation coordinates only',
        dict(scope='Posthoc waveform assessment only; no fresh validation, final outcome, alternative selection or qualification upgrade'),
        parent_iteration='I016_gate1R',events=events,reserved_outcomes_accessed=False)
    inv=next(i for i in read(ROOT/'inventory.json')['tapes'] if i['tape']==result['tape'])
    row=next(r for r in inv['channels'] if r['channel']==result['target_channel'])
    x,sr,source=load_excerpt(inv,row,result['start'],result['end'],'denoised')
    target=parent/f"{result['tape']}_{result['start']}_CH{result['target_channel']:02d}"
    with np.load(target/'challenger_map.npz') as grid:
        positions=np.interp(np.arange(len(x))/sr,grid['nominal'],grid['tt'])*sr
    if sha(positions.tobytes())!=result['map_stats']['positions_sha256']:
        raise ValueError('Cached target frame/rate differs from frozen inverse map; do not reanchor')
    with np.load(target/'baseline_positions.npz') as grid:baseline=grid['positions']
    if len(baseline)!=len(x):raise ValueError('Saved baseline frame binding differs')
    outputs={}
    for name,p in [('identity',np.arange(len(x),dtype=float)),('baseline',baseline),('challenger',positions)]:
        measured=evaluate_events(x,sr,None,events,sample_positions=p)
        outputs[name]=dict(summary=summarize(measured),measurements=measured)
    write(folder/'source_manifest.json',source);write(folder/'results.json',outputs)
    exposure(folder.name,[dict(source=source,events=events)],'consumed_validation_on_cached_render_input',
        'Actual output diagnostic, exact frozen maps; not new qualification or a new model choice')
    finish(folder,dict(summaries={k:v['summary'] for k,v in outputs.items()},
        frozen_challenger_positions_sha256=sha(positions.tobytes()),reserved_outcomes_accessed=False,qualification_upgraded=False),
        'output_diagnostic_recorded','Measured the same frozen map on the actual cached-denoised rendering input; do not treat posthoc agreement as fresh evidence.',
        'Finish I024unchanged; keep raw-response and cached-output evidence distinct, plus perceptual/physical-source limitations.')
    print({k:v['summary'] for k,v in outputs.items()},flush=True)


if __name__=='__main__':run()
