"""Source-feature-only electrical clock audit on an exposed real passage."""
import os
for _key in ('OPENBLAS_NUM_THREADS','OMP_NUM_THREADS','MKL_NUM_THREADS'):
    os.environ[_key]='1'
import time
import numpy as np
from deflutter_campaign import ROOT,begin,finish,read,write,load_excerpt,exposure,memory_peak,stamp
from deflutter_response_clock_guard import electrical_clock_agreement
from deflutter_response_line_audit import remove_audited_clock_envelopes


def run():
    parent=ROOT/'iterations/I024_gate2';r=read(parent/'results.json')[0]
    inv=next(i for i in read(ROOT/'inventory.json')['tapes'] if i['tape']==r['tape']);roles=read(parent/'roles_0.json')['roles']
    folder=begin('I042_real_clock_survey','A different response-free original channel supplies a qualified electrical pilot for the exposed target',
        'Every available original channel with zero known response events, original clipping/context guards; fixed clock thresholds, then residual audit only for clock-qualified inputs',
        dict(scope='Clock/source features and response amplitude metadata only; no calibration loss/validation/final frequency error',budget='At most29channels x380seconds read,3GiB/15minutes,1worker; no source editing'),
        target=r['target_channel'],tape=r['tape'],passage=[r['start'],r['end']],clock_limits=dict(p95_radians=.001,maximum_radians=.005),selection='Full declared eligible set; no target-beep outcome ranking')
    tick=time.perf_counter();targetrow=next(c for c in inv['channels'] if c['channel']==r['target_channel'])
    target,sr,ts=load_excerpt(inv,targetrow,r['start']-40,r['end']+40,'original')
    if np.any(abs(target)>=.999):raise ValueError('Original target context clipped; no survey')
    events=[e for values in roles[str(r['target_channel'])].values() for e in values];rows=[]
    for channel in sorted(inv['channels'],key=lambda c:c['channel']):
        ch=channel['channel']
        row=dict(channel=ch)
        if ch==r['target_channel'] or any(roles.get(str(ch),{}).values()):
            row.update(status='metadata_ineligible',reason='Target itself or known response events');rows.append(row);continue
        if r['start']-40<channel['start_seconds'] or r['end']+40>channel['end_seconds']:
            row.update(status='context_unavailable');rows.append(row);continue
        pilot,psr,ps=load_excerpt(inv,channel,r['start']-40,r['end']+40,'original');row['source']=ps
        if psr!=sr or np.any(abs(pilot)>=.999):row.update(status='physical_input_withheld');rows.append(row);continue
        try:
            clock=electrical_clock_agreement(target,pilot,sr,events)
            row.update(status='clock_agreement_pass',clock=clock)
            try:
                _,model=remove_audited_clock_envelopes(target,pilot,sr,events)
                row.update(status='measurement_inputs_supported_development_only',model=model)
            except ValueError as exc:row.update(status='response_model_withheld',reason=str(exc),model_withhold=getattr(exc,'metadata',None))
        except ValueError as exc:row.update(status='clock_withheld',reason=str(exc),clock=getattr(exc,'metadata',None))
        rows.append(row);write(folder/'results.json',rows)
        write(folder/'phase.json',dict(at=stamp(),completed_channels=len(rows),channel=ch,elapsed_seconds=time.perf_counter()-tick,peak_bytes=memory_peak()))
        print(ch,row['status'],row.get('clock',{}).get('absolute_phase_residual95_radians'),flush=True)
        if memory_peak()>3*2**30 or time.perf_counter()-tick>900:raise RuntimeError('Declared bounded clock survey exceeded budget')
    write(folder/'results.json',rows)
    counts={s:sum(r['status']==s for r in rows) for s in sorted({r['status'] for r in rows})}
    best=min((r for r in rows if r.get('clock',{}).get('absolute_phase_residual95_radians') is not None),key=lambda r:r['clock']['absolute_phase_residual95_radians'],default=None)
    exposure(folder.name,[dict(source=ts,features='Low-band clock phase and existing event amplitudes; no beep-frequency error')],
        'source_feature_only_clock_survey','No map/candidate change, no validation/final frequency outcome or production qualification')
    finish(folder,dict(status_counts=counts,best_clock_channel=best['channel'] if best else None,best_clock=best.get('clock') if best else None,
        wall_seconds=time.perf_counter()-tick,peak_working_set_bytes=memory_peak(),new_real_frequency_outcomes_accessed=False,qualification_upgraded=False),
        'input_feature_survey_recorded','All eligible original pilots assessed under frozen thresholds; every withholding and unexamined reason retained. No timing correction evaluated.',
        'If any input qualifies: freeze source-only pilot-selection/binding on unused material. Otherwise diagnose physical clock differences as a separate hypothesis; never relax thresholds for coverage.')
    print(counts,flush=True)


if __name__=='__main__':run()
