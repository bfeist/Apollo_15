"""Run instrumented I021 source-only retry singly after real recovery exits."""
import os
for _name in ('OPENBLAS_NUM_THREADS','OMP_NUM_THREADS','MKL_NUM_THREADS'):
    os.environ[_name]='1'
import ctypes
import argparse
from ctypes import wintypes
from pathlib import Path
import subprocess
import sys
import time
from deflutter_campaign import ROOT,REPO,read,write,sha,stamp,append


def child_memory(process):
    fields=[('cb',wintypes.DWORD),('PageFaultCount',wintypes.DWORD)]+[(k,ctypes.c_size_t) for k in
        ('PeakWorkingSetSize','WorkingSetSize','QuotaPeakPagedPoolUsage','QuotaPagedPoolUsage',
         'QuotaPeakNonPagedPoolUsage','QuotaNonPagedPoolUsage','PagefileUsage','PeakPagefileUsage')]
    counters=type('Counters',(ctypes.Structure,),dict(_fields_=fields))();counters.cb=ctypes.sizeof(counters)
    fn=ctypes.windll.psapi.GetProcessMemoryInfo
    fn.argtypes=[wintypes.HANDLE,ctypes.c_void_p,wintypes.DWORD];fn.restype=wintypes.BOOL
    ok=bool(fn(wintypes.HANDLE(int(process._handle)),ctypes.byref(counters),ctypes.sizeof(counters)))
    return dict(available=ok,working_set_bytes=counters.WorkingSetSize if ok else None,
        peak_working_set_bytes=counters.PeakWorkingSetSize if ok else None)


def run(recovery_iteration='I021_recovery2',child_iteration='I021R2_prefilter_discovery'):
    folder=ROOT/'iterations'/recovery_iteration;folder.mkdir(exist_ok=False)
    code=('deflutter_prefilter_discovery.py','deflutter_adaptive_mains.py','deflutter_near_mains_controls.py','deflutter_catalog.py')
    hashes={name:sha((REPO/'research'/name).read_bytes()) for name in code}
    command=[sys.executable,str(REPO/'research/deflutter_prefilter_discovery.py'),
        '--iteration',child_iteration,'--instrumented']
    if (ROOT/'iterations'/child_iteration).exists():raise ValueError('Existing retry; no uncertain automatic restart')
    write(folder/'manifest.json',dict(at=stamp(),controller_pid=os.getpid(),command=command,core_hashes=hashes,
        workers=1,native_threads=1,peak_memory_ceiling_bytes=int(1.5*2**30),wall_ceiling_seconds=900,
        wait_for='iterations/I016_recovery/summary.json',response_outcomes_evaluated=False,
        source_only=True,original_path_preserved=True,coordinator_sha256=sha(Path(__file__).read_bytes())))
    prerequisite=ROOT/'iterations/I016_recovery/summary.json';start=time.monotonic()
    while not prerequisite.exists():
        write(folder/'state.json',dict(at=stamp(),status='waiting_for_single_real_worker_exit',jobs=[],
            prerequisite=str(prerequisite),elapsed_wait_seconds=time.monotonic()-start))
        if time.monotonic()-start>3600:raise RuntimeError('Recovery wait exceeded1hour; do not launch into uncertain work')
        time.sleep(5)
    if any(sha((REPO/'research'/n).read_bytes())!=v for n,v in hashes.items()):raise ValueError('Frozen discovery instrumentation/source changed during wait')
    write(folder/'prerequisite_result.json',read(prerequisite))
    log=folder/f'{child_iteration}.log'
    with log.open('xb') as stream:
        process=subprocess.Popen(command,cwd=REPO,stdout=stream,stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW,env=os.environ.copy())
        print('instrumented_discovery_started',process.pid,flush=True);start=time.monotonic()
        while process.poll() is None:
            memory=child_memory(process)
            phase_path=ROOT/'iterations'/child_iteration/'phase.json'
            phase=read(phase_path) if phase_path.exists() else None
            state=dict(at=stamp(),controller_pid=os.getpid(),jobs=[dict(iteration=child_iteration,reported_pid=process.pid,status='running',log=str(log),memory=memory,phase=phase)],
                elapsed_seconds=time.monotonic()-start,champion='Frozen baseline; no independent real challenger upgrade',
                analysis_in_progress='Single-worker source-only full-band comb discovery; instrumented memory/time ceilings; no target outcomes',
                next='Assess strict known-center recovery, hum-only false groups and cost; no correction claim from peaks')
            write(folder/'state.json',state);write(ROOT/'active_jobs.json',state);write(ROOT/'state.json',state)
            append(folder/'resource_history.jsonl',dict(elapsed_seconds=state['elapsed_seconds'],memory=memory,phase=phase))
            if state['elapsed_seconds']>960:
                write(folder/'forced_stop.json',dict(at=stamp(),reason='Own child exceeded16minute controller ceiling; preserve committed source-only cases'))
                process.terminate();process.wait(timeout=30);break
            time.sleep(5)
    metrics=ROOT/'iterations'/child_iteration/'metrics.json'
    result=dict(at=stamp(),controller_pid=os.getpid(),jobs=[],exit_code=process.returncode,
        status='completed' if process.returncode==0 else 'failed',metrics=read(metrics) if metrics.exists() else None,
        next='Inspect strict recovery and false groups; retain hard-domain misses; no real promotion from source-only catalog')
    write(folder/'summary.json',result);write(ROOT/'active_jobs.json',result);write(ROOT/'state.json',result)
    append(ROOT/'experiment_ledger.jsonl',dict(iteration=child_iteration,outcome=result['status'],exit_code=process.returncode,artifacts=str(ROOT/'iterations'/child_iteration)))
    print('instrumented_discovery_finished',process.returncode,flush=True)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--recovery-iteration',default='I021_recovery2');p.add_argument('--child-iteration',default='I021R2_prefilter_discovery')
    a=p.parse_args();run(a.recovery_iteration,a.child_iteration)
