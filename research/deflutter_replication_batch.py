"""Bounded fixed-method replication coordinator; never production rendering.

One passage per job, at most two one-thread children, atomic live state. No
threshold/candidate/source policy changes from outcomes. Completed compatible
jobs can be reused; uncertain incomplete response gates require manual review.
"""
import os
for _name in ('OPENBLAS_NUM_THREADS','OMP_NUM_THREADS','MKL_NUM_THREADS'):
    os.environ[_name]='1'
import argparse
from collections import Counter
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time
from deflutter_campaign import ROOT,REPO,read,write,sha,stamp,append

CORE=('deflutter_real_experiment.py','deflutter_event_groups.py','deflutter_catalog.py','deflutter_extractors.py')


def core_hashes():
    return {name:sha((REPO/'research'/name).read_bytes()) for name in CORE}


def commands(index,manifest):
    catalog=f'I014_catalog{index}';gate=f'I014_gate{index}'
    return {
        'catalog': [sys.executable,str(REPO/'research/deflutter_campaign.py'),'survey','--iteration',catalog,'--passages-file',str(manifest)],
        'gate': [sys.executable,str(REPO/'research/deflutter_real_experiment.py'),'--iteration',gate,'--catalog-iteration',catalog,
            '--passages-file',str(ROOT/'replication_new_tapes.json'),'--passage-offset',str(index),'--max-passages','1',
            '--groups-per-channel','6','--provenance-balanced','--targets-per-passage','1','--lag-aware-roles','--defer-tracking-without-response']}


def tail(path):
    if not path.exists():return ''
    with path.open('rb') as stream:
        stream.seek(max(0,path.stat().st_size-4096));lines=stream.read().decode('utf-8',errors='replace').splitlines()
    return lines[-1] if lines else ''


def run(resume=False,dry_run=False):
    source=ROOT/'replication_new_tapes.json';data=read(source);passages=data['passages']
    config=dict(methodology='lag-aware-fixed-method-replication-batch-v1',at=stamp(),controller_pid=os.getpid(),
        passages=passages,source_manifest_sha256=sha(source.read_bytes()),core_hashes=core_hashes(),
        maximum_workers=2,native_threads=1,free_disk_bytes=shutil.disk_usage(REPO).free,
        hypothesis='Does unchanged lag-aware dual-source method generalize to three additional metadata-selected tapes?',
        sampling='Fixed tape hash in three archive strata excluding original three tapes; fixed coordinate-availability hash; all old blocks excluded',
        evaluation='One target and one calibration-selected variant per passage; no outcome-adaptive batch changes; finish all declared jobs',
        no_production=True,reserved_outcomes_evaluated=False,real_exposure='Frozen retrospective; all v5 real material potentially exposed',
        quality='Unchanged relative, baseline, support, control and exact-map limits; source origin/unknown dependencies remain explicit')
    if dry_run:
        print(json.dumps(dict(config=config,commands=[commands(i,ROOT/f'I014_passage{i}.json') for i in range(len(passages))]),indent=2));return
    if config['free_disk_bytes']<10*2**30:raise ValueError('Less than10GiB free; do not start batch')
    folder=ROOT/'iterations/I014_batch';manifest_path=folder/'batch_manifest.json'
    if not resume:
        folder.mkdir(parents=True,exist_ok=False);write(manifest_path,config)
        for i,p in enumerate(passages):write(ROOT/f'I014_passage{i}.json',dict(passages=[p],parent_manifest_sha256=config['source_manifest_sha256']))
    else:
        saved=read(manifest_path)
        if saved['core_hashes']!=config['core_hashes'] or saved['source_manifest_sha256']!=config['source_manifest_sha256']:
            raise ValueError('Batch scientific/source identity changed; use a new generation')
    tasks=[]
    for i in range(len(passages)):
        for stage in ('catalog','gate'):
            iteration=f'I014_{stage}{i}';output=ROOT/'iterations'/iteration
            status='pending'
            if output.exists():
                if (output/'metrics.json').exists() and (output/'experiment.json').exists():status='complete_reused'
                else:raise ValueError(f'Incomplete prior job {iteration}; inspect process, checkpoints and response consumption before resuming. No automatic retry.')
            tasks.append(dict(index=i,stage=stage,iteration=iteration,status=status,
                command=commands(i,ROOT/f'I014_passage{i}.json')[stage],output=str(output),
                log=str(folder/f'{iteration}.log')))
    active={};started=time.monotonic();stopped_reason=None
    def sync():
        running=[]
        for name,(process,stream,task) in active.items():
            info=dict(iteration=name,reported_pid=process.pid,status='running',log=task['log'],last_log_line=tail(Path(task['log'])))
            checkpoint=Path(task['output'])/'checkpoint.json'
            if checkpoint.exists():info['completed_sources']=read(checkpoint).get('completed_sources')
            running.append(info)
        write(folder/'state.json',dict(at=stamp(),controller_pid=os.getpid(),tasks=tasks,running_jobs=running,stopped_reason=stopped_reason))
        write(ROOT/'active_jobs.json',dict(at=stamp(),controller_pid=os.getpid(),controller_iteration='I014_batch',
            jobs=running,analysis_in_progress='Fixed-method replication; outcomes do not change remaining jobs',
            promotion_withheld=True,source='I014_batch/state.json'))
        write(ROOT/'state.json',dict(at=stamp(),running_jobs=running,controller_pid=os.getpid(),
            champion='Frozen baseline comparison; no independent real champion upgrade',next='Complete full I014 fixed replication batch, then inspect aggregate and worst cases',
            evidence_restrictions=str(ROOT/'evidence_restrictions.json')))
    try:
        while any(t['status']=='pending' for t in tasks) or active:
            if core_hashes()!=config['core_hashes']:
                stopped_reason='Scientific implementation changed; no new jobs started'
            for name,(process,stream,task) in list(active.items()):
                code=process.poll()
                if code is None:continue
                stream.close();task.update(status='complete' if code==0 else 'failed',exit_code=code,finished_at=stamp())
                append(ROOT/'experiment_ledger.jsonl',dict(iteration=name,outcome='batch_child_completed' if code==0 else 'batch_child_failed',
                    exit_code=code,artifacts=task['output'],log=task['log']))
                print('completed',name,'exit',code,tail(Path(task['log'])),flush=True);del active[name]
            if not stopped_reason:
                # Prefer one discovery and one qualification job when possible.
                for stage in ('catalog','gate'):
                    if len(active)>=2:break
                    if any(t['stage']==stage for _,_,t in active.values()):continue
                    ready=[]
                    for t in tasks:
                        if t['stage']!=stage or t['status']!='pending':continue
                        dependency=next((v for v in tasks if v['index']==t['index'] and v['stage']=='catalog'),None)
                        if stage=='gate' and dependency['status']=='failed':
                            t.update(status='blocked_source_job_failed');continue
                        if stage=='gate' and not dependency['status'].startswith('complete'):continue
                        ready.append(t)
                    if not ready:continue
                    task=ready[0];log=Path(task['log']);stream=log.open('xb')
                    flags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0
                    process=subprocess.Popen(task['command'],cwd=REPO,stdout=stream,stderr=subprocess.STDOUT,
                        creationflags=flags,env=os.environ.copy())
                    task.update(status='running',reported_pid=process.pid,started_at=stamp())
                    active[task['iteration']]=(process,stream,task)
                    print('started',task['iteration'],'pid',process.pid,'log',log,flush=True)
            sync()
            if stopped_reason and not active:break
            time.sleep(5)
        summary=[]
        for t in tasks:
            p=Path(t['output'])/'metrics.json'
            summary.append(dict(iteration=t['iteration'],stage=t['stage'],status=t['status'],
                metrics=read(p) if p.exists() else None,log=t['log']))
        write(folder/'summary.json',dict(tasks=summary,elapsed_seconds=time.monotonic()-started,
            stopped_reason=stopped_reason,reserved_outcomes_evaluated=False,production_accepted=False,listening_approved=False))
        (folder/'decision.md').write_text('# I014 fixed replication batch\n\nAll declared jobs retained, including failures and bypasses.\n\nRead summary.json and each gate result for numeric findings; no batch result supplies production, listening or untouched independent qualification.\n\nNext: inspect aggregate/worst cases and unused final-family availability before any further qualification.\n',encoding='utf-8')
        sync();print('batch_finished',json.dumps([(t['iteration'],t['status']) for t in tasks]),flush=True)
    finally:
        # Never terminate child science silently on coordinator interruption.
        for process,stream,task in active.values():stream.close()


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--resume',action='store_true');p.add_argument('--dry-run',action='store_true');a=p.parse_args();run(a.resume,a.dry_run)
