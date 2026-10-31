"""Recover only the interrupted, unconsumed I016 gate1 in a new directory."""
import os
for _name in ('OPENBLAS_NUM_THREADS','OMP_NUM_THREADS','MKL_NUM_THREADS'):
    os.environ[_name]='1'
import ctypes
from ctypes import wintypes
from pathlib import Path
import subprocess
import sys
import time
from deflutter_campaign import ROOT,REPO,read,write,sha,stamp,append


def log_has_no_writer(path):
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.CreateFileW.argtypes=[wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD,ctypes.c_void_p,wintypes.DWORD,wintypes.DWORD,wintypes.HANDLE]
    kernel.CreateFileW.restype=wintypes.HANDLE
    kernel.CloseHandle.argtypes=[wintypes.HANDLE]
    handle=kernel.CreateFileW(str(path),0x80000000,0,None,3,0,None)
    if handle==ctypes.c_void_p(-1).value:return False
    kernel.CloseHandle(handle);return True


def run():
    parent=ROOT/'iterations/I016_gate1';oldlog=ROOT/'iterations/I016_batch/I016_gate1.log'
    protocol=read(ROOT/'I016_protocol_frozen.json')
    if list(parent.glob('*validation_consumed.json')) or read(parent/'results.json'):
        raise ValueError('Response already attempted or committed; refuse recovery on consumed outcomes')
    if not log_has_no_writer(oldlog):raise ValueError('Original controller/worker may hold stdout; refuse duplicate job')
    if any(sha((REPO/'research'/name).read_bytes())!=digest for name,digest in protocol['core_hashes'].items()):
        raise ValueError('Frozen I016 source changed; cannot recover compatibly')
    folder=ROOT/'iterations/I016_recovery';folder.mkdir(exist_ok=False)
    old=read(ROOT/'iterations/I016_batch/state.json');write(folder/'prior_batch_state.json',old)
    task=next(t for t in old['tasks'] if t['iteration']=='I016_gate1')
    command=task['command'].copy();command[command.index('--iteration')+1]='I016_gate1R'
    if (ROOT/'iterations/I016_gate1R').exists():raise ValueError('Recovery already exists; no automatic uncertain retry')
    evidence=dict(at=stamp(),status='interrupted_before_response',prior_controller_pid=old['controller_pid'],
        prior_worker_pid=task['pid'],last_heartbeat=old['at'],no_open_stdout_writer=True,
        validation_consumption_markers=[],committed_results=0,
        execution_handles='Both prior unified-exec sessions returned Unknown process id; terminal adapter has no attached session',
        cause='Unestablished external execution interruption; resource pressure is possible but not proven',
        action='New directory, exact frozen method/settings and same unconsumed metadata-selected target, one worker only')
    write(folder/'interruption_evidence.json',evidence);write(parent/'interruption.json',evidence)
    append(ROOT/'experiment_ledger.jsonl',dict(iteration='I016_gate1',outcome='external_interruption_before_response',artifacts=str(parent),recovery='I016_gate1R'))
    write(folder/'manifest.json',dict(at=stamp(),controller_pid=os.getpid(),command=command,
        protocol_sha256=sha((ROOT/'I016_protocol_frozen.json').read_bytes()),workers=1,native_threads=1,
        coordinator_sha256=sha(Path(__file__).read_bytes()),no_production=True))
    log=folder/'I016_gate1R.log'
    with log.open('xb') as stream:
        process=subprocess.Popen(command,cwd=REPO,stdout=stream,stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW,env=os.environ.copy())
        print('recovery_started',process.pid,flush=True)
        while process.poll() is None:
            state=dict(at=stamp(),controller_pid=os.getpid(),jobs=[dict(iteration='I016_gate1R',reported_pid=process.pid,status='running',log=str(log))],
                champion='Frozen baseline; no independent real challenger upgrade',
                analysis_in_progress='Recover unconsumed interrupted gate alone; source/policy frozen; no parallel discovery',
                next='Assess completed recovered gate and structured map failures; no retry of consumed I016_gate0')
            write(folder/'state.json',state);write(ROOT/'active_jobs.json',state);write(ROOT/'state.json',state)
            time.sleep(5)
    metrics=ROOT/'iterations/I016_gate1R/metrics.json'
    result=dict(at=stamp(),controller_pid=os.getpid(),jobs=[],exit_code=process.returncode,
        status='completed' if process.returncode==0 else 'failed',metrics=read(metrics) if metrics.exists() else None,
        next='Assess recovered outcome, exact map control validity, reserved-family availability and source identity; retain failures')
    write(folder/'summary.json',result);write(ROOT/'active_jobs.json',result);write(ROOT/'state.json',result)
    append(ROOT/'experiment_ledger.jsonl',dict(iteration='I016_gate1R',outcome=result['status'],exit_code=process.returncode,artifacts=str(ROOT/'iterations/I016_gate1R')))
    print('recovery_finished',process.returncode,flush=True)


if __name__=='__main__':run()
