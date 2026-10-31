"""Offline cached-ASR inspection of blinded saved review WAVs, not listening approval.

Uses an existing separate Python installation. No model download, alignment,
source upload, global package write, or canonical audio change is permitted.
"""
import os
for _name in ('OPENBLAS_NUM_THREADS','OMP_NUM_THREADS','MKL_NUM_THREADS'):
    os.environ[_name]='1'
os.environ['HF_HUB_OFFLINE']='1';os.environ['TRANSFORMERS_OFFLINE']='1'
os.environ['PYTHONDONTWRITEBYTECODE']='1'
import argparse
import hashlib
import importlib.metadata
import json
from pathlib import Path
import re
import shutil
import site
import sys
import time
from datetime import datetime,timezone

REPO=Path(__file__).resolve().parents[1]
ROOT=REPO/'reports/deflutter_iterative_20261030'
MODEL=Path(r'C:\Users\Feist\.cache\huggingface\hub\models--Systran--faster-whisper-large-v3\snapshots\edaa852ec7e145841d8ffdb056a99866b5f0a478')


def stamp():return datetime.now(timezone.utc).isoformat()


def write(path,value):
    path=Path(path);temp=path.with_name(path.name+f'.{os.getpid()}.partial')
    temp.write_text(json.dumps(value,indent=2,allow_nan=False),encoding='utf-8');temp.replace(path)


def digest(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as stream:
        while data:=stream.read(8*2**20):h.update(data)
    return h.hexdigest()


def distance(a,b):
    row=list(range(len(b)+1))
    for i,x in enumerate(a,1):
        nextrow=[i]
        for j,y in enumerate(b,1):nextrow.append(min(nextrow[-1]+1,row[j]+1,row[j-1]+(x!=y)))
        row=nextrow
    return row[-1]


def run(review,device):
    folder=ROOT/'iterations'/review;out=folder/'asr_inspection';out.mkdir(exist_ok=False)
    cache=out/'runtime_cache';cache.mkdir()
    for name in ('CUDA_CACHE_PATH','TORCH_HOME','XDG_CACHE_HOME','NUMBA_CACHE_DIR'):
        os.environ[name]=str(cache)
    # Existing CUDA dependency directories only; no install or global PATH edit.
    dll_handles=[]
    for base in site.getsitepackages():
        for suffix in ('torch/lib','nvidia/cublas/bin','nvidia/cudnn/bin'):
            path=Path(base)/suffix
            if path.is_dir():dll_handles.append(os.add_dll_directory(str(path)))
    import ctranslate2
    from faster_whisper import WhisperModel
    if not MODEL.is_dir() or not (MODEL/'model.bin').is_file():raise RuntimeError('Cached model missing; no download allowed')
    queue=json.loads((folder/'review_queue.json').read_text());rows=[]
    options=dict(language='en',task='transcribe',beam_size=5,best_of=5,temperature=0.,
        condition_on_previous_text=False,vad_filter=False,word_timestamps=False,
        no_speech_threshold=.6,log_prob_threshold=-1.,compression_ratio_threshold=2.4)
    compute='float16' if device=='cuda' else 'int8'
    manifest=dict(at=stamp(),pid=os.getpid(),python=sys.executable,python_version=sys.version,
        versions={name:importlib.metadata.version(name) for name in ('faster-whisper','ctranslate2','soundfile')},
        model_path=str(MODEL),model_revision=MODEL.name,device=device,compute_type=compute,cpu_threads=1,num_workers=1,
        options=options,offline=True,no_source_upload=True,alignment=False,source_audio_changed=False,
        blind=True,blinding_key_opened_by_this_program=False,ground_truth_transcript_available=False,
        scope='Automatic audio recognition and pairwise transcript differences only; no human listening, perceived-warble or calibrated intelligibility judgment',
        script_sha256=digest(__file__),model_files={p.name:dict(bytes=p.stat().st_size,sha256=digest(p)) for p in MODEL.iterdir() if p.is_file()})
    write(out/'manifest.json',manifest);shutil.copyfile(__file__,out/'implementation.py')
    write(ROOT/'active_jobs.json',dict(at=stamp(),jobs=[dict(iteration=review+'_offline_asr',pid=os.getpid(),status='running')],
        analysis_in_progress='Offline cached large-v3 ASR on blinded matched review clips; no perceptual approval'))
    print('load_cached_model',device,compute,flush=True);tick=time.perf_counter()
    model=WhisperModel(str(MODEL),device=device,compute_type=compute,cpu_threads=1,num_workers=1,local_files_only=True)
    for item in queue:
        if not item.get('review_id'):continue
        for clip in item['files']:
            path=folder/clip['path'];start=time.perf_counter()
            segments,info=model.transcribe(str(path),**options)
            values=[dict(start=s.start,end=s.end,text=s.text,avg_logprob=s.avg_logprob,
                no_speech_prob=s.no_speech_prob,compression_ratio=s.compression_ratio) for s in segments]
            text=' '.join(s['text'].strip() for s in values).strip()
            rows.append(dict(review_id=item['review_id'],label=clip['label'],sampling_kind=item['sampling_kind'],
                file=str(path),source_sha256=digest(path),text=text,segments=values,
                duration=info.duration,language=info.language,language_probability=info.language_probability,
                wall_seconds=time.perf_counter()-start))
            write(out/'results.json',rows);print('decoded',item['review_id'],clip['label'],len(text.split()),flush=True)
    comparisons=[]
    for review_id in sorted({r['review_id'] for r in rows}):
        subset=[r for r in rows if r['review_id']==review_id]
        for i,a in enumerate(subset):
            for b in subset[i+1:]:
                aa=re.findall(r"[a-z0-9']+",a['text'].lower());bb=re.findall(r"[a-z0-9']+",b['text'].lower())
                comparisons.append(dict(review_id=review_id,labels=[a['label'],b['label']],
                    word_edit_distance=distance(aa,bb),normalized_pairwise_disagreement=distance(aa,bb)/max(1,len(aa),len(bb)),
                    identical_audio=a['source_sha256']==b['source_sha256'],identical_normalized_text=aa==bb))
    write(out/'comparisons.json',comparisons)
    write(out/'summary.json',dict(at=stamp(),clips=len(rows),wall_seconds=time.perf_counter()-tick,
        pairwise_comparisons=comparisons,no_ground_truth_wer=True,human_listening=False,
        perceived_pitch_warble_assessed=False,model_audio_processing='Offline large-v3 ASR',production_accepted=False))
    write(ROOT/'active_jobs.json',dict(at=stamp(),jobs=[],analysis_in_progress='Assess blind ASR transcripts and visual spectra; no human listening or production approval'))
    print('offline_asr_finished',len(rows),flush=True)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--review',default='I016_review_local');p.add_argument('--device',choices=['cpu','cuda'],default='cuda')
    a=p.parse_args();run(a.review,a.device)
