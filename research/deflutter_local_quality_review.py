"""Local NISQA perceptual-quality prediction, exploratory and never approval.

Use the existing Python3.10/PyTorch and isolated campaign dependencies. Model
weights are predownloaded; no network is permitted while audio is loaded.
"""
import os
for _name in ('OPENBLAS_NUM_THREADS','OMP_NUM_THREADS','MKL_NUM_THREADS','NUMBA_NUM_THREADS'):
    os.environ[_name]='1'
os.environ['HF_HUB_OFFLINE']='1';os.environ['TRANSFORMERS_OFFLINE']='1'
os.environ['PYTHONDONTWRITEBYTECODE']='1'
import argparse
import hashlib
import importlib.metadata
import json
from pathlib import Path
import shutil
import sys
import time
from datetime import datetime,timezone

REPO=Path(__file__).resolve().parents[1]
ROOT=REPO/'reports/deflutter_iterative_20261030'
MODEL=ROOT/'runtime_models/nisqa'


def stamp():return datetime.now(timezone.utc).isoformat()


def write(path,value):
    path=Path(path);part=path.with_name(path.name+f'.{os.getpid()}.partial')
    part.write_text(json.dumps(value,indent=2,allow_nan=False),encoding='utf-8');part.replace(path)


def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def run(review,wait_for_batch):
    folder=ROOT/'iterations'/review;out=folder/'nisqa_inspection';out.mkdir(exist_ok=False)
    if wait_for_batch:
        dependency=ROOT/'iterations/I024_batch/summary.json';start=time.monotonic()
        while not dependency.exists():
            write(out/'state.json',dict(at=stamp(),pid=os.getpid(),status='waiting_no_model_compute',
                dependency=str(dependency),elapsed_seconds=time.monotonic()-start))
            if time.monotonic()-start>7200:raise RuntimeError('Quality model waited2hours; do not assume main batch completed')
            time.sleep(5)
    deps=MODEL/'deps';weights=MODEL/'nisqa.tar';download=json.loads((MODEL/'download_manifest.json').read_text())
    if not weights.is_file() or sha(weights)!=download['sha256']:raise RuntimeError('Predownloaded official model identity differs; no network fallback')
    sys.path.insert(0,str(deps))
    cache=out/'runtime_cache';cache.mkdir()
    for name in ('TORCH_HOME','XDG_CACHE_HOME','NUMBA_CACHE_DIR','LIBROSA_CACHE_DIR','CUDA_CACHE_PATH'):
        os.environ[name]=str(cache)
    import torch
    import numpy as np
    import soundfile as sf
    import torchmetrics.functional.audio.nisqa as nisqa
    torch.set_num_threads(1);torch.set_num_interop_threads(1);torch.manual_seed(0)
    nisqa.NISQA_DIR=str(MODEL)
    def no_download():raise RuntimeError('Network downloads disabled during audio evaluation')
    nisqa._download_weights=no_download
    queue=json.loads((folder/'review_queue.json').read_text())
    manifest=dict(at=stamp(),pid=os.getpid(),python=sys.executable,
        versions={name:importlib.metadata.version(name) for name in ('torch','torchmetrics','librosa','numpy','scipy','soundfile')},
        model='NISQA v2.0 transmitted-speech multidimensional predictor',weights_sha256=download['sha256'],
        weights_source=download['url'],weights_path=str(weights),
        implementation_path=str(Path(nisqa.__file__)),implementation_sha256=sha(nisqa.__file__),script_sha256=sha(__file__),
        compute='CPU1thread,eval,no_grad',input='Original saved review WAVs, common unit gain, no normalization/resampling/cleanup rerun',
        model_output_order=['overall_quality','noisiness','discontinuity','coloration','loudness'],
        inference_network_disabled=True,audio_uploaded=False,blinding_key_read=False,
        score_use='Exploratory model perceptual-quality prediction; no acceptance threshold, human listening approval, WER or Apollo-domain calibration',
        documentation='https://github.com/gabrielmittag/NISQA')
    write(out/'manifest.json',manifest);shutil.copyfile(__file__,out/'implementation.py')
    write(ROOT/'active_jobs.json',dict(at=stamp(),jobs=[dict(iteration=review+'_NISQA',pid=os.getpid(),status='running')],
        analysis_in_progress='Local offline perceptual-quality prediction after serial campaign; no listening approval or method change'))
    rows=[];start=time.monotonic()
    for item in queue:
        if not item.get('review_id'):continue
        for clip in item['files']:
            path=folder/clip['path'];audio,sr=sf.read(path,dtype='float32')
            if audio.ndim!=1 or not np.isfinite(audio).all():raise ValueError('Review WAV must be finite mono')
            with torch.no_grad():
                values=nisqa.non_intrusive_speech_quality_assessment(torch.from_numpy(audio).unsqueeze(0),int(sr)).reshape(-1).tolist()
            if len(values)!=5 or not all(np.isfinite(values)):raise RuntimeError('Invalid model output; never repair/clip scores')
            row=dict(review_id=item['review_id'],label=clip['label'],source_sha256=sha(path),sample_rate=sr,frames=len(audio),
                scores=dict(zip(manifest['model_output_order'],values)))
            rows.append(row);write(out/'results.json',rows)
            write(out/'state.json',dict(at=stamp(),pid=os.getpid(),status='scoring',completed_clips=len(rows)))
            print('nisqa',item['review_id'],clip['label'],row['scores'],flush=True)
    write(out/'summary.json',dict(at=stamp(),clips=len(rows),wall_seconds=time.monotonic()-start,
        model_perceptual_quality_prediction_performed=True,subjective_human_listening=False,
        assistant_auditory_warble_rating=False,Apollo_domain_validated=False,production_accepted=False))
    write(out/'state.json',dict(at=stamp(),pid=os.getpid(),status='completed',completed_clips=len(rows)))
    write(ROOT/'active_jobs.json',dict(at=stamp(),jobs=[],analysis_in_progress='Interpret model quality predictions alongside frozen numeric and ASR evidence; no approval'))
    print('local_quality_finished',len(rows),flush=True)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--review',default='I016_review_local');p.add_argument('--wait-for-batch',action='store_true')
    a=p.parse_args();run(a.review,a.wait_for_batch)
