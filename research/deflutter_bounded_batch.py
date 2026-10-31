"""Fixed I016 local-acquisition gates, with durable live job accounting."""
import os
for _name in ('OPENBLAS_NUM_THREADS','OMP_NUM_THREADS','MKL_NUM_THREADS'):
    os.environ[_name]='1'
import json
from pathlib import Path
import subprocess
import sys
import time
from deflutter_campaign import ROOT,REPO,read,write,sha,stamp,append


def run():
    protocol=read(ROOT/'I016_protocol_frozen.json')
    def verify():
        changed=[name for name,digest in protocol['core_hashes'].items()
                 if sha((REPO/'research'/name).read_bytes())!=digest]
        if changed:raise ValueError(f'Frozen I016 methods changed: {changed}')
    verify()
    folder=ROOT/'iterations/I016_batch';folder.mkdir(exist_ok=False)
    tasks=[]
    for i,catalog in enumerate(('I016_first1','I016_discovery')):
        iteration=f'I016_gate{i}'
        if (ROOT/'iterations'/iteration).exists():raise ValueError('Existing or uncertain gate; no automatic retry')
        command=[sys.executable,str(REPO/'research/deflutter_real_experiment.py'),
            '--iteration',iteration,'--catalog-iteration',catalog,
            '--passages-file',str(ROOT/'local_unused_passages.json'),'--passage-offset',str(i),
            '--max-passages','1','--groups-per-channel','6','--provenance-balanced',
            '--targets-per-passage','1','--lag-aware-roles','--defer-tracking-without-response',
            '--bounded-acquisition']
        tasks.append(dict(iteration=iteration,catalog=catalog,command=command,status='pending',
            log=str(folder/f'{iteration}.log')))
    write(folder/'batch_manifest.json',dict(at=stamp(),controller_pid=os.getpid(),tasks=tasks,
        protocol_sha256=sha((ROOT/'I016_protocol_frozen.json').read_bytes()),
        coordinator_sha256=sha(Path(__file__).read_bytes()),gate_workers=1,
        concurrent_discovery='I016_discovery may still be running; total maximum two native-thread workers',
        outcome_adaptive_changes=False,reserved_outcomes_evaluated=False,production_accepted=False))
    def sync():
        jobs=[dict(iteration=t['iteration'],reported_pid=t['pid'],status='running',log=t['log'])
              for t in tasks if t['status']=='running']
        discovery=ROOT/'iterations/I016_discovery'
        if not (discovery/'metrics.json').exists():
            jobs.append(dict(iteration='I016_discovery',reported_pid=read(discovery/'experiment.json')['process_id'],
                status='observed_incomplete_external_worker',checkpoint=str(discovery/'checkpoint.json'),
                completed_sources=read(discovery/'checkpoint.json').get('completed_sources'),
                caveat='Controller does not own this worker; tool session20153 confirms liveness'))
        state=dict(at=stamp(),controller_pid=os.getpid(),tasks=tasks,jobs=jobs,
            champion='Frozen baseline; no independent real challenger upgrade',
            analysis_in_progress='Fixed bounded acquisition test; finish both predeclared gates without fitting first outcome',
            next='Assess aggregate, exact map boundaries, controls and unused local final availability; no production')
        write(folder/'state.json',state);write(ROOT/'active_jobs.json',state);write(ROOT/'state.json',state)
    for task in tasks:
        dependency=ROOT/'iterations'/task['catalog']/'metrics.json'
        while not dependency.exists():
            verify();sync();time.sleep(5)
        verify()
        task.update(status='running',started_at=stamp())
        with Path(task['log']).open('xb') as stream:
            process=subprocess.Popen(task['command'],cwd=REPO,stdout=stream,stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0,env=os.environ.copy())
            task['pid']=process.pid;print('started',task['iteration'],process.pid,flush=True)
            while process.poll() is None:
                sync();time.sleep(5)
            task.update(exit_code=process.returncode,status='complete' if process.returncode==0 else 'failed',finished_at=stamp())
        append(ROOT/'experiment_ledger.jsonl',dict(iteration=task['iteration'],outcome='bounded_batch_child_finished',
            exit_code=task['exit_code'],log=task['log']))
        print('finished',task['iteration'],task['status'],flush=True);sync()
    summary=[dict(iteration=t['iteration'],status=t['status'],
        metrics=read(ROOT/'iterations'/t['iteration']/'metrics.json') if (ROOT/'iterations'/t['iteration']/'metrics.json').exists() else None)
        for t in tasks]
    write(folder/'summary.json',dict(at=stamp(),tasks=summary,
        reserved_outcomes_evaluated=False,listening_approved=False,production_accepted=False))
    (folder/'decision.md').write_text('# I016 bounded acquisition batch\n\nBoth frozen gates retained, including failures. Read summary and target records. A validation pass is only retrospective local numeric evidence; reserved independence, source identity, map lifetime, perceptual and production qualification remain separate.\n\nNext: inspect aggregate and exact outputs; do not fit these consumed validation outcomes.\n')
    sync();print('batch_finished',flush=True)


if __name__=='__main__':run()
