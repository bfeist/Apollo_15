"""Bounded coded-clock availability probe, separate from correction scoring."""
import sys
sys.dont_write_bytecode=True
import time
from pathlib import Path
from deflutter_campaign import ROOT,begin,finish,read,write,load_excerpt,exposure,memory_peak
from irig_clock import legacy_decoder,decode_excerpt,legacy_hashes,SETTINGS


def run():
    scopes=[dict(tape='A15_T510073',channel=1,start=9480.,end=9492.,reason='Previously exposed development passage'),
            dict(tape='A15_T510103',channel=1,start=22500.,end=22512.,reason='Inside prior frozen I024 map support; code availability only')]
    folder=begin('I049_irig_availability','Original timing track contains structured progressing IRIG code, an independent availability lead beyond hum peaks',
        'Two metadata-predeclared12secondCH01 excerpts; unchanged legacy raw/highpass/bandpass xpolarity attempts; retain every decode including invalid fields',
        dict(available='At least4progressing BCD+SBS-consistent frames in one attempt; provisional recorded clock only',
             unavailable='No qualifying run stops this decoder/window; not proof of absent IRIG or all timecode',
             limits='No target/map/event-error evaluation, no recorded-clock basis or channel-transfer authentication',
             budget='24original source seconds,1GiB/120seconds,1worker'),scopes=scopes,decoder_settings=SETTINGS)
    started=time.perf_counter();decoder=legacy_decoder(Path('F:/_repos/Apollo_Audio_Processing'))
    write(folder/'dependency_manifest.json',legacy_hashes());rows=[];sources=[]
    for scope in scopes:
        inv=next(t for t in read(ROOT/'inventory.json')['tapes'] if t['tape']==scope['tape'])
        channel=next(c for c in inv['channels'] if c['channel']==scope['channel'])
        x,sr,source=load_excerpt(inv,channel,scope['start'],scope['end'],'original');sources.append(source)
        attempts=decode_excerpt(x,sr,round(scope['start']*sr),decoder)
        longest=max(a['longest_run'] for a in attempts)
        rows.append(dict(scope=scope,source=source,attempts=attempts,longest_sequential_run=longest,
            status='structured_code_available_provisional' if longest>=4 else 'no_qualifying_decode_this_window',
            limitations='Legacy AM nominal1kHz/SBS+year0 assumptions may reject historical BCD-only variants. No authenticated GMT/GET, oscillator stability, tape transport or cross-head transfer claim.'))
        write(folder/'results.json',rows);print(scope['tape'],longest,rows[-1]['status'],flush=True)
        if memory_peak()>2**30 or time.perf_counter()-started>120:raise RuntimeError('Declared availability probe budget exceeded')
    write(folder/'source_manifest.json',sources)
    exposure(folder.name,sources,'coded_clock_source_features','Structured code availability only, no correction-map or target timing-error outcomes')
    available=sum(r['longest_sequential_run']>=4 for r in rows)
    finish(folder,dict(windows=len(rows),available_windows=available,longest_runs=[r['longest_sequential_run'] for r in rows],
        wall_seconds=time.perf_counter()-started,peak_working_set_bytes=memory_peak(),new_target_frequency_outcomes_accessed=False),
        'coded_clock_availability_lead' if available else 'legacy_decode_unavailable_in_declared_windows',
        'All six fixed attempts/window retained. Code structure cannot alone establish historical transport or transfer to another head.',
        'If code available: characterize edge/carrier timing sensitivity and generator/mixing controls on development only; otherwise inspect historical-variant/AM availability before rejecting timecode.')


if __name__=='__main__':run()
