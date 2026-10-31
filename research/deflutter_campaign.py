"""Reproducible, research-only Apollo 15 campaign; never production rendering."""
import os
for _name in ('OPENBLAS_NUM_THREADS', 'OMP_NUM_THREADS', 'MKL_NUM_THREADS', 'NUMEXPR_NUM_THREADS'):
    os.environ[_name] = '1'
import argparse
from collections import Counter
import datetime as dt
import hashlib
import importlib.util
import json
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import time
import traceback

import numpy as np
import scipy
import soundfile as sf

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO/'scripts'))
from flutter_planning import inventory, read_channel, STAMP, seconds
from flutter_policy import implementation_digest, policy_digest, LIMITS
from deflutter_catalog import CatalogConfig, discover_ridges, allocation_only, identity, matching_truth

ROOT = REPO/'reports/deflutter_iterative_20261030'
ORIG = Path('D:/Apollo_15_30-Track/orig_concatenated')
CLEAN = Path('D:/Apollo_15_30-Track/denoised')
SAVED = Path('D:/Apollo_15_30-Track/deflutter_planning_v5_allchannel')
SEED = 'apollo15-deflutter-20261030-v1'


def sha(value):
    return hashlib.sha256(value).hexdigest()


def stamp():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def memory_peak():
    import ctypes
    from ctypes import wintypes
    fields=[('cb',wintypes.DWORD),('PageFaultCount',wintypes.DWORD)]+[(k,ctypes.c_size_t) for k in (
        'PeakWorkingSetSize','WorkingSetSize','QuotaPeakPagedPoolUsage','QuotaPagedPoolUsage',
        'QuotaPeakNonPagedPoolUsage','QuotaNonPagedPoolUsage','PagefileUsage','PeakPagefileUsage')]
    counters_type=type('ProcessCounters',(ctypes.Structure,),dict(_fields_=fields))
    counters=counters_type();counters.cb=ctypes.sizeof(counters)
    fun=ctypes.windll.psapi.GetProcessMemoryInfo
    fun.argtypes=[wintypes.HANDLE,ctypes.c_void_p,wintypes.DWORD];fun.restype=wintypes.BOOL
    if not fun(wintypes.HANDLE(-1),ctypes.byref(counters),counters.cb):
        raise OSError('GetProcessMemoryInfo failed')
    return counters.PeakWorkingSetSize


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    # Independent workers may update coordinator state; never share one temp
    # filename across processes. The committed destination is still atomic.
    tmp = path.with_name(path.name+f'.{os.getpid()}.partial')
    tmp.write_text(json.dumps(value, indent=2, allow_nan=False), encoding='utf-8')
    tmp.replace(path)


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def append(path, value):
    with path.open('a', encoding='utf-8') as f:
        f.write(json.dumps(dict(at=stamp(), **value), allow_nan=False)+'\n')


def snapshot(destination):
    destination.mkdir(parents=True, exist_ok=False)
    files = list((REPO/'scripts').glob('*.py'))+list((REPO/'tests').glob('test_*continuous*.py'))+list((REPO/'tests').glob('test_v5*.py'))
    files += list((REPO/'research').glob('deflutter_*.py'))
    files += list((REPO/'tests').glob('test_deflutter_*.py'))
    files += [REPO/'pyproject.toml', REPO/'uv.lock', REPO/'docs/deflutter_iterative_research_runbook.md']
    hashes = {}
    for source in files:
        relative = source.relative_to(REPO)
        target = destination/relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        hashes[str(relative)] = sha(target.read_bytes())
    write(destination/'hashes.json', hashes)
    return sha(json.dumps(hashes, sort_keys=True).encode())


def frozen_discover():
    p = ROOT/'baseline/implementation/scripts/flutter_tracking.py'
    spec = importlib.util.spec_from_file_location('frozen_flutter_tracking', p)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module.discover


def exposure(stage, sources, kind='predictor_features', purpose='development'):
    append(ROOT/'exposure_ledger.jsonl', dict(actor='deflutter_campaign', iteration=stage,
        sources=sources, evidence=kind, purpose=purpose, restriction='retrospective; no fresh-real qualification claim'))


def begin(iteration, hypothesis, comparison, acceptance, **extra):
    frozen=read(ROOT/'campaign.json')
    if implementation_digest()!=frozen['implementation_sha256'] or policy_digest()!=frozen['policy_sha256']:
        raise ValueError('Frozen shared baseline/policy changed; do not reuse this campaign silently')
    runtime=dict(python=platform.python_version(),numpy=np.__version__,scipy=scipy.__version__,soundfile=sf.__version__)
    if runtime!=frozen['versions']:
        raise ValueError('Scientific dependency runtime changed; use an explicitly versioned generation')
    folder = ROOT/'iterations'/iteration
    folder.mkdir(parents=True, exist_ok=False)
    implementation = snapshot(folder/'implementation')
    write(folder/'experiment.json', dict(iteration=iteration, at=stamp(), process_id=os.getpid(), command=sys.argv, hypothesis=hypothesis,
        comparison=comparison, acceptance=acceptance, implementation_sha256=implementation,
        seed=SEED, workers=1, native_threads=1, sealed_outcomes_accessed=False, **extra))
    write(ROOT/'state.json',dict(at=stamp(),current_iteration=iteration,
        running_jobs=[dict(pid=os.getpid(),iteration=iteration,command=sys.argv,status='running')],
        champion='frozen v5; no new real correction qualified',next='Inspect committed checkpoints and iteration results'))
    write(ROOT/'active_jobs.json',dict(at=stamp(),jobs=[dict(pid=os.getpid(),iteration=iteration,status='running')],
        scope='Current experiment; a live batch controller supplies its aggregate at its next heartbeat',
        analysis_in_progress=hypothesis,promotion_withheld=True))
    return folder


def finish(folder, metrics, decision, reason, next_action):
    write(folder/'metrics.json', metrics)
    text = f'# {folder.name}\n\nDecision: {decision}\n\n{reason}\n\nMetrics: [metrics.json](metrics.json).\n\nNo human listening or production approval. Real evidence is previously exposed.\n\nNext: {next_action}\n'
    (folder/'decision.md').write_text(text, encoding='utf-8')
    append(ROOT/'experiment_ledger.jsonl', dict(iteration=folder.name, outcome=decision,
        reason=reason, artifacts=str(folder), next=next_action))
    write(ROOT/'state.json', dict(at=stamp(), completed_iteration=folder.name,
        champion='frozen v5; no challenger qualified for real correction', running_jobs=[], next=next_action))
    write(ROOT/'active_jobs.json',dict(at=stamp(),jobs=[],completed_iteration=folder.name,
        analysis_in_progress=next_action,promotion_withheld=True))
    (ROOT/'NEXT.md').write_text(f'# Next action\n\n{next_action}\n\nLast decision: {folder.name}: {decision}.\n\n{reason}\n', encoding='utf-8')


def init():
    ROOT.mkdir(parents=True, exist_ok=False)
    baseline = snapshot(ROOT/'baseline/implementation')
    invs = []
    for tape in sorted(ORIG.glob('A15_T*')):
        if not tape.is_dir():
            continue
        try:
            inv = inventory(tape)
            inv['unassigned_sources'] = []
            for item in inv.get('excluded_files', []):
                path = Path(item['path'])
                info = sf.info(path)
                match = STAMP.search(path.name)
                offset = seconds(match[1])-inv['origin_filename_seconds']
                inv['unassigned_sources'].append(dict(item, channel='unassigned_'+str(len(inv['unassigned_sources'])+1),
                    sample_rate=info.samplerate, frames=info.frames, start_seconds=offset,
                    end_seconds=offset+info.duration, role='unassigned_survey_only', head=None))
            for row in inv['channels']:
                folder = CLEAN/tape.name/f"CH{row['channel']:02d}"
                meta = folder/'denoised.json'; wav = folder/'denoised.wav'
                row['denoised_available'] = meta.exists() and wav.exists()
                if row['denoised_available']:
                    raw = meta.read_bytes(); data = json.loads(raw)
                    row['denoised_metadata_sha256'] = sha(raw)
                    row['denoised_status'] = data.get('status')
                    row['denoised_identity_matches'] = data.get('signature', {}).get('source') == {
                        k:row[k] for k in ('path','bytes','mtime_ns','frames','sample_rate','channel')}
                    row['cleanup_log_available'] = (folder/'cleanup.jsonl').exists()
            inv['baseline_files'] = []
            for relative in ('survey_manifest.json','plan/plan.json','joined_assessment.json'):
                p = SAVED/tape.name/relative
                inv['baseline_files'].append(dict(path=str(p), available=p.exists(),
                    sha256=sha(p.read_bytes()) if p.exists() else None,
                    exposure='previously_evaluated_outcomes; not read for new selection'))
            invs.append(inv)
            print('inventoried', tape.name, len(inv['channels']), flush=True)
        except Exception as exc:
            invs.append(dict(tape=tape.name, status='inventory_error', error=str(exc)))
    write(ROOT/'inventory.json', dict(at=stamp(), tapes=invs))
    # Global absolute filename-clock blocks unify overlapping recordings. These
    # clocks are not authenticated mission time; uncertain dependencies remain.
    blocks = sorted({int((i['origin_filename_seconds']+s)//1800)
        for i in invs if 'channels' in i for s in np.arange(0, i['duration_seconds'], 900)})
    def rank(value):
        return sha(f'{SEED}:{value}'.encode())
    roles = ['development','development','development','promotion_validation','reserved_audit']
    block_rows = [dict(block=b, absolute_start=b*1800, absolute_end=(b+1)*1800,
        role=roles[int(rank('role:'+str(b))[:8],16)%len(roles)], guard_seconds=360,
        guard_excluded=[[b*1800,b*1800+360],[(b+1)*1800-360,(b+1)*1800]],
        prior_exposure='potentially exposed by complete v5; prospectively frozen retrospective only',
        dependence_group=f'filename_clock_block_{b}') for b in blocks]
    dev = {r['block'] for r in block_rows if r['role']=='development'}
    eligible = []
    for i in invs:
        if 'channels' not in i:
            continue
        choices = []
        for b in dev:
            for offset in (480, 1080):
                absolute = b*1800+offset; start = absolute-i['origin_filename_seconds']
                if start >= 360 and start+180+360 <= i['duration_seconds']:
                    choices.append(dict(tape=i['tape'], start_seconds=start, duration_seconds=180.,
                        absolute_start=absolute, block=b, role='development',
                        dependence_group=f'filename_clock_block_{b}',
                        prior_exposure='previous v5; new features are development'))
        if len(choices) >= 2:
            eligible.append((i, choices))
    # Three archive strata before outcome-independent hash ranking, two passages
    # in separate blocks per tape. No assessment values enter this selection.
    passages = []
    for stratum in np.array_split(np.arange(len(eligible)), 3):
        i, choices = min((eligible[int(k)] for k in stratum), key=lambda pair: rank('tape:'+pair[0]['tape']))
        selected = []
        for choice in sorted(choices, key=lambda r: rank(f"passage:{r['tape']}:{r['start_seconds']}")):
            if choice['block'] not in {r['block'] for r in selected}:
                selected.append(choice)
            if len(selected)==2:
                break
        passages.extend(selected)
    write(ROOT/'split_manifest.json', dict(seed=SEED, block_seconds=1800, guard_seconds=360,
        selection_algorithm='global filename-clock block hash roles; three archive strata; lowest SHA256 tape; lowest SHA256 starts in distinct blocks',
        mission_time_authenticated=False, blocks=block_rows, development_passages=passages,
        activity_classifier='Frozen metadata-only initial sample; quiet-passage inclusion must be checked using activity features; no outcome-conditioned reselection',
        limitation='All v5 tapes potentially exposed. No real block is called untouched. Unknown clock/head/feed dependencies conservative within blocks.'))
    write(ROOT/'campaign.json', dict(at=stamp(), methodology='tone-catalog-research-v1', objective='General dual-source discovery and independently qualified timing correction',
        runbook=str(REPO/'docs/deflutter_iterative_research_runbook.md'), baseline_snapshot_sha256=baseline,
        implementation_sha256=implementation_digest(), policy_sha256=policy_digest(), limits=LIMITS,
        source_roots=[str(ORIG),str(CLEAN),str(SAVED)], seed=SEED,
        compute=dict(workers=1,native_threads=1,maximum_workers=4,target_job_minutes=30),
        versions=dict(python=platform.python_version(),numpy=np.__version__,scipy=scipy.__version__,soundfile=sf.__version__),
        safeguards=['originals and caches read-only','no production','no sealed response evaluation','no listening approval'],
        resources=dict(free_repo_bytes=shutil.disk_usage(REPO).free, free_source_bytes=shutil.disk_usage(ORIG).free),
        process_check='Get-Process python*,uv,ffmpeg returned none; CIM command lines unavailable; one CPU worker used; GPU not needed'))
    for name in ('exposure_ledger.jsonl','experiment_ledger.jsonl','candidate_catalog.jsonl'):
        (ROOT/name).touch()
    exposure('I000', [str(SAVED)], 'existing_bundle_hashes_and_metadata', 'inventory; hashes only for assessments, no outcomes parsed')
    write(ROOT/'state.json', dict(at=stamp(), next='I000 replay and 180-second paired benchmark', running_jobs=[]))
    (ROOT/'NEXT.md').write_text('Run I000 replay and benchmark; then I001 synthetic allocation ablation.\n', encoding='utf-8')
    print(json.dumps(dict(tapes=len(invs),passages=passages)), flush=True)


def load_excerpt(inv, row, start, end, version):
    if version == 'original':
        x,sr,digest = read_channel(row,start,end)
        return x,sr,dict(path=row['path'], decoded_sha256=digest,
            source_identity={k:row[k] for k in ('bytes','mtime_ns','frames','sample_rate')})
    folder = CLEAN/inv['tape']/f"CH{row['channel']:02d}"
    raw = (folder/'denoised.json').read_bytes(); meta = json.loads(raw); sig = meta['signature']
    if row.get('denoised_metadata_sha256') and sha(raw)!=row['denoised_metadata_sha256']:
        raise ValueError('Cached denoised source metadata changed since campaign inventory')
    if meta['status'] != 'complete' or sig['source'] != {k:row[k] for k in ('path','bytes','mtime_ns','frames','sample_rate','channel')} or sig['offset_frames'] != 0 or sig['frames'] != row['frames']:
        raise ValueError('Denoised source/alignment binding mismatch')
    path = folder/'denoised.wav'; info = sf.info(path); stat = path.stat()
    if info.frames != row['frames'] or info.samplerate != row['sample_rate'] or info.channels != 1:
        raise ValueError('Denoised format mismatch')
    with sf.SoundFile(path) as stream:
        stream.seek(round((start-row['start_seconds'])*info.samplerate))
        x = stream.read(round((end-start)*info.samplerate), dtype='float64')
    after = path.stat()
    if len(x)!=round((end-start)*info.samplerate) or not np.isfinite(x).all() or (stat.st_size,stat.st_mtime_ns)!=(after.st_size,after.st_mtime_ns):
        raise ValueError('Denoised read incomplete or changed')
    return x,info.samplerate,dict(path=str(path),decoded_sha256=sha(x.tobytes()), metadata_sha256=sha(raw),
        source_identity=dict(bytes=stat.st_size,mtime_ns=stat.st_mtime_ns,frames=info.frames,sample_rate=info.samplerate),
        cleanup_log=str(folder/'cleanup.jsonl'),full_source_decoded_hash_reverified=False)


def replay(iteration='I000'):
    folder = begin(iteration, 'Frozen legacy detector reproduces source-bound audit', 'Saved October30 audit vs frozen implementation',
                   'All selected decoded hashes and frequency lists match exactly; benchmark all available channels and versions')
    evidence = read(REPO/'reports/deflutter_discovery_audit_20261030/evidence.json')
    invs = {r['tape']:r for r in read(ROOT/'inventory.json')['tapes']}
    legacy = frozen_discover(); results=[]
    # Two predeclared channels x first/middle excerpt x both versions x 3 tapes.
    for tape in evidence['tapes']:
        excerpts = [e for e in tape['excerpts'] if e['channel'] in (3,14)]
        starts = sorted({e['start'] for e in excerpts})[:2]
        for e in excerpts:
            if e['start'] not in starts:
                continue
            inv = invs[tape['tape']]; row = next(r for r in inv['channels'] if r['channel']==e['channel'])
            x,sr,source = load_excerpt(inv,row,e['start'],e['end'],e['version'])
            freqs = sorted(r['carrier_hz'] for r in legacy(x,sr,8))
            results.append(dict(tape=tape['tape'],channel=e['channel'],version=e['version'],start=e['start'],
                hash_match=source['decoded_sha256']==e['decoded_sha256'], frequencies_match=freqs==e['capped_frequencies'],
                source=source, frequencies=freqs))
            print('replay',tape['tape'],e['channel'],e['version'],results[-1]['hash_match'],results[-1]['frequencies_match'],flush=True)
    write(folder/'replay.json',results)
    exposure('I000',results,'original_and_cleaned_predictor_features','baseline replay; no response outcomes')
    # Frozen metadata sample, first passage, actual 180 sec all channels/sources.
    passage = read(ROOT/'split_manifest.json')['development_passages'][0]
    inv=invs[passage['tape']]; records=[]; start_wall=time.perf_counter(); decoded=0
    for row in inv['channels']+inv.get('unassigned_sources',[]):
        lo=max(passage['start_seconds'],row['start_seconds']);hi=min(lo+180,row['end_seconds'])
        for version in ('original','denoised'):
            result=dict(tape=inv['tape'],channel=row['channel'],version=version,start=lo,end=hi)
            if hi<=lo:
                result['status']='unavailable'
            elif version=='denoised' and not row.get('denoised_available'):
                result['status']='missing_source'
            else:
                tick=time.perf_counter()
                try:
                    x,sr,source=load_excerpt(inv,row,lo,hi,version);decoded+=x.nbytes
                    result.update(status='analyzed',source=source,candidates=legacy(x,sr,8),wall_seconds=time.perf_counter()-tick)
                except Exception as exc:
                    result.update(status='read_error',error=str(exc))
            records.append(result)
            print('benchmark',row['channel'],version,result['status'],flush=True)
    write(folder/'benchmark.json',records)
    wall=time.perf_counter()-start_wall
    peak=memory_peak()
    metrics=dict(replay_count=len(results),all_hashes_match=all(r['hash_match'] for r in results),
        all_frequencies_match=all(r['frequencies_match'] for r in results),
        benchmark_passage=passage,statuses=dict(Counter(r['status'] for r in records)),
        wall_seconds=wall,decoded_bytes=decoded,decoded_mib_per_second=decoded/wall/2**20,
        throughput_caveat='Decoded array throughput, not physical disk I/O; OS cache may participate',peak_working_set_bytes=peak,
        artifact_bytes=sum(p.stat().st_size for p in folder.rglob('*') if p.is_file()),
        discovery_only=True,target_evaluation_imported=False)
    finish(folder,metrics,'advance' if metrics['all_hashes_match'] and metrics['all_frequencies_match'] else 'reject',
        f"Replayed {len(results)} excerpts. Benchmark {wall:.1f}s, {metrics['statuses']}. Discovery adapter has no response evaluator.",
        '.venv/Scripts/python.exe research/deflutter_campaign.py synthetic --iteration I001')


def synthetic_cases(generation='development', count=48):
    rng=np.random.default_rng(int(sha((SEED+generation).encode())[:16],16));sr=8000;duration=32
    t=np.arange(sr*duration)/sr
    for index in range(count):
        kind=('easy','close_hum','interrupted','drift','am_only','hum_only','noise_only','nearby_pair')[index%8]
        hz=float(rng.uniform(110,3750))
        while 2100<hz<2900 or abs(hz-round(hz/60)*60)<10:
            hz=float(rng.uniform(110,3750))
        x=rng.normal(0,.001,len(t));truth=[]
        if kind not in ('hum_only','noise_only'):
            if kind=='close_hum':
                hz=60*int(rng.integers(2,31))+float(rng.uniform(3,20))
            gate=np.ones(len(t));lo=0.;hi=duration
            if kind=='interrupted':
                lo=6.;hi=20.;gate=(t>=lo)&(t<hi)
            deviation=.6*np.sin(2*np.pi*.07*t) if kind=='drift' else 0.
            phase=2*np.pi*hz*t+(2*np.pi*np.cumsum(deviation)/sr if np.ndim(deviation) else 0)
            amp=.012*(1+.7*np.sin(2*np.pi*5.3*t)) if kind=='am_only' else .012
            x+=amp*gate*np.cos(phase)
            truth.append(dict(hz=hz,start=lo,end=hi))
            if kind=='nearby_pair':
                hz2=hz+float(rng.uniform(5,22));x+=.009*np.cos(2*np.pi*hz2*t)
                truth.append(dict(hz=hz2,start=0.,end=duration))
        if kind in ('close_hum','hum_only'):
            for h in range(1,45):
                x+=(.15/h**.3)*np.cos(2*np.pi*60*h*t)
        yield dict(index=index,kind=kind,sr=sr,x=x,truth=truth)


def synthetic(iteration):
    if iteration not in ('I001','I002','I002b'):
        raise ValueError('Use I001, I002 or I002b')
    ridge=iteration!='I001'
    folder=begin(iteration, 'Multiresolution ridges recover close/interrupted carriers' if ridge else 'Mains exclusion before candidate allocation recovers useful hypotheses',
        'legacy cap8 vs allocation-only cap8'+(' vs 0.5/2/8-second ridge catalog' if ridge else ''),
        dict(easy_recall_min=.95,negative_catalog_false_positives_reported=True,
             correction_acceptance='not attempted; detector-only, no accepted corrections',
             hard_tier='report separately, never reclassify after failure'),
        synthetic=dict(seed=SEED+'development',count=48,sample_rate=8000,duration_seconds=32,
            carrier_amplitude=.012,noise_sigma=.001,matching='80% truth interval overlap; max(2Hz,FFT-bin) frequency tolerance',
            mask='max(0.5Hz, Hann first null 2/window_seconds); withheld response 2150-2850Hz'))
    legacy=frozen_discover();results=[];tick=time.perf_counter()
    for case in synthetic_cases():
        x,sr=case['x'],case['sr'];base=legacy(x,sr,8);alloc,counts=allocation_only(x,sr,legacy)
        methods=dict(baseline=base,allocation_only=alloc)
        accounting={}
        if ridge:
            methods['ridges'],accounting=discover_ridges(x,sr)
        row=dict(index=case['index'],kind=case['kind'],truth=case['truth'],decoded_sha256=sha(x.tobytes()),
            methods={key:dict(metrics=matching_truth(rows,case['truth']),candidates=rows) for key,rows in methods.items()},accounting=accounting)
        results.append(row)
        print(iteration,case['index'],case['kind'],{k:v['metrics']['recall'] for k,v in row['methods'].items()},flush=True)
    write(folder/'synthetic.json',results)
    summary={}
    for kind in sorted({r['kind'] for r in results}):
        summary[kind]={}
        for method in results[0]['methods']:
            selected=[r['methods'][method]['metrics'] for r in results if r['kind']==kind]
            n=sum(r['truth_count'] for r in selected);found=sum(r['found'] for r in selected)
            groups=sum(r['catalog_groups'] for r in selected);unmatched=sum(r['unmatched_groups'] for r in selected)
            summary[kind][method]=dict(truth=n,found=found,recall=found/max(1,n),
                groups=groups,unmatched=unmatched,precision=(groups-unmatched)/max(1,groups))
    metrics=dict(summary=summary,wall_seconds=time.perf_counter()-tick,accepted_corrections=0,
                 correction_tested=False,development_only=True,synthetic_draws_not_final_audit=True)
    method='ridges' if ridge else 'allocation_only'
    easy=summary['easy'][method]['recall']>=.95
    negatives=sum(summary[k][method]['groups'] for k in ('hum_only','noise_only'))
    reason=f"Easy recall {summary['easy'][method]['recall']:.3f}; close-hum {summary['close_hum'][method]['recall']:.3f}; interrupted {summary['interrupted'][method]['recall']:.3f}; negative-control catalog groups {negatives}. No motion/correction qualification from peaks."
    metrics['peak_working_set_bytes']=memory_peak()
    # Passing easy recall alone is not evidence that allocation fixes its
    # intended blind spots. Preserve the failed narrow hypothesis explicitly.
    useful = ridge or summary['close_hum'][method]['found'] > summary['close_hum']['baseline']['found']
    finish(folder,metrics,'advance_to_development_only' if easy and negatives==0 and useful else 'reject',reason,
        '.venv/Scripts/python.exe research/deflutter_campaign.py survey --iteration I003' if ridge else '.venv/Scripts/python.exe research/deflutter_campaign.py synthetic --iteration I002')


def survey(iteration,passages_file=None):
    passages=read(Path(passages_file))['passages'] if passages_file else read(ROOT/'split_manifest.json')['development_passages']
    folder=begin(iteration,'Dual-source multiresolution catalog adds trackable independent hypotheses',
        'frozen v5 cap8, allocation-only cap8, multiresolution on the declared passages, channels and versions',
        'Account for every requested source/version; no qualified-carrier claim from peak counts; track frozen catalog next',
        catalog_config=CatalogConfig().__dict__,passages=passages)
    invs={r['tape']:r for r in read(ROOT/'inventory.json')['tapes']};legacy=frozen_discover()
    records=[];tick=time.perf_counter();queue=folder/'catalog.jsonl';sources=[]
    for passage in passages:
        inv=invs[passage['tape']]
        for row in inv['channels']+inv.get('unassigned_sources',[]):
            for version in ('original','denoised'):
                lo=max(passage['start_seconds'],row['start_seconds']);hi=min(passage['start_seconds']+passage['duration_seconds'],row['end_seconds'])
                key=f"{inv['tape']}:{row['channel']}:{passage['start_seconds']}:{version}"
                record=dict(key=key,tape=inv['tape'],channel=row['channel'],version=version,start=lo,end=hi,
                    exposure_role=passage.get('role','development'),block=passage.get('block'))
                if hi<=lo:
                    record['status']='unavailable'
                elif version=='denoised' and not row.get('denoised_available'):
                    record['status']='missing_source'
                else:
                    start_wall=time.perf_counter()
                    try:
                        x,sr,source=load_excerpt(inv,row,lo,hi,version)
                        # Three 60-second windows reproduce baseline scale; new
                        # detector likewise uses fixed independent 60s contexts.
                        base=[];alloc=[];ridges=[];accounts=[]
                        for offset in range(0,int(np.ceil(hi-lo)),60):
                            segment=x[round(offset*sr):round(min(offset+60,hi-lo)*sr)]
                            if len(segment)<10*sr:continue
                            for r in legacy(segment,sr,8):
                                base.append(dict(r,start_seconds=offset,end_seconds=min(offset+60,hi-lo)))
                            a,ac=allocation_only(segment,sr,legacy)
                            alloc.extend(dict(r,start_seconds=offset,end_seconds=min(offset+60,hi-lo)) for r in a)
                            rs,account=discover_ridges(segment,sr)
                            for r in rs:
                                r=dict(r,start_seconds=r['start_seconds']+offset,end_seconds=r['end_seconds']+offset)
                                r['candidate_id']=identity(source['source_identity'],lo+r['start_seconds'],lo+r['end_seconds'],r['resolution_seconds'],r['carrier_hz'])
                                # Bind full source path too: metadata alone can
                                # coincide across different sources.
                                r['candidate_id']=identity(source['path'],lo+r['start_seconds'],lo+r['end_seconds'],r['resolution_seconds'],r['carrier_hz'])
                                r.update(source_key=key,tape=inv['tape'],channel=row['channel'],version=version,
                                    source_path=source['path'],passage_start=lo,exposure_role=passage.get('role','development'),block=passage.get('block'))
                                append(queue,r);ridges.append(r)
                            accounts.append(dict(offset=offset,allocation=ac,ridges=account))
                        # Frozen activity descriptor: broadband cleaned/original
                        # envelope variation after 300-2100Hz bandpass. No labels
                        # or response residuals enter this stratification.
                        from scipy import signal
                        filtered=signal.sosfilt(signal.butter(3,(300,2100),fs=sr,btype='bandpass',output='sos'),x)
                        rms=np.sqrt(np.mean(filtered[:len(filtered)//sr*sr].reshape(-1,sr)**2,axis=1))
                        activity=float(np.percentile(rms,90)/max(np.percentile(rms,20),1e-15))
                        record.update(status='analyzed',source=source,sample_rate=sr,baseline=base,allocation_only=alloc,
                            ridge_count=len(ridges),accounting=accounts,activity_ratio_p90_p20=activity,
                            low_activity=activity<2,wall_seconds=time.perf_counter()-start_wall)
                        sources.append(dict(key=key,**source))
                    except Exception as exc:
                        record.update(status='read_error',error=str(exc),traceback=traceback.format_exc())
                records.append(record)
                # Atomic per-source checkpoint; catalog cannot be silently
                # resumed with changed code. A fresh iteration is required.
                write(folder/'checkpoint.json',dict(completed_sources=len(records),last_key=key,
                    catalog_sha256=sha(queue.read_bytes()) if queue.exists() else None,
                    implementation_sha256=read(folder/'experiment.json')['implementation_sha256']))
                write(folder/'source_records.json',records)
                print('survey',len(records),key,record['status'],record.get('ridge_count'),flush=True)
    write(folder/'source_manifest.json',sources)
    exposure(iteration,[dict(key=r['key'],source=r.get('source'),role=r['exposure_role']) for r in records],
        'predictor_features','Frozen dual-source catalog; target validation/final outcomes not evaluated')
    metrics=dict(statuses=dict(Counter(r['status'] for r in records)),wall_seconds=time.perf_counter()-tick,
        baseline_candidates=sum(len(r.get('baseline',[])) for r in records),
        allocation_candidates=sum(len(r.get('allocation_only',[])) for r in records),
        ridge_candidates=sum(r.get('ridge_count',0) for r in records),
        low_activity_sources=sum(r.get('low_activity',False) for r in records),
        requested_source_versions=len(records),available_source_versions=sum(r['status']=='analyzed' for r in records),
        precision_on_real_unknown=True,correction_qualified=0,source_time_seconds=sum(max(0,r['end']-r['start']) for r in records if r['status']=='analyzed'))
    finish(folder,metrics,'catalog_complete_track_next',
        f"{metrics['statuses']}; catalog {metrics['ridge_candidates']} ridges vs {metrics['baseline_candidates']} baseline instances. Counts do not establish usable motion.",
        'Freeze catalog, measure source-union and track a deterministic stratified sample on original and cleaned samples; then independent response qualification.')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='command',required=True)
    sub.add_parser('init');p=sub.add_parser('replay');p.add_argument('--iteration',default='I000')
    p=sub.add_parser('synthetic');p.add_argument('--iteration',required=True,choices=['I001','I002','I002b'])
    p=sub.add_parser('survey');p.add_argument('--iteration',default='I003');p.add_argument('--passages-file')
    args=parser.parse_args()
    if args.command=='init':init()
    elif args.command=='replay':replay(args.iteration)
    elif args.command=='synthetic':synthetic(args.iteration)
    elif args.command=='survey':survey(args.iteration,args.passages_file)


if __name__=='__main__':
    main()
