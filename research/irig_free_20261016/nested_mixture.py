"""Test whether stationary additive noise can explain mismatched warp sensitivity."""
from nested_amplitude import *

def run():
    sr=8000;t=np.arange(24*sr)/sr;truth=.025*np.sin(2*np.pi*6.5*t+.4);tau=t+cumulative_trapezoid(truth,t,initial=0)
    probe=.0025*np.sin(2*np.pi*6.5*t+1.9);probe_tau=t+cumulative_trapezoid(probe,t,initial=0);rows=[]
    for snr in [20,0,-10,-20]:
        for kind in ['all_content_warped','speech_warped_noise_stationary']:
            curves=[];differences=[];active=[]
            for seed in range(1,5):
                s=speech(seed);rng=np.random.default_rng(seed+400)
                noise=signal.sosfiltfilt(signal.butter(3,[300,3400],fs=sr,btype='bandpass',output='sos'),rng.normal(size=len(t)))
                noise*=np.std(s)/np.std(noise)*10**(-snr/20)
                x=np.interp(tau,t,s+noise) if kind=='all_content_warped' else np.interp(tau,t,s)+noise
                tt,e,a,mask,m=tracker(x,sr);curves.append(e);active.append(a)
                y=np.interp(probe_tau,t,x);_,ee,_,_,_=tracker(y,sr)
                differences.append(ee-np.interp(np.interp(tt,t,probe_tau),tt,e))
            true=np.interp(tt,t,np.log1p(truth));pr=np.interp(tt,t,np.log1p(probe))
            row=dict(snr_db=snr,kind=kind,actual=measure(np.mean(curves,axis=0),true,tt,6.5),injected=measure(np.mean(differences,axis=0),pr,tt,6.5));rows.append(row)
            print('MIXTURE',snr,kind,'actual',round(row['actual']['gain'],3),'injected',round(row['injected']['gain'],3),flush=True)
    (OUT/'mixture.json').write_text(json.dumps(rows,indent=2))

if __name__=='__main__':run()
