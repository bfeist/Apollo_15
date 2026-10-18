"""Raw/cleaned diagnostic pictures, with unsupported motion explicitly identified."""
from nested_cleanup_audit import *
os.environ['MPLCONFIGDIR']=str(OUTC/'mpl_config')
sys.path.append(str(ROOT/'reports/nested_amplitude_20261017/plot_deps'))
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

def phase_test(e,ref):
    # Evaluation-only reference coherence. Random phase rotations preserve per-block power.
    cs=[];rs=[]
    for lo in range(600,len(e)-1400,800):
        tt=np.arange(800)/200;a=np.column_stack([np.ones(800),tt-tt.mean(),np.cos(2*np.pi*6.5*tt),np.sin(2*np.pi*6.5*tt)])
        for trace,dest in [(e,cs),(ref,rs)]:
            c=np.linalg.lstsq(a,trace[lo:lo+800],rcond=None)[0];dest.append(complex(c[2],-c[3]))
    cs=np.array(cs);rs=np.array(rs);den=np.sum(abs(cs)**2)*np.sum(abs(rs)**2)
    stat=float(abs(np.sum(cs*np.conj(rs)))**2/max(den,1e-30));rng=np.random.default_rng(177)
    null=[float(abs(np.sum(cs*np.conj(rs)*np.exp(2j*np.pi*rng.random(len(cs)))))**2/max(den,1e-30)) for _ in range(1000)]
    return dict(blocks=len(cs),coherence=stat,phase_rotation_p=(1+sum(v>=stat for v in null))/1001,null95=float(np.percentile(null,95)),limitation='Phase association with held-out reference; not speech-only transport identification or calibration')

def main():
    d=np.load(OUTC/'data.npz');results=json.loads((OUTC/'results.json').read_text());approved=json.loads((ROOT/'reports/cyclic_flutter_20261017/listening.json').read_text());plt.rcParams.update({'font.size':10})
    for row in results:
        tag=f"{row['start']}_{row['channel']}";ref=d[f"{row['start']}_reference_evaluation_only"]
        for mode in ['raw','clean']:row[mode]['phase_test']=phase_test(d[tag+'_'+mode+'_motion'],ref)
    (OUTC/'results.json').write_text(json.dumps(results,indent=2))
    for start,ch in [(19715.34,15),(44595.55,15),(19715.34,23)]:
        rec=next(r for r in approved if r['window_start']==start);offset=rec['clip_offset'];duration=12;tag=f'{start}_{ch}';sr=8000
        fig=plt.figure(figsize=(16,13),facecolor='#fafbfc');gs=fig.add_gridspec(4,2,left=.08,right=.92,top=.86,bottom=.13,hspace=.72,wspace=.25,height_ratios=[.6,1.1,.8,1.3]);colours=['#6b7785','#1f6b9b']
        context='Approved speech excerpt — CH15 COMP_SUP' if ch==15 else 'Historical background control — CH23 INCO (speech not certified absent)'
        fig.text(.08,.963,'Does pipeline cleanup rescue the nested estimate?',fontsize=23,weight='bold')
        fig.text(.08,.933,context,fontsize=14);fig.text(.08,.910,f'T510077 · {start+offset:.3f}–{start+offset+12:.3f} s · identical source interval, sample rate and axis scales',fontsize=11)
        for col,mode in enumerate(['raw','clean']):
            x=d[tag+'_'+mode][round(offset*sr):round((offset+12)*sr)];e=d[tag+'_'+mode+'_motion'][round(offset*200):round((offset+12)*200)];t=np.arange(len(x))/sr;tt=np.arange(len(e))/200
            ax=fig.add_subplot(gs[0,col]);ax.plot(t[::8],x[::8],lw=.5,color=colours[col]);ax.set_xlim(0,12);ax.set_ylim(-.08,.08);ax.set_ylabel('Waveform\n(full scale)');ax.set_title('RAW concatenated audio' if mode=='raw' else 'CLEANED: stage 03 + stage 04',loc='left',fontweight='bold');ax.set_xlabel('Time in excerpt (s)')
            ax=fig.add_subplot(gs[1,col]);f,st,z=signal.stft(x,sr,nperseg=320,noverlap=240,nfft=512,boundary=None);im=ax.pcolormesh(st,f/1000,20*np.log10(abs(z)+1e-12),shading='auto',vmin=-100,vmax=-30,cmap='magma');ax.set_ylim(.3,3.5);ax.set_xlim(0,12);ax.set_ylabel('Audio frequency\n(kHz)');ax.set_xlabel('Time in excerpt (s)')
            ax=fig.add_subplot(gs[2,col]);ax.set_facecolor('#fff1ee');ax.plot(tt,e*100,color=colours[col],lw=.7);ax.set_xlim(0,12);ax.set_ylim(-4,4);ax.set_ylabel('Candidate motion\n(%; uncalibrated)');ax.set_title('NOT a validated flutter waveform — no trusted segments established',fontsize=9,loc='left');ax.set_xlabel('Time in excerpt (s)');ax.axhline(0,color='#b0a0a0',lw=.5)
            ax=fig.add_subplot(gs[3,col]);ff,p=signal.periodogram(e*100,200,window='hann',detrend='linear',scaling='density');ax.semilogy(ff,p,color=colours[col],lw=1);ax.axvspan(6.3,6.8,color='#e3a646',alpha=.25);ax.axvline(6.5,color='#ba781d',ls='--',lw=1);ax.set_xlim(2,15);ax.set_ylim(1e-7,2);ax.set_ylabel('Candidate motion PSD\n(%²/Hz; log scale)');ax.set_xlabel('Modulation rate (Hz), not audio pitch');ax.grid(alpha=.2);ax.set_title('Hann PSD · bin spacing 0.0833 Hz · ENBW 0.125 Hz',fontsize=9,loc='left')
        cb=fig.colorbar(im,cax=fig.add_axes([.94,.55,.012,.24]));cb.set_label('Spectrogram dBFS (shared scale)',fontsize=9)
        fig.text(.08,.083,'Cleanup: saved approved notch passes for CH15; current ≤6-pass detector for CH23; existing quiet-window denoiser capped at 6 dB.',fontsize=10)
        fig.text(.08,.058,'No time correction or normalization is applied here. The original CH30 timing reference is preserved separately for evaluation only.',fontsize=10)
        fig.text(.08,.033,'Red-tinted traces are unsupported candidates, not measured speech flutter. The highlighted 6.5 Hz region is a hypothesis, not a detection label.',fontsize=10)
        name=f'raw_vs_cleaned_{start:.2f}_CH{ch}.png';fig.savefig(OUTC/name,dpi=170);plt.close(fig)
    rows=json.loads((OUTC/'beep_traces.json').read_text());fig,axs=plt.subplots(2,3,figsize=(16,9));fig.subplots_adjust(left=.07,right=.98,top=.81,bottom=.18,hspace=.65,wspace=.28)
    fig.suptitle('Actual check-beep frequency movement: cleanup versus correction',fontsize=21,y=.97,weight='bold')
    fig.text(.07,.91,'Measured from held-out CH15 beeps in the two approved listening excerpts. These are tone measurements, not a speech-derived estimate.',fontsize=11)
    styles={'raw':('#89919a','--'),'clean':('#216b9a','-'),'approved_corrected':('#16815b','-')}
    for ri,row in enumerate(rows):
        for i in range(3):
            ax=axs[ri,i]
            if i>=len(row['events']):ax.axis('off');continue
            event=row['events'][i];traces=[tr for tr in row['traces'] if tr['event']==i]
            for tr in traces:
                c,ls=styles[tr['version']];label={'raw':'Raw','clean':'Noise-cleaned only','approved_corrected':'Approved tone-driven correction'}[tr['version']]
                ax.plot(np.array(tr['relative_time'])*1000,np.array(tr['frequency_hz'])-tr['center_hz'],color=c,ls=ls,lw=1.8,label=label)
            ax.set_xlim(0,300);ax.set_ylim(-150,150);ax.axvspan(0,40,color='#ddd',alpha=.5);ax.axvspan((event['end']-event['start']-.04)*1000,300,color='#ddd',alpha=.5);ax.axhline(0,color='#aaa',lw=.7);ax.grid(alpha=.2)
            ax.set_title(f"Window {row['start']:.2f} s · beep {i+1}\nRMS cleaned → corrected: {traces[1]['rms_hz']:.1f} → {traces[2]['rms_hz']:.1f} Hz",fontsize=11)
            ax.set_xlabel('Time from source beep start (ms)');ax.set_ylabel('Frequency minus local median (Hz)')
    handles,labels=axs[0,0].get_legend_handles_labels();fig.legend(handles,labels,loc='lower center',bbox_to_anchor=(.5,.075),ncol=3,fontsize=11)
    fig.text(.07,.045,'Gray edges: invalid/unmeasured segments (40 ms margins; mapped through the saved correction). Lines are measured phase derivatives, not fitted sine waves.',fontsize=10)
    fig.text(.07,.020,'A 6.5 Hz cycle lasts ~154 ms. Each short beep contains only ~1–2 cycles; it cannot identify the entire speech timing curve on its own.',fontsize=10)
    fig.savefig(OUTC/'heldout_beep_frequency_motion.png',dpi=180);plt.close(fig)
    print('Saved four comparison figures and phase-control metrics.')

if __name__=='__main__':main()
