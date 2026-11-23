"""Stage whole denoised tapes sequentially on Genus and execute the frozen workers there."""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time


ROOT = Path.home()/'.local/share/apollo15-crosstalk'
REMOTE_D = Path('/mnt/d/Apollo_15_30-Track')
STAGE = Path(__file__).resolve().parents[1]


def digest(path):
    with path.open('rb') as data:
        return hashlib.file_digest(data,'sha256').hexdigest()


def save(path,value):
    partial = path.with_suffix(path.suffix+'.partial')
    partial.write_text(json.dumps(value,indent=2,allow_nan=False),encoding='utf-8')
    partial.replace(path)


def prefetch(local,tape):
    local.mkdir(parents=True,exist_ok=True)
    records = {}
    for manifest in sorted((REMOTE_D/'denoised'/tape).glob('CH[0-9][0-9]/denoised.json')):
        raw = manifest.read_bytes()
        saved = json.loads(raw)
        if saved['status'] != 'complete':
            continue
        if saved['cleanup_version'] != 'v2' or saved['action'] != 'denoise_v2':
            raise ValueError(f'Unexpected input: {manifest}')
        ch = manifest.parent.name
        audio = saved['audio']
        if Path(audio['filename']).name != audio['filename']:
            raise ValueError(f'Invalid WAV filename {manifest}')
        dst = local/(ch+'.wav')
        records[ch] = dict(sha256=audio['sha256'],bytes=audio['bytes'],manifest=hashlib.sha256(raw).hexdigest())
        if dst.exists():
            if dst.stat().st_size != audio['bytes'] or digest(dst) != audio['sha256']:
                raise ValueError(f'Changed local tape input {dst}')
            continue
        if shutil.disk_usage(local).free < audio['bytes']+25*(1<<30):
            raise OSError(f'Not enough native SSD space for {dst}')
        partial = dst.with_suffix('.partial')
        h = hashlib.sha256()
        # One sequential network reader per tape. All channel workers open
        # native files only after the copy has passed its independent readback.
        with (manifest.parent/audio['filename']).open('rb') as src, partial.open('wb') as out:
            for block in iter(lambda:src.read(4<<20),b''):
                h.update(block)
                out.write(block)
            out.flush()
            os.fsync(out.fileno())
        if h.hexdigest() != audio['sha256'] or digest(partial) != audio['sha256']:
            raise ValueError(f'Failed native SSD copy {dst}')
        partial.replace(dst)
        print(f'Staged {tape} {ch} on Genus SSD',flush=True)
    if not records:
        raise ValueError(f'No complete stage-03 sources for {tape}')
    save(local/'prefetch.json',records)


def process(local,run,tape,nworkers):
    archive = REMOTE_D/'stage_evidence/archive'
    evidence = archive if (archive/'layout.json').is_file() else REMOTE_D
    detection = evidence/'crosstalk_detection'/run
    report = detection/tape
    spec_bytes = (report/'sources.json').read_bytes()
    spec = json.loads(spec_bytes)
    records = json.loads((local/'prefetch.json').read_bytes())
    expected = {Path(e['name']).stem:dict(sha256=e['audio']['sha256'],bytes=e['audio']['bytes'],
                manifest=e['manifest_sha256']) for e in spec['channels']}
    if records != expected:
        raise ValueError('Native SSD cache differs from canonical Windows inventory')
    (local/'sources.json').write_bytes(spec_bytes)
    source_hash = hashlib.sha256(spec_bytes).hexdigest()
    for mode in ('scan','validate'):
        # Workers execute the exact science bytes sealed at launch on Windows.
        # All numerical inputs and outputs are local until the small result is closed.
        name = f'repeat_{mode}.py'
        immutable = detection/'implementation'/name
        program = local/name
        shutil.copyfile(immutable,program)
        code = digest(program)
        result_dir = local/mode
        result_dir.mkdir(exist_ok=True)
        shared = report/mode
        shared.mkdir(exist_ok=True)

        def channel(ch):
            suffix = '.npy' if mode == 'scan' else '.json'
            output = result_dir/(ch+suffix)
            receipt = shared/(ch+'.receipt.json')
            identity = dict(code=code,sources=source_hash)
            scan = local/'scan'/(ch+'.npy')
            if mode == 'validate':
                identity['scan'] = digest(scan)
            if receipt.exists():
                record = json.loads(receipt.read_bytes())
                if record['identity'] != identity or digest(shared/output.name) != record['sha256']:
                    raise ValueError(f'Stale receipt {receipt}')
                if not output.exists() or digest(output) != record['sha256']:
                    shutil.copyfile(shared/output.name,output)
                if digest(output) != record['sha256']:
                    raise ValueError(f'Changed imported result {output}')
                return
            args = [sys.executable,'-u',str(program),str(local/(ch+'.wav')),str(output)]
            if mode == 'validate':
                args.append(str(scan))
            started = time.monotonic()
            with (result_dir/(ch+'.log')).open('w') as log:
                subprocess.run(args,stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT,check=True)
            value = digest(output)
            upload = shared/(output.name+'.native-partial')
            shutil.copyfile(output,upload)
            if digest(upload) != value:
                raise ValueError(f'Failed evidence upload {upload}')
            upload.replace(shared/output.name)
            save(receipt,dict(identity=identity,sha256=value,host='Genus',storage='native SSD',
                              worker_seconds=time.monotonic()-started))
            print(f'{mode} {tape} {ch} Genus SSD',flush=True)
        with ThreadPoolExecutor(max_workers=nworkers) as pool:
            for future in as_completed([pool.submit(channel,ch) for ch in sorted(records)]):
                future.result()


def reclaim(local):
    # This private cache is solely for this run/tape. Require known names at
    # every level and leave unfamiliar files untouched for inspection.
    for directory in ('scan','validate'):
        folder=local/directory
        if folder.is_dir():
            for item in folder.iterdir():
                if item.is_symlink() or not item.is_file() or not re.fullmatch(r'CH\d{2}\.(npy|json|log)',item.name):
                    raise ValueError(f'Unexpected cache entry {item}')
    allowed = {'sources.json','prefetch.json','repeat_scan.py','repeat_validate.py','scan','validate','__pycache__'}
    for item in local.iterdir():
        if item.is_symlink() or (item.name not in allowed and not re.fullmatch(r'CH\d{2}\.wav',item.name)):
            raise ValueError(f'Unexpected cache entry {item}')
    if local.resolve() != local or local.parents[1] != ROOT:
        raise ValueError('Unexpected native cache path')
    shutil.rmtree(local)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['prefetch','process','reclaim'])
    parser.add_argument('run')
    parser.add_argument('tape')
    parser.add_argument('--workers',type=int,default=6)
    args = parser.parse_args()
    if not re.fullmatch(r'[A-Za-z0-9_-]+',args.run) or not re.fullmatch(r'A15_T\d+',args.tape) or not 1<=args.workers<=6:
        parser.error('Expected a simple run name, canonical tape name and 1–6 workers')
    local = ROOT/args.run/args.tape
    if args.action=='prefetch':
        prefetch(local,args.tape)
    elif args.action=='process':
        process(local,args.run,args.tape,args.workers)
    else:
        reclaim(local)
