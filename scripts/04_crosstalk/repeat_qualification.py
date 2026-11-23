"""Qualify discovered repeats against periodic signals before archive audio publication."""
from collections import Counter
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile

import soundfile as sf

from repeat_archive import HERE, SSD, atomic_json, planning_lock, sha256, snapshot_sources
from repeat_catalog import compile_channel
from repeat_cluster import complete
from repeat_periodicity import DelayWitnesses, RATE, in_domain, witness
from repeat_render import read_lease
from repeat_retain import HOLD
from crosstalk_stage import evidence_for
from shared_tape import bound


def immutable_copy(src, dst):
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        if sha256(src) != sha256(dst):
            raise ValueError(f'Existing qualification input differs {dst}')
    else:
        partial = dst.with_suffix(dst.suffix+'.partial')
        shutil.copyfile(src,partial)
        if sha256(partial) != sha256(src):
            raise OSError(f'Qualification input copy failed {src}')
        partial.replace(dst)


def frozen_qualification(base, raw):
    folder = base/'implementation'
    snapshot_sources(folder, immutable_copy)
    probe = HERE.parents[1]/'research/crosstalk_probe.py'
    immutable_copy(probe,folder/probe.name)
    for name in ('repeat_scan.py','repeat_validate.py','repeat_catalog.py','crosstalk_model.py','crosstalk_stage.py'):
        if sha256(folder/name) != sha256(raw/'implementation'/name):
            raise ValueError(f'Qualification and detection use different {name}')
    record = folder/'qualification.json'
    files = {p.name:sha256(p) for p in folder.glob('*.py')}
    value = dict(files=files,tests=['renderer path domain','clock-line rejection','shifted waveform controls',
        'independent donor in multiple residual bands','same-tape delay in two separate speech minutes'],
        delay_witness=dict(tolerance_seconds=.015,maximum_distance_minutes=30,
            minimum_minute_separation=2,timing_target_requires_two_ordinary_target_channels=True))
    if record.exists() and json.loads(record.read_bytes()) != value:
        raise ValueError('Qualification implementation changed')
    if not record.exists():
        atomic_json(record,value)
    return record


def verified_streams(stack, rows, needed, hold):
    streams = {}
    for ch in sorted(needed):
        entry = rows[ch]
        path = hold/entry['name']
        expected = entry['audio']['sha256']
        if not path.exists():
            original = Path(entry['source'])
            if shutil.disk_usage(hold).free < entry['audio']['bytes']:
                raise OSError(f'Insufficient SSD space for {path}')
            partial = path.with_suffix('.partial')
            digest = hashlib.sha256()
            with read_lease(original), original.open('rb') as src, partial.open('wb') as dst:
                for block in iter(lambda:src.read(4<<20),b''):
                    digest.update(block)
                    dst.write(block)
                dst.flush()
                os.fsync(dst.fileno())
            if digest.hexdigest() != expected:
                raise ValueError(f'Changed stage-03 audio {original}')
            partial.replace(path)
        stack.enter_context(read_lease(path))
        if sha256(path) != expected:
            raise ValueError(f'Staged qualification audio differs {path}')
        streams[ch] = stack.enter_context(sf.SoundFile(path))
    return streams


def qualified(folder):
    seal = folder/'qualified.json'
    if not seal.exists():
        return False
    record = json.loads(seal.read_bytes())
    for name,digest in record['files'].items():
        if sha256(folder/name) != digest:
            raise ValueError(f'Changed qualified result {folder/name}')
    return True


def qualify_tape(raw, report, implementation, *, staged=None):
    if qualified(report):
        return
    if not complete(raw):
        raise ValueError(f'Unsealed discovery tape {raw}')
    spec = json.loads((raw/'sources.json').read_bytes())
    tape = spec['tape']
    rows = {r['row']['channel']:r for r in spec['channels']}
    if 'clock_shift_frames' in spec:
        rows = {ch:dict(r, clock_shift_frames=spec['clock_shift_frames'][str(ch)]) for ch,r in rows.items()}
    observations = {ch:json.loads((raw/'validate'/f'CH{ch:02d}.json').read_bytes()) for ch in rows}
    events = {ch:v['events'] for ch,v in observations.items()}
    initial = DelayWitnesses(events,rows)
    accepted = {ch:[] for ch in rows}
    screened = {ch:[] for ch in rows}
    counts = {ch:Counter() for ch in rows}
    needed = set()
    for ch,hits in events.items():
        for e in hits:
            if not in_domain(e):
                counts[ch]['outside renderer path domain'] += 1
            elif not initial.lookup(e,rows[ch]['row']['role']=='timing_only', ch):
                counts[ch]['no independent local delay witnesses'] += 1
            else:
                screened[ch].append(e)
                needed.update((ch,e['donor']))
    hold = staged or HOLD/report.parent.name/tape
    hold.mkdir(parents=True,exist_ok=True)
    with ExitStack() as stack:
        streams = verified_streams(stack,rows,needed,hold)
        for ch,hits in screened.items():
            for e in hits:
                donor = e['donor']
                value, reason = witness(e,streams[ch],streams[donor],
                    round(rows[ch]['row']['start_seconds']*RATE), round(rows[donor]['row']['start_seconds']*RATE))
                if value is None:
                    counts[ch][reason] += 1
                else:
                    accepted[ch].append(value)
    reference = DelayWitnesses(accepted,rows)
    immutable_copy(raw/'sources.json',report/'sources.json')
    scope = {}
    for ch in rows:
        stem = f'CH{ch:02d}'
        selected = []
        for e in accepted[ch]:
            support = reference.lookup(e, rows[ch]['row']['role']=='timing_only', ch)
            if support:
                selected.append(dict(e, independent_delay_witnesses=support))
            else:
                counts[ch]['no independent line-rejected delay witnesses'] += 1
        counts[ch]['qualified_windows'] = len(selected)
        if sum(counts[ch].values()) != len(events[ch]):
            raise ValueError('Incomplete repeat qualification accounting')
        for suffix in ('.npy','.receipt.json'):
            immutable_copy(raw/'scan'/(stem+suffix),report/'scan'/(stem+suffix))
        validation = report/'validate'/(stem+'.json')
        validation.parent.mkdir(parents=True,exist_ok=True)
        output = dict(tape=tape,channel=ch,counts=dict(observations[ch]['counts'],
            qualification=counts[ch]),events=selected)
        atomic_json(validation,output)
        extra = [bound(raw/'detected.json'),bound(raw/'validate'/(stem+'.json')),
                 bound(raw/'validate'/(stem+'.receipt.json')),bound(implementation)]
        atomic_json(report/'validate'/(stem+'.receipt.json'),dict(stage='periodicity_and_local_delay_qualification',
            sha256=sha256(validation),identity=dict(sources=sha256(report/'sources.json'),
                scan=sha256(report/'scan'/(stem+'.npy')),code=sha256(implementation)),inputs=extra))
        final = report/'catalog'/stem
        final.parent.mkdir(parents=True,exist_ok=True)
        # The original compiler binds inputs in report. Assemble its output
        # privately so every published catalog includes the qualification trail.
        with tempfile.TemporaryDirectory(dir=report) as temp:
            prepared = Path(temp)/stem
            compile_channel(report,ch,prepared)
            packed = json.loads((prepared/'reviews.json').read_bytes())
            packed['bindings'].extend(extra)
            atomic_json(prepared/'reviews.json',packed)
            if final.exists():
                previous = {p.relative_to(final):sha256(p) for p in final.rglob('*') if p.is_file()}
                current = {p.relative_to(prepared):sha256(p) for p in prepared.rglob('*') if p.is_file()}
                if previous != current:
                    raise ValueError(f'Conflicting existing final catalog {final}')
            else:
                prepared.rename(final)
        direction, checked, _ = evidence_for(final,tape,ch)
        scope[stem] = dict(raw_windows=len(events[ch]),decisions=counts[ch],direction=direction,
                           eligible_intervals=len(checked))
    files = {p.relative_to(report).as_posix():sha256(p) for p in report.rglob('*')
             if p.is_file() and p.suffix in ('.npy','.json') and p.name != 'qualified.json'}
    atomic_json(report/'qualified.json',dict(status='complete',tape=tape,channels=scope,files=files))
    if staged is None:
        for row in rows.values():
            (hold/row['name']).unlink(missing_ok=True)
        hold.rmdir()
    print(f'Qualified {tape}: {sum(v["eligible_intervals"] for v in scope.values())} eligible intervals',flush=True)
