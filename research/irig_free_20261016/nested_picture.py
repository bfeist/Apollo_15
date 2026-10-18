"""Scientific comparison of actual audio and its second-stage modulation spectrum."""
from nested_amplitude import *
import os,sys
PLOT=ROOT/'reports/nested_amplitude_20261017'
os.environ['MPLCONFIGDIR']=str(PLOT/'mpl_config')
sys.path.append(str(PLOT/'plot_deps'))
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

def run():
    inv=json.loads((ROOT/'reports/canonical_bespoke_20261017/inventory.json').read_text());tape=next(x for x in inv if x['tape']=='A15_T510077');by={c['channel']:c for c in tape['channels']}
    start=19715.34;data={};curves={};sources=[]
    for ch in [13,23]:
        x,sr,m=get(by[ch],tape,start,60);sources.append(m);data[ch]=x;curves[ch]=estimate(x,sr)[1]
    filtered=signal.sosfiltfilt(signal.butter(3,[300,2100],fs=sr,btype='bandpass',output='sos'),data[13])
    starts=np.arange(6,47);scores=[np.std(filtered[int(s*sr):int((s+8)*sr)]) for s in starts];short=float(starts[np.argmax(scores)])
    fig=plt.figure(figsize=(19,15),layout=None,facecolor='#fafbfc');outer=fig.add_gridspec(2,2,left=.065,right=.91,top=.865,bottom=.115,hspace=.38,wspace=.19)
    plt.rcParams.update({'font.family':'DejaVu Sans','font.size':11,'axes.titlesize':12,'axes.labelsize':10})
    fig.text(.065,.965,'What the nested FFT actually sees',fontsize=26,weight='bold',color='#172b42')
    fig.text(.065,.934,'Real Apollo 15 audio • T510077 • short versus long observation • speech activity versus historical background',fontsize=13)
    fig.text(.065,.910,'Read each panel top → bottom: recorded sound frequencies → estimated fractional-frequency motion → modulation-rate spectrum.',fontsize=11,color='#34465a')
    records=[];saved={};axes=[]
    for row,ch in enumerate([13,23]):
        for col,duration in enumerate([8,48]):
            offset=short if duration==8 else 6.;lo=int(offset*sr);hi=int((offset+duration)*sr);x=data[ch][lo:hi];e=curves[ch][int(offset*200):int((offset+duration)*200)]
            t=np.arange(len(e))/200;f,st,z=signal.stft(x,sr,nperseg=320,noverlap=240,nfft=512,boundary=None)
            inner=outer[row,col].subgridspec(3,1,height_ratios=[1.25,.7,1.45],hspace=.72)
            ax=fig.add_subplot(inner[0]);axes.append(ax)
            title=('Speech-like activity' if row==0 else 'Background-dominant recording')+f'  |  {duration} seconds'
            label='COMCNTRL' if ch==13 else 'INCO'
            ax.set_title(title+f'\nCH{ch} {label} · {start+offset:.2f}–{start+offset+duration:.2f} s',loc='left',fontweight='bold',pad=12)
            im=ax.pcolormesh(st,f/1000,20*np.log10(abs(z)+1e-12),shading='auto',cmap='magma',vmin=-100,vmax=-35,rasterized=True)
            ax.set_ylim(.3,3.5);ax.set_xlim(0,duration);ax.set_ylabel('Audio frequency\n(kHz)');ax.set_xlabel('Time within selected window (s)')
            if duration==48:ax.axvspan(short-6,short+2,facecolor='none',edgecolor='#56d6f2',linewidth=1.6,linestyle='--')
            ax2=fig.add_subplot(inner[1]);ax2.plot(t,e*100,color='#276da4',linewidth=.6);ax2.axhline(0,color='#909aa5',linewidth=.6);ax2.set_xlim(0,duration);ax2.set_ylim(-2.5,2.5);ax2.set_ylabel('Motion estimate\n(%; uncalibrated)');ax2.set_xlabel('');ax2.grid(alpha=.2)
            ff,p=signal.periodogram(e*100,fs=200,window='hann',detrend='linear',scaling='density');sel=(ff>=4)&(ff<=10);peak=float(ff[sel][np.argmax(p[sel])]);maxpeak=float(p[sel].max())
            ax3=fig.add_subplot(inner[2]);ax3.semilogy(ff,p,color='#276da4',linewidth=1.5);ax3.axvspan(6.3,6.8,color='#e8a045',alpha=.22);ax3.axvline(6.5,color='#c17316',linestyle='--',linewidth=1)
            ax3.set_xlim(2,15);ax3.set_ylim(1e-7,1);ax3.set_ylabel('Motion PSD\n(%² / Hz; log scale)');ax3.set_xlabel('Modulation rate (Hz) — NOT audio pitch');ax3.grid(alpha=.2,which='major')
            ax3.set_title(f'Bin spacing {1/duration:.3f} Hz · Hann ENBW {1.5/duration:.3f} Hz\nLargest 4–10 Hz bin: {peak:.3f} Hz (not a significance test)',loc='left',fontsize=9,pad=8)
            rms=float(np.sqrt(np.mean(x*x)));zero=float(np.mean(x==0));records.append(dict(channel=ch,label=label,start=start+offset,duration=duration,peak_4_10_hz=peak,peak_psd_percent_squared_per_hz=maxpeak,rms=rms,exact_zero_fraction=zero,bin_spacing_hz=1/duration,hann_enbw_hz=1.5/duration,classification='speech-like activity' if ch==13 else 'background-dominant; faint speech not ruled out'))
            key=f'ch{ch}_{duration}';saved[key+'_motion']=e;saved[key+'_frequency']=ff;saved[key+'_psd']=p
    cb=fig.colorbar(im,cax=fig.add_axes([.925,.55,.012,.315]));cb.set_label('Audio spectrogram level (dBFS; same scale in all panels)',fontsize=10)
    fig.text(.065,.075,'Orange band: 6.3–6.8 Hz. Cyan box: short window inside the long one. Both rows use real recordings, not digital silence.',fontsize=11)
    fig.text(.065,.052,'Method: 40 ms spectra → signed log-frequency translation → 2–30 Hz motion trace → Hann-windowed second FFT. Known tone bands/lines excluded.',fontsize=10)
    fig.text(.065,.030,'Speech labels are spectrogram-based, not verified transcripts. Background may contain faint speech or residual lines. PSD discards phase; a peak alone is not a correction.',fontsize=10)
    fig.savefig(PLOT/'nested_fft_comparison.png',dpi=180,facecolor=fig.get_facecolor());fig.savefig(PLOT/'nested_fft_comparison.pdf',facecolor=fig.get_facecolor());plt.close(fig)
    np.savez_compressed(PLOT/'figure_data.npz',**saved)
    (PLOT/'figure_metadata.json').write_text(json.dumps(dict(sources=sources,panels=records,selection='CH13 maximum 8-second 300-2100 Hz RMS within offsets 6-54; CH23 same times; long windows offsets6-54',classification_limit='Spectral context only; no claim of verified absence of faint speech',amplitude_scale='one-sided Hann periodogram in percent-squared/Hz; no per-panel peak normalization'),indent=2))
    assert all(Path(m['path']).stat().st_size==m['bytes'] and Path(m['path']).stat().st_mtime_ns==m['mtime_ns'] for m in sources)
    print(json.dumps(records,indent=2));print('PNG',PLOT/'nested_fft_comparison.png')

if __name__=='__main__':run()
