"""Separate capture bandwidth, centering and modulation bandwidth; test transfer."""
from cyclic_diagnose import *
from scipy.integrate import cumulative_trapezoid

def wide(trace,band):return bound(signal.sosfiltfilt(signal.butter(3,band,fs=200,btype='bandpass',output='sos'),trace[1]))

def tests():
    sr=8000;t=np.arange(20*sr)/sr;tt=np.arange(4000)/200;rows=[]
    for centre in [1005.5,986.3]:
        for depth in [.025,.04]:
            truth=depth*np.sin(2*np.pi*6.55*t)+.006*np.sin(2*np.pi*13.1*t+.4);delay=cumulative_trapezoid(truth,dx=1/sr,initial=0);delay-=np.linspace(delay[0],delay[-1],len(delay));tau=t+delay
            ref=np.sin(2*np.pi*centre*tau);trace=trajectory(ref,sr,1005.5)
            maps={'old':phase_map(ref,sr,1005.5,[4,10]),'old_recentered':phase_map(ref,sr,centre,[4,10]),'broad_capture_4_10':wide(trace,[4,10]),'broad_capture_2_30':wide(trace,[2,30])}
            pulse=np.sin(2*np.pi*2500*tau)*((tau>=8)&(tau<=8.25));event=dict(start=float(np.searchsorted(tau,8)/sr),end=float(np.searchsorted(tau,8.25)/sr));truth200=np.interp(tt,t,truth);keep=(tt>3)&(tt<17)
            # Speech-like signal retains slow natural pitch changes; only planted speed modulation is removed.
            def speech(u):
                ph=2*np.pi*(125*u+2*np.sin(2*np.pi*.7*u))
                return sum(np.sin(k*ph)/(k**.8) for k in range(1,20))*(.7+.2*np.sin(2*np.pi*2.3*u))
            base=speech(t);warped=speech(tau);rawerr=float(np.std(warped[3*sr:17*sr]-base[3*sr:17*sr]))
            for name,e in maps.items():
                co=np.mean(e[keep]*np.exp(-2j*np.pi*6.55*tt[keep]));ct=np.mean(truth200[keep]*np.exp(-2j*np.pi*6.55*tt[keep]));metric=fm_measure(pulse,sr,e,[event],6.55)[0]['margins']['0.04'];restored=resample_at(warped,positions(e,sr,len(warped)));err=float(np.std(restored[3*sr:17*sr]-base[3*sr:17*sr]))
                rows.append(dict(centre=centre,configured_carrier=1005.5,depth=depth,method=name,fundamental_gain=float(abs(co/ct)),fundamental_phase_degrees=float(np.angle(co/ct,deg=True)),short_gated_tone=metric,planted_speech_error_before=rawerr,planted_speech_error_after=err))
    # Known 10 ms measurement lag, explicit sign and alignment controls.
    e=.025*np.sin(2*np.pi*6.5*tt);truth=.025*np.sin(2*np.pi*6.5*t);tau=t+cumulative_trapezoid(truth,dx=1/sr,initial=0);target=np.sin(2*np.pi*2500*tau)
    lagtests={name:fm_measure(target,sr,curve,[dict(start=8,end=8.25)],6.5)[0]['margins']['0.04'] for name,curve in {'correct':e,'wrong_sign':-e,'10ms_delayed':.025*np.sin(2*np.pi*6.5*(tt-.01)),'known_lag_compensated':.025*np.sin(2*np.pi*6.5*(tt-.01+.01))}.items()}
    (DEST/'capture_gain_tests.json').write_text(json.dumps(dict(cases=rows,lag_tests=lagtests),indent=2));print('GAIN/PHASE TESTS',len(rows),flush=True)

def run():
    inventory={t['tape']:t for t in json.loads((OUT/'inventory.json').read_text())}
    plan=[('A15_T510077',19715.34,30,1005.5),('A15_T510077',32710.81,30,1005.5),('A15_T510077',16729.21,30,1005.5),('A15_T510077',24623.89,30,1005.5),('A15_T510075',28663.35,30,1000.),('A15_T510075',36831.3,30,1000.),('A15_T510076',11311.58,30,1000.),('A15_T510076',24612.63,30,1000.),('A15_T510073',6782.31,3,117.5),('A15_T510073',44772.75,3,117.5)]
    (DEST/'followup_plan.json').write_text(json.dumps(plan,indent=2));rows=[]
    for tapename,start,ch,hz in plan:
        tape=inventory[tapename];bych={c['channel']:c for c in tape['channels']};ref,sr,m=get(bych[ch],tape,start,60);trace=trajectory(ref,sr,hz);narrow=trajectory(ref,sr,hz,min(80,max(40,hz*.05)))
        maps={'old_4_10':phase_map(ref,sr,hz,[4,10]),'old_recentered_4_10':phase_map(ref,sr,trace[-1]['centre_hz'],[4,10]),'narrow_capture_2_30':wide(narrow,[2,30]),'broad_capture_4_10':wide(trace,[4,10]),'broad_capture_2_30':wide(trace,[2,30])}
        maps['broad_wrong_sign']=-maps['broad_capture_2_30'];maps['broad_shift_71ms']=np.roll(maps['broad_capture_2_30'],14)
        result=dict(tape=tapename,start=start,reference=m,trajectory=trace[-1],channels=[])
        for targetch in [12,15,20]:
            x,sr,tm=get(bych[targetch],tape,start,60);events=beeps(x,sr)
            metrics={name:fm_measure(x,sr,e,events,trace[-1]['dominant_modulation_hz']) for name,e in maps.items()}
            result['channels'].append(dict(channel=targetch,source=tm,events=events,results=metrics))
        tag=f'{tapename}_{start:.2f}';np.savez_compressed(DEST/(tag+'_maps.npz'),**maps,reference_error=trace[1],reference_displacement=trace[2],local_modulation_rate=trace[4])
        rows.append(result);(DEST/'followup.json').write_text(json.dumps(rows,indent=2))
        print('FACTORIAL',tapename,start,'centre',round(trace[-1]['centre_hz'],2),'mod',trace[-1]['rate_percentiles_hz'],'events',[(c['channel'],len(c['events'])) for c in result['channels']],flush=True)

if __name__=='__main__':tests();run()
