"""Unseen-passage check of fixed wide-capture signed-FM correction."""
from cyclic_followup import *

def speech_check():
    sr=8000;t=np.arange(20*sr)/sr;tt=np.arange(4000)/200
    e=.025*np.sin(2*np.pi*6.55*t)+.006*np.sin(2*np.pi*13.1*t+.4)
    delay=cumulative_trapezoid(e,dx=1/sr,initial=0);delay-=np.linspace(delay[0],delay[-1],len(delay));tau=t+delay
    def voice(u):
        ph=2*np.pi*(125*u+2*np.sin(2*np.pi*.7*u));return sum(np.sin(k*ph)/k**.8 for k in range(1,20))*(.7+.2*np.sin(2*np.pi*2.3*u))
    ref=np.sin(2*np.pi*986.3*tau);warped=voice(tau);curve=wide(trajectory(ref,sr,1005.5),[2,30]);pos=positions(curve,sr,len(t));restored=resample_at(warped,pos)
    results={}
    for name,x in [('unwarped',voice(t)),('warped',warped),('restored',restored)]:
        tr=trajectory(x,sr,125.);fraction=np.gradient(tr[2])*200;freq,power=signal.welch(fraction[600:-600],200,nperseg=2000)
        results[name]=dict(natural_pitch_band_rms=float(np.sqrt(np.sum(power[(freq>.4)&(freq<1.)])*.1)),flutter_band_rms=float(np.sqrt(np.sum(power[(freq>5.5)&(freq<7.5)])*.1)),second_harmonic_rms=float(np.sqrt(np.sum(power[(freq>12)&(freq<14.5)])*.1)))
    # Report timing-origin error separately: waveform L2 without alignment confounds it with flutter.
    remaining=np.interp(pos/sr,t,tau)-t;keep=(t>3)&(t<17);linear=np.polyfit(t[keep],remaining[keep],1)
    results['timing']=dict(interior_offset_seconds=float(np.median(remaining[keep])),interior_linear_rate_error=float(linear[0]),residual_after_affine_seconds_rms=float(np.std(remaining[keep]-np.polyval(linear,t[keep]))))
    assert results['restored']['flutter_band_rms']<results['warped']['flutter_band_rms']*.1
    assert .9<results['restored']['natural_pitch_band_rms']/results['unwarped']['natural_pitch_band_rms']<1.1
    (DEST/'speech_synthetic.json').write_text(json.dumps(results,indent=2));print('SPEECH',results,flush=True)

def main():
    speech_check();tape=next(t for t in json.loads((OUT/'inventory.json').read_text()) if t['tape']=='A15_T510077');bych={c['channel']:c for c in tape['channels']};rows=[];search=[]
    plan=dict(fractions=[.13,.23,.43,.63,.73,.93],reference_channel=30,carrier_hz=1005.5,capture_hz=90.495,modulation_band=[2,30],max_event_windows=2,target_channels=[12,15,20],selection='At least two target events on one channel; freeze method before outcomes')
    (DEST/'fresh_plan.json').write_text(json.dumps(plan,indent=2))
    for fraction in plan['fractions']:
        start=round(fraction*(tape['duration']-60),2);targets={};events={};sources=[]
        for ch in plan['target_channels']:
            x,sr,m=get(bych[ch],tape,start,60);targets[ch]=x;events[ch]=beeps(x,sr);sources.append(m)
        search.append(dict(start=start,sources=sources,events=events))
        if max(map(len,events.values()))>=2:
            ref,sr,m=get(bych[30],tape,start,60);tr=trajectory(ref,sr,1005.5);curve=wide(tr,[2,30]);old=phase_map(ref,sr,1005.5,[4,10]);maps={'old':old,'wide':curve,'wrong_sign':-curve,'shift_71ms':np.roll(curve,14)}
            result=dict(start=start,reference=m,trajectory=tr[-1],channels=[])
            for ch in plan['target_channels']:
                result['channels'].append(dict(channel=ch,source=next(s for s in sources if s['channel']==ch),events=events[ch],results={name:fm_measure(targets[ch],sr,e,events[ch],tr[-1]['dominant_modulation_hz']) for name,e in maps.items()}))
            rows.append(result);np.savez_compressed(DEST/f'fresh_{start:.2f}_maps.npz',**maps)
            print('FRESH',start,[(ch,len(qs)) for ch,qs in events.items()],flush=True)
        (DEST/'fresh.json').write_text(json.dumps(dict(plan=plan,search=search,windows=rows),indent=2))
        if len(rows)>=2:break

if __name__=='__main__':main()
