"""Inspect local review waveforms/spectra; never claim perceptual listening."""
import os
for _name in ('OPENBLAS_NUM_THREADS','OMP_NUM_THREADS','MKL_NUM_THREADS'):
    os.environ[_name]='1'
import argparse
import json
from pathlib import Path
import shutil
import subprocess
import time
import numpy as np
import soundfile as sf
from scipy import signal
from deflutter_campaign import ROOT,read,write,sha,exposure


def run(iteration):
    folder=ROOT/'iterations'/iteration;queue=read(folder/'review_queue.json')
    out=folder/'signal_inspection';out.mkdir(exist_ok=False)
    records=[];ffmpeg=shutil.which('ffmpeg')
    for item in queue:
        if not item.get('review_id'):continue
        images=[]
        for clip in item['files']:
            path=folder/clip['path'];x,sr=sf.read(path,dtype='float64')
            f,t,z=signal.stft(x,sr,nperseg=256,noverlap=192,boundary=None)
            power=abs(z)**2
            total=float(power.sum());p=power.sum(axis=1)
            flux=np.sqrt(np.sum(np.diff(abs(z),axis=1)**2,axis=0))
            row=dict(review_id=item['review_id'],label=clip['label'],source_sha256=sha(path.read_bytes()),
                frames=len(x),sr=sr,peak=float(np.max(abs(x))),rms=float(np.sqrt(np.mean(x*x))),
                clipping_samples=int(np.sum(abs(x)>=1)),dc=float(np.mean(x)),
                difference_p99_9=float(np.percentile(abs(np.diff(x)),99.9)),
                max_sample_step=float(np.max(abs(np.diff(x)))),
                spectral_flux_p99=float(np.percentile(flux,99)),spectral_flux_max=float(flux.max()),
                band_energy_fractions={str(a)+'_'+str(b):float(p[(f>=a)&(f<b)].sum()/max(total,1e-30))
                    for a,b in ((0,300),(300,1000),(1000,2150),(2150,2850),(2850,4001))},
                assessment_type='Signal measurements and visual spectrogram only; no listening or intelligibility judgment')
            records.append(row)
            if ffmpeg and item['sampling_kind']=='worst_validation':
                picture=out/(path.stem+'_spectrum.png')
                cmd=[ffmpeg,'-hide_banner','-loglevel','error','-threads','1','-filter_threads','1','-i',str(path),
                    '-lavfi','showspectrumpic=s=1000x420:legend=1:scale=log:drange=90:limit=0:color=viridis:win_func=hann',
                    '-frames:v','1','-threads','1',str(picture)]
                subprocess.run(cmd,check=True,capture_output=True)
                images.append(picture)
        if images:
            combined=out/(item['review_id']+'_ABC_spectra.png')
            command=[ffmpeg,'-hide_banner','-loglevel','error','-threads','1','-filter_complex_threads','1']
            for p in images:command+=['-i',str(p)]
            command+=['-filter_complex',f'vstack=inputs={len(images)}','-frames:v','1','-threads','1',str(combined)]
            subprocess.run(command,check=True,capture_output=True)
    write(out/'blind_signal_metrics.json',records)
    write(out/'inspection_scope.json',dict(available_audio_listening_tools=[],
        installed_python_audio_perceptual_packages=False,ffmpeg=ffmpeg,
        assessed=['decoded output integrity','clipping','sample steps','spectral energy','spectral flux','fixed-scale spectrograms'],
        not_assessed=['human listening','model audio listening','speech intelligibility','perceived warble'],
        no_audio_uploaded=True))
    exposure(iteration,[dict(review_id=r['review_id'],label=r['label'],sha256=r['source_sha256']) for r in records],
        'rendered_output_waveform_and_spectrum','Local signal diagnostics; no perceptual listening capability exposed')
    print(out,len(records),flush=True)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--iteration',default='I006_review_initial');a=p.parse_args();run(a.iteration)
