"""Reproducible signed-motion, AM and inverse-render controls over several rates."""
from bespoke_expanded import *

def main():
    sr=8000;t=np.arange(20*sr)/sr;rows=[]
    for kind,fm in [('warp',3.),('warp',6.5),('warp',11.),('drift',6.5),('AM',6.5),('stationary',6.5)]:
        phase=2*np.pi*(fm*t if kind!='drift' else 5*t+.075*t*t)
        truth=.025*np.sin(phase+.3)+.005*np.sin(2*phase) if kind in ['warp','drift'] else np.zeros(len(t))
        tau=t+cumulative_trapezoid(truth,dx=1/sr,initial=0)
        ref=np.sin(2*np.pi*997*tau)+.2*np.sin(2*np.pi*600*t)
        if kind=='AM':ref*=1+.65*np.sin(phase)
        target=np.sin(2*np.pi*2500*tau)
        for mode in ['broad','narrow']:
            e,m=reference(ref,sr,1005.5,mode);tt=np.arange(len(e))/200;tr=np.interp(tt,t,truth);keep=(tt>3)&(tt<17)
            gain=float(np.dot(e[keep],tr[keep])/max(np.dot(tr[keep],tr[keep]),1e-20)) if np.any(tr) else None
            ph=np.interp(tt,t,phase);a=2*np.mean(e[keep]*np.exp(-1j*ph[keep]));b=2*np.mean(tr[keep]*np.exp(-1j*ph[keep]))
            ev=[dict(start=8,end=8.25)];good=fm_measure(target,sr,e,ev,m['rate_hz'])[0];bad=fm_measure(target,sr,-e,ev,m['rate_hz'])[0]
            if np.any(tr):assert good['margins']['0.04']['after']['rms_hz']<bad['margins']['0.04']['after']['rms_hz']
            else:assert np.std(e[keep])<1e-5
            rows.append(dict(kind=kind,injected_hz=fm,mode=mode,estimated_hz=m['rate_hz'],signed_gain=gain,phase_degrees=float(np.angle(a/b,deg=True)) if np.any(tr) else None,estimated_speed_rms=float(np.std(e[keep])),correct=good,wrong_sign=bad))
    (DEST/'synthetic_verified.json').write_text(json.dumps(rows,indent=2,allow_nan=False));print('Verified',len(rows),'signed and negative-control cases',flush=True)

if __name__=='__main__':main()
