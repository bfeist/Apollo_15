"""Source-only availability of one metadata-bound delayed broadband pair."""
import time
import numpy as np
from scipy import signal
from deflutter_campaign import ROOT,begin,finish,read,write,load_excerpt,exposure,memory_peak


def fractional_shift(x,shift):
    # Cubic fixed-delay interpolation; fitting is restricted to A below.
    from scipy.interpolate import CubicSpline
    p=np.arange(len(x));return CubicSpline(p,x)(p+shift)


def normalized(a,b):
    a=a-np.mean(a);b=b-np.mean(b)
    den=np.linalg.norm(a)*np.linalg.norm(b)
    return float(a@b/den) if den>1e-12 else 0.


def run():
    # H=b_event-a_event from already exposed response-copy metadata. No
    # broadband waveform or alignment outcome informed these fixed choices.
    scope=dict(tape='A15_T510103',channels=[11,12],start=22500.,end=22580.,context=2.,H_seconds=-1.359,
        fit_interval=[4.,20.],held_out_interval=[32.,72.],delay_search_radius=.06)
    folder=begin('I054_broadband_copy_availability','A response-event delayed-copy lead also contains measurable broadband copied content',
        'Onefixedpair, originalsourceonly; Afitconstantlag/gain17tapFIR; Bnoalignmentrefit; identity/zero-lag/fixedwronglagscontrols',
        dict(available='Bcorrelation>=.7; prediction/target residualenergy<=.5; >=4/6Bsubbands correlation>=.5; Bcorrelation .1abovezero/wronglagcontrols',
             stop='Fail=>unavailable under bounded model; no pair/lag/filter/window rescue',
             limitation='No correction map or eventfrequency outcomes. Even real broadbandcopy needs pre-record physicaldelay provenance for transport leverage.',
             budget='2x84originalsource seconds,1GiB/120s,oneworker'),scope=scope,
        filter='Fixed finite4097tapKaiser8.6 response:350..1950Hz,0elsewhere; suppress60multiples±3Hzwith1Hztransitions; responsebandexcluded',
        model='17causal native8kHz taps after A-onlydelay; fixedridge.01traceGram/p; per-banddiagnostics neverchangefit')
    started=time.perf_counter();inv=next(i for i in read(ROOT/'inventory.json')['tapes'] if i['tape']==scope['tape']);inputs=[];sources=[]
    for c in scope['channels']:
        ch=next(v for v in inv['channels'] if v['channel']==c)
        x,sr,source=load_excerpt(inv,ch,scope['start']-2,scope['end']+2,'original')
        if sr!=8000:raise ValueError('Frozennative8kHzdomainrequired')
        if not np.isfinite(x).all() or np.any(abs(x)>=.999):raise ValueError('Originalclipping/nonfiniteinput')
        inputs.append(x);sources.append(source)
    result=measure_copy(inputs,sr,scope,folder/'frozen.json');result['source_manifest']=sources
    write(folder/'results.json',result);write(folder/'source_manifest.json',sources)
    exposure(folder.name,sources,'retrospective_broadband_copy_features','Onefixedpairsourceavailability;oldresponsefamilyexposed,nocorrectionoutcomes')
    finish(folder,dict(available=result['available'],Bcorrelation=result['Bcorrelation'],Bresidual_energy_ratio=result['Bresidual_energy_ratio'],checks=result['checks'],
        wall_seconds=time.perf_counter()-started,peak_working_set_bytes=memory_peak(),new_response_frequency_outcomes_accessed=False),
        'conditional_broadband_copy_available' if result['available'] else 'stop_broadband_copy_unavailable',
        'Newretrospectivesource-onlydiagnostic; prior frozenmaps remaininadmissible. Entirecopyfamilymustbeexcludedfromanynewmapfit;delayoriginunverified.',
        'Ifavailable:known-injectedwarp/stationarynullsensitivityfirst, then source-onlymapconditionaltest. Ifunavailable:stopthispairwithoutfurtherfitting.')


def measure_copy(inputs,sr,scope,freeze_path=None):
    f=np.arange(0,4001,dtype=float);gain=np.zeros(len(f));inside=(f>=350)&(f<=1950)
    nearest=abs(f-60*np.rint(f/60));gain[inside]=np.clip((nearest[inside]-3),0,1)
    fir=signal.firwin2(4097,f,gain,fs=8000,window=('kaiser',8.6))
    a,b=[signal.fftconvolve(x,fir,mode='same')[2*sr:-2*sr] for x in inputs]
    fit=slice(4*sr,20*sr);test=slice(32*sr,72*sr);H=scope['H_seconds'];center=round(H*sr);radius=round(.06*sr)
    # A-only lag profile via one FFT cross-correlation, excluding edge regions.
    segment=a[fit];around=b[4*sr+center-radius:20*sr+center+radius]
    dot=signal.correlate(around,segment,mode='valid',method='fft')
    energies=np.convolve(around*around,np.ones(len(segment)),mode='valid') if len(segment)<100 else signal.fftconvolve(around*around,np.ones(len(segment)),mode='valid')
    corr=dot/np.maximum(np.sqrt(np.sum(segment*segment)*np.maximum(energies,0)),1e-15)
    k=int(np.argmax(abs(corr)));lag=(center-radius+k)/sr;offset=0.
    if 0<k<len(corr)-1:
        q=abs(corr[k-1:k+2]);den=q[0]-2*q[1]+q[2]
        if den<0:offset=float(.5*(q[0]-q[2])/den)
    lag+=offset/sr
    aligned=fractional_shift(b,lag*sr)
    design=np.column_stack([np.roll(aligned,i) for i in range(17)])
    gram=design[fit].T@design[fit];ridge=.01*np.trace(gram)/17
    coeff=np.linalg.solve(gram+ridge*np.eye(17),design[fit].T@a[fit]);pred=design@coeff
    polarity=1 if np.dot(pred[fit],a[fit])>=0 else -1
    if freeze_path is not None:
        write(freeze_path,dict(scope=scope,lag_seconds=lag,Apeak_correlation=float(corr[k]),coefficients=coeff.tolist(),
            ridge_lambda=float(ridge),polarity=polarity,held_out_alignment_optimized=False))
    # Controls use the same fixed coefficients, never B fitting.
    control_rows=[]
    for shift in [0.,H-.311,H+.311]:
        control=fractional_shift(b,shift*sr);p=sum(coef*np.roll(control,i) for i,coef in enumerate(coeff))
        control_rows.append(dict(lag_seconds=shift,correlation=normalized(a[test],p[test])))
    bands=[]
    for low,high in zip(np.linspace(350,1950,7)[:-1],np.linspace(350,1950,7)[1:]):
        kernel=signal.firwin(513,[low,high],pass_zero=False,fs=sr,window=('kaiser',8.6))
        xa=signal.fftconvolve(a,kernel,mode='same');xp=signal.fftconvolve(pred,kernel,mode='same')
        bands.append(dict(low_hz=float(low),high_hz=float(high),correlation=normalized(xa[test],xp[test])))
    observed=normalized(a[test],pred[test]);ratio=float(np.sum((a[test]-pred[test])**2)/np.sum(a[test]**2))
    checks=dict(Bcorrelation=observed>=.7,Bresidual=ratio<=.5,broadbands=sum(x['correlation']>=.5 for x in bands)>=4,
        controls=all(observed>=r['correlation']+.1 for r in control_rows))
    return dict(scope=scope,lag_seconds=lag,Apeak_correlation=float(corr[k]),
        Bcorrelation=observed,Bresidual_energy_ratio=ratio,bands=bands,controls=control_rows,checks=checks,available=all(checks.values()),
        no_map_or_event_frequency_outcomes=True,physical_delay_origin='Unknown; code cannot certify pre-record delay',
        raw_input_versus_processed='Originalsread-only; finitefiltersinmemory; no audio restoration or perceptual judgment')


if __name__=='__main__':run()
