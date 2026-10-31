"""Frozen serial adaptive-original promotion batch and exact local F audit."""
import os
for _name in ('OPENBLAS_NUM_THREADS','OMP_NUM_THREADS','MKL_NUM_THREADS'):
    os.environ[_name]='1'
from pathlib import Path
import subprocess
import sys
import time
from deflutter_campaign import ROOT,REPO,read,write,sha,stamp,append
from deflutter_recover_gate import log_has_no_writer

CORE=('deflutter_real_experiment.py','deflutter_adaptive_source.py','deflutter_adaptive_mains.py',
      'deflutter_local_acquisition.py','deflutter_map_preflight.py','deflutter_event_groups.py',
      'deflutter_extractors.py','deflutter_catalog.py','deflutter_campaign.py','deflutter_frozen_reserved_audit.py')


def tail(path):
    if not path.exists():return ''
    with path.open('rb') as f:
        f.seek(max(0,path.stat().st_size-2048));rows=f.read().decode('utf-8',errors='replace').splitlines()
    return rows[-1] if rows else ''


def run():
    folder=ROOT/'iterations/I024_batch';folder.mkdir(exist_ok=False)
    controls=read(ROOT/'iterations/I023_fullband_controls/metrics.json')
    if not controls['advance_to_unused_gate']:raise ValueError('Combined controls did not authorize advance')
    source=ROOT/'dense_promotion_passages.json';passages=read(source)['passages']
    if len(passages)!=3:raise ValueError('Expected frozen3-stratum sample; do not invent replacements')
    hashes={n:sha((REPO/'research'/n).read_bytes()) for n in CORE}
    protocol=dict(at=stamp(),controller_pid=os.getpid(),methodology='adaptive-original-bounded-island-research-v1',
        core_hashes=hashes,passages=passages,passages_sha256=sha(source.read_bytes()),
        controls_sha256=sha((ROOT/'iterations/I023_fullband_controls/metrics.json').read_bytes()),
        maximum_heavy_workers=1,native_threads=1,groups_per_channel=6,targets_per_passage=1,
        target_availability='>=2C/2V/2Fknown families, fixed hash, before scores; common existing acquisition horizons',
        source='Original and cached denoised catalogs remain; original, denoised, stationary and full-band adaptive-original measurements',
        support='One contiguous source-supported island containing allC/V and>=2F; no unsupported reacquisition; originals retain their path',
        clipping='Transformed paths withheld if original dependency input clips; raw original stream retains existing physical clip support',
        maps='Exact main/identity/sign/AM/3wrong-time grids saved before validation; invalid map rejected, never repaired',
        response='One calibration-selected exact-map-valid choice; validation once; reserved only after passing, using frozen grids only',
        gates='All existing numeric and absolute goals unchanged',
        exposure='Prospectively frozen retrospective promotion blocks; all historical v5material potentially exposed',
        no_outcome_adaptive_changes=True,no_production=True,no_source_upload=True,
        coordinator_sha256=sha(Path(__file__).read_bytes()))
    write(folder/'protocol.json',protocol)
    tasks=[]
    for i in range(3):
        iteration=f'I024_gate{i}'
        if (ROOT/'iterations'/iteration).exists():raise ValueError('Existing/uncertain gate; no automatic retry')
        command=[sys.executable,str(REPO/'research/deflutter_real_experiment.py'),
            '--iteration',iteration,'--catalog-iteration','I024_discovery','--passages-file',str(source),
            '--passage-offset',str(i),'--max-passages','1','--groups-per-channel','6','--provenance-balanced',
            '--targets-per-passage','1','--lag-aware-roles','--bounded-acquisition','--preflight-maps',
            '--require-final-events','2','--adaptive-extraction','--defer-tracking-without-response']
        tasks.append(dict(iteration=iteration,command=command,status='pending',log=str(folder/(iteration+'.log'))))
    audit='I024_reserved_audit'
    tasks.append(dict(iteration=audit,status='pending',log=str(folder/(audit+'.log')),
        command=[sys.executable,str(REPO/'research/deflutter_frozen_reserved_audit.py'),
            '--iteration',audit,'--parents',','.join(f'I024_gate{i}' for i in range(3))]))
    def verify():
        if sha(source.read_bytes())!=protocol['passages_sha256']:raise ValueError('Frozen passage manifest changed')
        changed=[n for n,v in hashes.items() if sha((REPO/'research'/n).read_bytes())!=v]
        if changed:raise ValueError(f'Frozen scientific code changed: {changed}; no more jobs may start')
    def sync(discovery=False):
        jobs=[]
        for t in tasks:
            if t['status']!='running':continue
            info=dict(iteration=t['iteration'],launcher_pid=t['pid'],status='running',log=t['log'],last_log_line=tail(Path(t['log'])))
            experiment=ROOT/'iterations'/t['iteration']/'experiment.json'
            if experiment.exists():info['scientific_pid']=read(experiment)['process_id']
            phase=ROOT/'iterations'/t['iteration']/'adaptive_source_progress.json'
            if phase.exists():info['source_phase']=read(phase)
            jobs.append(info)
        if discovery:
            d=ROOT/'iterations/I024_discovery';jobs.append(dict(iteration='I024_discovery',
                scientific_pid=read(d/'experiment.json')['process_id'],status='observed_live_external_worker',
                tool_session=83402,completed_sources=read(d/'checkpoint.json').get('completed_sources'),log=str(ROOT/'survey_I024.log')))
        state=dict(at=stamp(),controller_pid=os.getpid(),jobs=jobs,tasks=tasks,
            champion='Frozen baseline; no independent real restoration upgrade',
            analysis_in_progress='Fixed serial3-block challenger, then once-only frozen-map local reserved audit',
            next='Finish unchanged declared stages; inspect exact outputs/source identity and preserve failures, no production')
        write(folder/'state.json',state);write(ROOT/'active_jobs.json',state);write(ROOT/'state.json',state)
    prerequisite=ROOT/'iterations/I024_discovery/metrics.json'
    while not prerequisite.exists():
        verify()
        if log_has_no_writer(ROOT/'survey_I024.log'):
            write(folder/'blocked.json',dict(at=stamp(),reason='Discovery incomplete with no stdout writer; inspect before recovery, never duplicate uncertain work'))
            raise RuntimeError('Discovery worker ended without a complete catalog')
        sync(discovery=True);time.sleep(5)
    write(folder/'discovery_result.json',read(prerequisite))
    for task in tasks:
        verify();task.update(status='running',started_at=stamp())
        with Path(task['log']).open('xb') as stream:
            process=subprocess.Popen(task['command'],cwd=REPO,stdout=stream,stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW,env=os.environ.copy())
            task['pid']=process.pid;print('started',task['iteration'],process.pid,flush=True)
            while process.poll() is None:sync();time.sleep(5)
            task.update(status='complete' if process.returncode==0 else 'failed',exit_code=process.returncode,finished_at=stamp())
        append(ROOT/'experiment_ledger.jsonl',dict(iteration=task['iteration'],outcome='frozen_batch_stage_finished',exit_code=task['exit_code'],log=task['log']))
        sync();print('finished',task['iteration'],task['status'],flush=True)
    summaries=[]
    for t in tasks:
        p=ROOT/'iterations'/t['iteration']/'metrics.json'
        summaries.append(dict(iteration=t['iteration'],status=t['status'],exit_code=t['exit_code'],metrics=read(p) if p.exists() else None))
    write(folder/'summary.json',dict(at=stamp(),stages=summaries,untouched_real_qualification=False,
        listening_approved=False,production_accepted=False,champion_upgraded=False))
    sync();print('I024_batch_finished',flush=True)


if __name__=='__main__':run()
