"""Fresh source-only full-band comb-prefilter discovery experiment.

Original catalog is always retained. More peaks are not qualified references.
"""
import os
for _name in ('OPENBLAS_NUM_THREADS','OMP_NUM_THREADS','MKL_NUM_THREADS'):
    os.environ[_name]='1'
import time
import argparse
import numpy as np
from deflutter_campaign import ROOT,begin,finish,write,sha,memory_peak
from deflutter_near_mains_controls import cases
from deflutter_adaptive_mains import remove_adaptive_comb
from deflutter_catalog import discover_ridges,CatalogConfig


def matched(ridges,hz):
    # Tight known-center check avoids calling a ground-hum skirt 1-2Hz away
    # a recovered near-mains carrier. Do not change it after seeing outputs.
    return [r for r in ridges if r['resolution_seconds']==8 and abs(r['carrier_hz']-hz)<=.25
            and r['end_seconds']-r['start_seconds']>=.8*44]


def run(iteration='I021_prefilter_discovery',instrumented=False):
    chosen={0,1,8,9,16,17,24,25,32,33,40,41,5,21,29,45}
    folder=begin(iteration,
        'Source-only adaptive full-band comb subtraction before ranking reveals weak lines hidden by hum skirts',
        'Frozen raw catalog versus derived in-memory adaptive-original catalog; raw path preserved, no response-based selection',
        dict(easy='Report40dBfloat/PCM near/far strict known-center recovery; no tone-frequency input to prefilter',
             hard='60/80dB and PCM16 remain separate, no relaxed matching',
             controls='Hum-only retained ridges and false groups reported, zero correction claims',
             matching='8s resolution only,<=.25Hz known center,>=80%44s interval support; existing10s observed-center persistence'),
        synthetic=dict(generation='discovery-prefilter-v1',indices=sorted(chosen),sr=8000,core44s=True,context_each_side=40,
            instrumented=instrumented,peak_memory_ceiling_bytes=int(1.5*2**30) if instrumented else None,
            wall_time_ceiling_seconds=900 if instrumented else None,
            original_preserved=True,carrier_inputs_to_prefilter=False,comb_band='Full positive spectrum via center=sr/4,capture=sr/4',
            contamination_origin='Generated60.02Hz slowly moving digitization comb',
            response_outcomes_accessed=False,catalog_config=CatalogConfig().__dict__))
    rows=[];tick=time.perf_counter()
    for case in cases('discovery-prefilter-v1'):
        if case['index'] not in chosen:continue
        sr=case['sr'];source=case['reference_extended'];core=source[40*sr:84*sr]
        extraction_tick=time.perf_counter()
        def progress(stage):
            peak=memory_peak()
            write(folder/'phase.json',dict(case_index=case['index'],phase='full_comb_extraction',
                elapsed_seconds=time.perf_counter()-tick,peak_working_set_bytes=peak,**stage))
            print('stage',case['index'],stage['harmonic'],peak,flush=True)
            if peak>int(1.5*2**30):raise RuntimeError('Controlled discovery1.5GiB peak-memory ceiling reached; stop without alteration')
            if time.perf_counter()-tick>900:raise RuntimeError('Controlled discovery15minute wall-time ceiling reached')
        write(folder/'phase.json',dict(case_index=case['index'],phase='starting_comb_extraction'))
        derived,meta=remove_adaptive_comb(source,sr,sr/4,sr/4,progress=progress if instrumented else None)
        extraction_seconds=time.perf_counter()-extraction_tick
        outputs={}
        for version,audio in [('original',core),('adaptive_original',derived[40*sr:84*sr])]:
            ridges,accounting=discover_ridges(audio,sr)
            outputs[version]=dict(retained_groups=len(ridges),matched=matched(ridges,case['hz']) if case['kind']!='hum_only' else [],
                candidates=ridges,accounting=accounting,decoded_sha256=sha(audio.tobytes()))
        row=dict(index=case['index'],kind=case['kind'],hz=case['hz'],hum_to_carrier_db=case['hum_to_carrier_db'],pcm16=case['pcm16'],
            source_sha256=sha(source.tobytes()),extraction_seconds=extraction_seconds,extraction_metadata=meta,methods=outputs)
        rows.append(row);write(folder/'results.json',rows)
        write(folder/'phase.json',dict(case_index=case['index'],phase='case_committed',completed_cases=len(rows),
            peak_working_set_bytes=memory_peak(),elapsed_seconds=time.perf_counter()-tick))
        print(folder.name,case['index'],case['kind'],case['hum_to_carrier_db'],case['pcm16'],
              {name:(len(value['matched']),value['retained_groups']) for name,value in outputs.items()},flush=True)
        del derived,core,source,audio,ridges
    metrics=dict(cases=len(rows),positive_cases=sum(r['kind']!='hum_only' for r in rows),
        recovery={name:sum(bool(r['methods'][name]['matched']) for r in rows if r['kind']!='hum_only') for name in ('original','adaptive_original')},
        hum_only_groups={name:sum(r['methods'][name]['retained_groups'] for r in rows if r['kind']=='hum_only') for name in ('original','adaptive_original')},
        wall_seconds=time.perf_counter()-tick,peak_working_set_bytes=memory_peak(),qualified_corrections=0,
        source_only=True,response_outcomes_evaluated=False)
    finish(folder,metrics,'discovery_development_assessed',
        'Fresh full-band source-only catalog comparison complete; preserve all false groups and hard misses. A spectral match does not establish faithful motion or original oscillator identity.',
        'Assess exact line matches, unmatched groups and extraction cost. No real adaptive discovery gate until this component passes controls and a combined method is frozen on unused families.')


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--iteration',default='I021_prefilter_discovery');p.add_argument('--instrumented',action='store_true')
    a=p.parse_args();run(a.iteration,a.instrumented)
