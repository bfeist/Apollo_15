"""All remaining catalog-positive development passages, frozen decoder paths."""
import sys
sys.dont_write_bytecode=True
import time
from pathlib import Path
from deflutter_campaign import ROOT,begin,finish,read,write,load_excerpt,exposure,memory_peak
from deflutter_irig_projection_probe import stationary_projection
from irig_clock import legacy_decoder,decode_excerpt,legacy_hashes


def run():
    if not read(ROOT/'iterations/I052_irig_projection/metrics.json')['controls_passed']:raise ValueError('Preprocessing prerequisites failed')
    catalog=read(ROOT/'lines/timing_witness/catalog_availability.json')
    keys=sorted({(c['tape'],c['passage_start']) for c in catalog['candidates'] if c['channel']==1 and c['version']=='original'})
    previous={(r['scope']['tape'],r['scope']['start']) for r in read(ROOT/'iterations/I049_irig_availability/results.json')}
    scopes=[dict(tape=tape,channel=1,start=float(a),end=float(a+12)) for tape,a in keys if (tape,a) not in previous]
    folder=begin('I053_irig_catalog_followup','Persistent catalog-positive near1kHz carriers elsewhere in exposed development may contain code despite the two local nulls',
        'ALL remaining originalCH01 development passage keys from already-inspected catalog, fixed first12s; raw+same frozen stationary-comb paths, six legacy attempts each',
        dict(available='>=4valid progressing frames in any fixed attempt, followed by raw symbol/marker audit before stronger claims',
             stop='Finish declared catalog set, then stop this availability route if no code; no frequency/window/decoder retuning',
             budget='At most9x12source seconds,1GiB/180s,oneworker',restriction='Source features only; no response timing-error or correction-map access'),
        scopes=scopes,catalog_source='lines/timing_witness/catalog_availability.json',
        selection='No ranking on decoder outcomes; all9developmentpassagekeys, skip prior identicalwindow; spectralcarrierpresenceisnotcode')
    started=time.perf_counter();decoder=legacy_decoder(Path('F:/_repos/Apollo_Audio_Processing'))
    write(folder/'dependency_manifest.json',legacy_hashes());rows=[];sources=[]
    for scope in scopes:
        inv=next(i for i in read(ROOT/'inventory.json')['tapes'] if i['tape']==scope['tape'])
        channel=next(c for c in inv['channels'] if c['channel']==1)
        x,sr,source=load_excerpt(inv,channel,scope['start'],scope['end'],'original');sources.append(source)
        projected,projection=stationary_projection(x,sr);paths=[]
        for label,samples in [('raw',x),('stationary_comb',projected)]:
            attempts=decode_excerpt(samples,sr,round(scope['start']*sr),decoder)
            paths.append(dict(path=label,attempts=attempts,longest_run=max(a['longest_run'] for a in attempts)))
        longest=max(p['longest_run'] for p in paths)
        rows.append(dict(scope=scope,source=source,projection=projection,paths=paths,longest_run=longest,
            status='provisional_code_lead' if longest>=4 else 'no_decoded_code_under_frozen_paths'))
        write(folder/'results.json',rows);print(scope['tape'],scope['start'],longest,flush=True)
        if memory_peak()>2**30 or time.perf_counter()-started>180:raise RuntimeError('Declared catalog followup budget exceeded')
    write(folder/'source_manifest.json',sources)
    exposure(folder.name,sources,'catalog_selected_coded_clock_features','All fixed remainingdevelopmentkeys, no response outcomes or real qualification')
    available=sum(r['longest_run']>=4 for r in rows)
    finish(folder,dict(windows=len(rows),available_windows=available,source_seconds=sum(s['end']-s['start'] for s in scopes),
        wall_seconds=time.perf_counter()-started,peak_working_set_bytes=memory_peak(),new_target_frequency_outcomes_accessed=False),
        'code_lead_requires_raw_structure_audit' if available else 'stop_catalog_timecode_route',
        'Full metadata-frozen catalog set retained. Negative is decoder/path/window limited, not archive-wide absence or physical flutter impossibility.',
        'Audit raw code if available; otherwise synthesize unavailable independent witness routes and defer further source search until a distinct positive lead/provenance exists.')


if __name__=='__main__':run()
