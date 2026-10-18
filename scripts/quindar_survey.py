"""Resumable Quindar analysis of completed defluttered tapes only.

Original filenames/headers establish expected channels and acquisition clocks;
original audio is never scanned. Every expected non-IRIG channel must finish
stage 05b before the tape-wide analysis can start. CAPCOM affects ordering only.
"""
import argparse
from concurrent.futures import ProcessPoolExecutor, wait, FIRST_COMPLETED
import hashlib
import inspect
import json
from multiprocessing import get_context
import os
from pathlib import Path
import re
import time

import numpy as np
import soundfile as sf

import quindar_speed
from quindar_retime import write_json
from flutter_plan_store import planning_lock

DEFAULT_ANALYSIS_ROOT = Path('D:/Apollo_15_30-Track/quindar_post_deflutter_analysis')
SETTINGS = dict(block_seconds=60, context_seconds=1, frequency_band_hz=[2250,2750],
                post_max_fm_sd_hz=35., post_max_amplitude_ratio=3.)


def implementation():
    code = ''.join(inspect.getsource(f) for f in (quindar_speed.detect, quindar_speed.measure,
                quindar_speed.baseband, quindar_speed.spans, scan_file))
    return hashlib.sha256(code.encode()).hexdigest()


def stamp_seconds(name):
    match = re.search(r'_(\d+)-(\d+)-(\d+)-(\d+)_\d+-\d+-\d+-\d+\.wav$', name, re.I)
    if not match:
        return None
    day, hour, minute, second = map(int, match.groups())
    if hour > 24 or minute > 59 or second > 59:
        return None
    return day*86400+hour*3600+minute*60+second


def file_row(path, tape, stage, channel=None):
    stat, info = path.stat(), sf.info(path)
    match = re.search(r'_CH(\d+)_', path.name, re.I)
    channel = channel if channel is not None else int(match[1]) if match else None
    head = re.search(r'_(HR\d+[UL])_', path.name)
    return dict(tape=tape, stage=stage, channel=channel, path=str(path.resolve()),
                bytes=stat.st_size, mtime_ns=stat.st_mtime_ns, frames=info.frames,
                sample_rate=info.samplerate, audio_channels=info.channels,
                duration_seconds=info.duration, absolute_start=stamp_seconds(path.name),
                head_label=head[1] if head else None, source_label=path.stem,
                timing_role='IRIG' if 'IRIG' in path.name.upper() else 'voice_or_unknown')


def inventory_all(original_root, defluttered_root, tapes=None):
    tapes = set(tapes or [])
    rows, issues = [], []
    folders = sorted(p for p in Path(original_root).glob('A15_T*') if p.is_dir() and (not tapes or p.name in tapes))
    for folder in folders:
        local = []
        for path in sorted(folder.rglob('*.wav')):
            try:
                row = file_row(path, folder.name, 'original')
                local.append(row)
            except Exception as error:
                issues.append(dict(path=str(path), tape=folder.name, error=str(error)))
        starts = [r['absolute_start'] for r in local if r['absolute_start'] is not None]
        origin = min(starts) if starts else None
        capcom = [r['channel'] for r in local if r['channel'] is not None and 'CAPCOM' in r['source_label'].upper()]
        for r in local:
            r['origin_filename_seconds'] = origin
            r['start_seconds'] = r['absolute_start']-origin if r['absolute_start'] is not None and origin is not None else None
            r['id'] = (f'CH{r["channel"]:02d}' if r['channel'] is not None else 'UNIDENTIFIED')+'_'+hashlib.sha256(Path(r['path']).name.encode()).hexdigest()[:8]
            r['priority'] = 0 if r['channel'] in capcom else 1 if r['channel'] is not None and any(abs(r['channel']-c) <= 2 for c in capcom) else 2
        # File metadata is used only to establish readiness and tape placement.
        # Never enqueue original WAVs for Quindar analysis.
        for original in local:
            if original['timing_role']=='IRIG':continue
            if original['channel'] is None:
                issues.append(dict(tape=folder.name,path=original['path'],error='Unidentified audio channel needs identification and stage 05b before exhaustive analysis'))
                continue
            path=Path(defluttered_root)/folder.name/f'CH{original["channel"]:02d}'/'deflutter.wav'
            try:
                review_path = path.parent/'review.json'
                if not path.is_file() or not review_path.is_file():
                    raise ValueError(f'Awaiting completed stage 05b for CH{original["channel"]:02d}')
                review = json.loads(review_path.read_text())
                if review['status'] != 'complete' or review['tape'] != folder.name or review['channel']!=original['channel']:
                    raise ValueError('Deflutter manifest is incomplete or tape mismatches')
                declared=review['source']
                if (Path(declared['path']).resolve()!=Path(original['path']).resolve()
                        or (declared['bytes'],declared['mtime_ns'])!=(original['bytes'],original['mtime_ns'])):
                    raise ValueError('Defluttered file uses a different original source version')
                if (review['start_seconds']!=original['start_seconds'] or review['frames']!=original['frames']
                        or declared['absolute_start']-declared['start_seconds']!=origin):
                    raise ValueError('Full-channel stage 05b completion on the common acquisition clock is required')
                row = file_row(path, folder.name, 'defluttered', review['channel'])
                if row['frames'] != review['frames'] or row['sample_rate'] != review['sample_rate']:
                    raise ValueError('Deflutter manifest and audio header disagree')
                row.update(origin_filename_seconds=origin, start_seconds=review['start_seconds'],
                           id=f'CH{row["channel"]:02d}', priority=original['priority'],
                           review_sha256=hashlib.sha256(review_path.read_bytes()).hexdigest(),
                           expected_decoded_float32_sha256=review['audio']['deflutter']['decoded_float32_sha256'],
                           original_source=original,
                           source_label=Path(review['source']['path']).stem,
                           experimental_transfer=review.get('experimental_transfer',False))
                rows.append(row)
            except Exception as error:
                issues.append(dict(path=str(path), tape=folder.name, error=str(error)))
    return rows, issues


def scan_file(row, destination, identity):
    if row.get('stage')!='defluttered':
        raise ValueError('Stage 05c only scans completed defluttered audio')
    path, destination = Path(row['path']), Path(destination)
    sr = row['sample_rate']
    if row['audio_channels'] != 1 or sr < 6000 or sr % 1000:
        raise ValueError('Requires mono sample rate >=6000 and divisible by 1000')
    stat = path.stat()
    if (stat.st_size, stat.st_mtime_ns) != (row['bytes'], row['mtime_ns']):
        raise ValueError('Source changed since inventory')
    progress_file = destination.with_suffix('.progress.json')
    events, digest = [], hashlib.sha256()
    started = time.monotonic()
    with sf.SoundFile(path) as f:
        n = len(f)
        block, halo = 60*sr, sr
        for index, begin in enumerate(range(0,n,block)):
            left, right = max(0, begin-halo), min(n,begin+block+halo)
            f.seek(left)
            x = f.read(right-left)
            if not np.isfinite(x).all():
                raise ValueError('Non-finite source audio')
            core = x[begin-left:min(begin+block,n)-left].astype(np.float32)
            digest.update(core.tobytes())
            events.extend(quindar_speed.detect(x,sr,left/sr,begin/sr,min(begin+block,n)/sr,
                          max_fm_hz=35., max_amplitude_ratio=3.))
            if index % 30 == 0:
                write_json(progress_file,dict(processed_seconds=min(begin+block,n)/sr,
                           total_seconds=n/sr,candidates=len(events),elapsed_seconds=time.monotonic()-started))
    after = path.stat()
    if (after.st_size, after.st_mtime_ns) != (row['bytes'], row['mtime_ns']):
        raise ValueError('Source changed during discovery')
    if digest.hexdigest()!=row['expected_decoded_float32_sha256']:
        raise ValueError('Defluttered audio content differs from the completed stage-05b manifest')
    result = dict(schema=1,status='complete',source=row,settings=SETTINGS,
                  implementation_sha256=identity,scan_start=0.,scan_end=n/sr,
                  input_decoded_float32_sha256=digest.hexdigest(),events=events,
                  elapsed_seconds=time.monotonic()-started)
    result['events_sha256'] = hashlib.sha256(json.dumps(events,separators=(',',':')).encode()).hexdigest()
    write_json(destination,result)
    if progress_file.exists():
        progress_file.unlink()
    return dict(path=str(destination),tape=row['tape'],stage=row['stage'],channel=row['channel'],
                id=row['id'],candidates=len(events),elapsed_seconds=result['elapsed_seconds'],
                sha256=hashlib.sha256(destination.read_bytes()).hexdigest(),status='complete')


def survey(original_root,defluttered_root,output,tapes=None,workers=8,max_files=None):
    original_root,defluttered_root,output = map(lambda x:Path(x).resolve(),(original_root,defluttered_root,output))
    if any(output==p or output in p.parents or p in output.parents for p in [original_root,defluttered_root]):
        raise ValueError('Survey output must be separate from source roots')
    if not 1 <= workers <= 32:
        raise ValueError('Use 1-32 workers')
    rows,issues=inventory_all(original_root,defluttered_root,tapes)
    if issues:
        counts={}
        for issue in issues:counts[issue['tape']]=counts.get(issue['tape'],0)+1
        detail=', '.join(f'{tape}: {count} pending/invalid channels' for tape,count in sorted(counts.items()))
        raise ValueError('Stage 05c survey not started: finish stage 05b for every expected audio channel. '+detail)
    if not rows:
        raise ValueError('No available channel files')
    identity=implementation()
    with planning_lock(output):
        output.mkdir(parents=True,exist_ok=True)
        pending,completed=[],[]
        for row in rows:
            dest=output/row['tape']/'survey'/row['stage']/(row['id']+'.json')
            dest.parent.mkdir(parents=True,exist_ok=True)
            if dest.exists():
                saved=json.loads(dest.read_text())
                if saved['source'] != row or saved['settings'] != SETTINGS or saved['implementation_sha256'] != identity:
                    issues.append(dict(path=str(dest),tape=row['tape'],error='Completed discovery source/settings changed; preserved result requires a new analysis root'))
                    continue
                if saved.get('status') != 'complete' or hashlib.sha256(json.dumps(saved['events'],separators=(',',':')).encode()).hexdigest()!=saved['events_sha256']:
                    issues.append(dict(path=str(dest),tape=row['tape'],error='Completed discovery integrity check failed'))
                    continue
                completed.append(dict(path=str(dest),tape=row['tape'],stage=row['stage'],channel=row['channel'],id=row['id'],
                                      candidates=len(saved['events']),status='complete',sha256=hashlib.sha256(dest.read_bytes()).hexdigest()))
            else:
                pending.append((row,dest))
        pending.sort(key=lambda pair:(pair[0]['priority'],pair[0]['tape'],pair[0]['id']))
        index=dict(schema=2,status='running',input_stage='defluttered',complete_defluttered_coverage=True,
                   original_root=str(original_root),defluttered_root=str(defluttered_root),
                   implementation_sha256=identity,inventory=rows,issues=issues,results=completed,
                   targets=[r['original_source'] for r in rows],
                   total_files=len(rows),workers=workers,scope='All completed non-IRIG stage-05b channels on each selected tape; no original-audio analysis')
        write_json(output/'survey.json',index)
        scheduled=pending[:max_files] if max_files is not None else pending
        print(f'{len(rows)} files; {sum(r["bytes"] for r in rows)/1e9:.1f} GB; {len(completed)} reused; {len(scheduled)} queued; {workers} workers',flush=True)
        started=time.monotonic()
        with ProcessPoolExecutor(max_workers=workers,mp_context=get_context('spawn')) as pool:
            active={}
            iterator=iter(scheduled)
            def fill():
                while len(active)<workers:
                    item=next(iterator,None)
                    if item is None:break
                    row,dest=item
                    active[pool.submit(scan_file,row,dest,identity)]=row
            fill()
            while active:
                done,_=wait(active,timeout=30,return_when=FIRST_COMPLETED)
                for future in done:
                    row=active.pop(future)
                    try:
                        result=future.result()
                        completed.append(result)
                        print(f'{len(completed)}/{len(rows)} {row["tape"]} {row["stage"]} {row["id"]}: {result["candidates"]} candidates, {result["elapsed_seconds"]:.1f}s',flush=True)
                    except Exception as error:
                        issues.append(dict(path=row['path'],tape=row['tape'],error=str(error)))
                        print(f'FAILED {row["tape"]} {row["id"]}: {error}',flush=True)
                fill()
                index.update(results=completed,issues=issues,elapsed_seconds=time.monotonic()-started)
                write_json(output/'survey.json',index)
                if not done:
                    print(f'Survey running: {len(completed)}/{len(rows)} complete; {len(active)} active; elapsed {(time.monotonic()-started)/60:.1f} min',flush=True)
        index.update(status='complete' if len(completed)==len(rows) and not issues else 'partial',results=completed,issues=issues)
        write_json(output/'survey.json',index)
        return index


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--original-root',type=Path,default=Path('D:/Apollo_15_30-Track/orig_concatenated'))
    p.add_argument('--defluttered-root',type=Path,default=Path('D:/Apollo_15_30-Track/defluttered'))
    p.add_argument('--output',type=Path,default=DEFAULT_ANALYSIS_ROOT)
    p.add_argument('--tape',action='append')
    p.add_argument('--workers',type=int,default=8)
    p.add_argument('--max-files',type=int,help='Bounded checkpoint trial; omit for exhaustive completion')
    args=p.parse_args()
    try:
        survey(args.original_root,args.defluttered_root,args.output,args.tape,args.workers,args.max_files)
    except (ValueError,OSError,RuntimeError,KeyError,TypeError) as error:
        p.error(str(error))


if __name__=='__main__':
    main()
