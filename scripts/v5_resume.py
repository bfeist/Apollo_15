"""Durable survey storage around the unchanged v5 scientific engine.

Each window is analyzed by v5_allchannel.survey_tape, then committed with the
coverage and candidate journal offsets and digests. Uncommitted tails are
discarded on restart. Keeping the engine unchanged preserves existing plan
implementation signatures.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil

import v5_allchannel as engine
from flutter_planning import inventory
from flutter_policy import implementation_digest, digest
from denoise_archive import CANONICAL
from v5_compatibility import SURVEY_STORAGE_DIGESTS

CHECKPOINT = 'survey_checkpoint.json'
JOURNAL = 'survey_windows.jsonl'


def resume_root(requested, saved, windows):
    resolved = engine.discovery_root(saved, windows)
    key = engine.denoised_root_key(requested)
    if key == resolved or (key == saved and key is not None and CANONICAL in Path(key).parents):
        return resolved
    raise ValueError('V5 denoised discovery root changed')


def saved_survey_root(output, requested):
    for name in ('survey_manifest.json', 'survey_live_snapshot.json'):
        path = output/name
        if path.is_file():
            saved = json.loads(path.read_text(encoding='utf-8'))
            return resume_root(requested, saved.get('denoised_root'), saved['windows'])
    path = output/CHECKPOINT
    if path.is_file():
        state = json.loads(path.read_text(encoding='utf-8'))
        with (output/JOURNAL).open('rb') as stream:
            windows = [json.loads(line)['window'] for line in stream.read(state['journal_bytes']).splitlines()]
        return resume_root(requested, state['signature']['denoised_root'], windows)
    return requested


def _sync(stream):
    stream.flush()
    os.fsync(stream.fileno())


def _publish(path, value):
    temporary = path.with_suffix(path.suffix+'.partial')
    with temporary.open('w', encoding='utf-8') as stream:
        json.dump(value, stream, allow_nan=False)
        _sync(stream)
    temporary.replace(path)


def _denoised_identity(root, inv):
    if root is None:
        return None
    result = {}
    for row in inv['channels']:
        folder = Path(root)/f'CH{row["channel"]:02d}'
        identity = {}
        for name in ('denoised.wav', 'denoised.json'):
            path = folder/name
            if path.exists():
                stat = path.stat()
                identity[name] = [stat.st_size, stat.st_mtime_ns]
                if name.endswith('.json'):
                    identity['manifest_sha256'] = digest(path)
        result[str(row['channel'])] = identity
    return result


def _validate_discovery(windows, identities):
    for window in windows:
        for channel, result in window['channels'].items():
            clean = result.get('discovery_sources', {}).get('denoised')
            if not isinstance(clean, dict) or clean.get('status') != 'analyzed':
                continue
            current = (identities or {}).get(channel, {})
            if (current.get('denoised.wav') != [clean['bytes'], clean['mtime_ns']]
                or current.get('manifest_sha256') != clean['manifest_sha256']):
                raise ValueError('Denoised survey source changed since checkpoint')


def _signature(source, start, duration, monitor_seconds, context_seconds,
               candidates_per_channel, denoised_root):
    inv = inventory(source)
    return dict(inventory=inv, start=start, duration=duration,
        monitor_seconds=monitor_seconds, context_seconds=context_seconds,
        candidates_per_channel=candidates_per_channel,
        denoised_root=str(Path(denoised_root).resolve()) if denoised_root is not None else None,
        denoised_identity=_denoised_identity(denoised_root, inv),
        implementation_sha256=implementation_digest(), storage_sha256=digest(__file__))


def _prefix_hash(path, size):
    value = hashlib.sha256()
    with path.open('rb') as stream:
        remaining = size
        while remaining:
            block = stream.read(min(4*1024*1024, remaining))
            if not block:
                raise ValueError(f'Survey checkpoint file is truncated: {path}')
            value.update(block)
            remaining -= len(block)
    return value


def repair_planner_tails(output):
    """Verify committed stream prefixes before removing work after a checkpoint.

    The legacy engine appends streams before publishing its next plan. Its
    normal reader checks entire files, so a mid-window shutdown needs this
    recovery step before that reader can restore the committed tracker state.
    """
    folder = Path(output)/'plan'
    plan = json.loads((folder/'plan.json').read_text(encoding='utf-8'))
    if plan['complete']:
        return []
    state_path = (folder/plan['checkpoint_file']).resolve()
    if folder.resolve() not in state_path.parents or digest(state_path) != plan['checkpoint_sha256']:
        raise ValueError('Planning checkpoint changed')
    state = json.loads(state_path.read_text(encoding='utf-8'))
    streams = plan['regions'][0]['maps']['streams']
    pending = []
    for session in state['sessions']:
        descriptor = session['descriptor']
        if streams.get(session['key']) != descriptor:
            raise ValueError('Planning checkpoint and stream index disagree')
        path = (folder/descriptor['file']).resolve()
        size = descriptor['frames']*16
        if folder.resolve() not in path.parents or size < 0:
            raise ValueError('Invalid checkpoint stream path or size')
        if _prefix_hash(path, size).hexdigest() != descriptor['sha256']:
            raise ValueError('Committed planning stream changed')
        if path.stat().st_size > size:
            pending.append((path,size))
    for path,size in pending:
        with path.open('r+b') as stream:
            stream.truncate(size)
            _sync(stream)
    return [str(path) for path,_ in pending]


def _load(output, signature):
    state = json.loads((output/CHECKPOINT).read_text(encoding='utf-8'))
    hashes = {}
    for name, prefix in (('survey.jsonl', 'coverage'), (JOURNAL, 'journal')):
        path = output/name
        value = _prefix_hash(path, state[prefix+'_bytes'])
        if value.hexdigest() != state[prefix+'_sha256']:
            raise ValueError(f'Survey checkpoint file changed: {name}')
        hashes[prefix] = value
    with (output/JOURNAL).open('rb') as stream:
        windows = [json.loads(line)['window'] for line in stream.read(state['journal_bytes']).splitlines()]
    stored = dict(state['signature'])
    stored['denoised_root'] = engine.discovery_root(stored['denoised_root'], windows)
    current = hashlib.sha256(Path(__file__).read_bytes().replace(b'\r\n', b'\n')).hexdigest()
    if stored['storage_sha256'] == SURVEY_STORAGE_DIGESTS.get(current):
        stored['storage_sha256'] = signature['storage_sha256']
    if stored != signature:
        raise ValueError('Survey checkpoint sources/settings/implementation changed')
    _validate_discovery(windows, signature['denoised_identity'])
    # Check both committed files and identities before trimming uncommitted tails.
    for name, prefix in (('survey.jsonl', 'coverage'), (JOURNAL, 'journal')):
        with (output/name).open('r+b') as stream:
            stream.truncate(state[prefix+'_bytes'])
            _sync(stream)
    if len(windows) != state['windows'] or (windows and windows[-1]['end_seconds'] != state['cursor']):
        raise ValueError('Survey checkpoint journal and cursor disagree')
    return state, hashes, windows


def _commit(output, state, hashes, window, coverage):
    with (output/'survey.jsonl').open('ab') as stream:
        stream.write(coverage)
        _sync(stream)
        state['coverage_bytes'] = stream.tell()
    hashes['coverage'].update(coverage)
    entry = json.dumps(dict(window=window, coverage_end=state['coverage_bytes']), allow_nan=False).encode()+b'\n'
    with (output/JOURNAL).open('ab') as stream:
        stream.write(entry)
        _sync(stream)
        state['journal_bytes'] = stream.tell()
    hashes['journal'].update(entry)
    state.update(cursor=window['end_seconds'], windows=state['windows']+1,
        coverage_sha256=hashes['coverage'].hexdigest(), journal_sha256=hashes['journal'].hexdigest())
    _publish(output/CHECKPOINT, state)


def import_live_snapshot(source, output, *, start=0., duration=None,
                         monitor_seconds=60., context_seconds=4., candidates_per_channel=8,
                         denoised_root=None):
    """Import the pre-reboot capture; retain only complete coverage windows."""
    output = Path(output)
    denoised_root = saved_survey_root(output, denoised_root)
    signature = _signature(source, start, duration, monitor_seconds, context_seconds,
                           candidates_per_channel, denoised_root)
    snapshot = json.loads((output/'survey_live_snapshot.json').read_text(encoding='utf-8'))
    if (snapshot['inv'] != signature['inventory'] or snapshot['start'] != start
        or snapshot['monitor_seconds'] != monitor_seconds or snapshot['context_seconds'] != context_seconds
        or snapshot['candidates_per_channel'] != candidates_per_channel
        or engine.discovery_root(snapshot['denoised_root'], snapshot['windows']) != signature['denoised_root']):
        raise ValueError('Captured survey sources or settings changed')
    if (output/CHECKPOINT).exists():
        return _load(output, signature)[0]
    expected_end = min(snapshot['source_end'], start+duration) if duration is not None else snapshot['source_end']
    if expected_end != snapshot['end']:
        raise ValueError('Captured survey duration changed')
    _validate_discovery(snapshot['windows'], signature['denoised_identity'])
    channels = {str(r['channel']) for r in snapshot['inv']['channels']+snapshot['unassigned']}
    channels.update(str(ch) for ch in snapshot['inv']['missing_channels'])
    value = hashlib.sha256()
    coverage_hash = hashlib.sha256()
    cursor, count, coverage_end = start, 0, 0
    # Preserve the old bytes, including any uncommitted tail, for recovery.
    backup = output/'survey_before_checkpoint.jsonl'
    if not backup.exists():
        shutil.copy2(output/'survey.jsonl', backup)
    with backup.open('rb') as stream, (output/JOURNAL).open('wb') as journal:
        for window in snapshot['windows']:
            low, high = window['start_seconds'], window['end_seconds']
            # Capture may happen just before the legacy final manifest is
            # published. Recompute its final window to recover that metadata.
            if high >= snapshot['end']-1e-8:
                break
            if low != cursor:
                raise ValueError('Captured survey windows have a gap')
            ends = {ch:low for ch in channels}
            chunk = bytearray()
            valid = True
            while True:
                position = stream.tell()
                line = stream.readline()
                if not line:
                    break
                try:
                    row = json.loads(line)
                except ValueError:
                    valid = False
                    break
                a, b, ch = row['start_seconds'], row['end_seconds'], str(row['channel'])
                if a >= high-1e-8:
                    stream.seek(position)
                    break
                if ch not in ends or abs(a-ends[ch]) > 1e-7 or not a < b <= high+1e-7:
                    valid = False
                    break
                ends[ch] = b
                chunk.extend(line)
            if not valid or any(abs(end-high) > 1e-7 for end in ends.values()):
                break
            coverage_hash.update(chunk)
            coverage_end = stream.tell()
            entry = json.dumps(dict(window=window, coverage_end=coverage_end), allow_nan=False).encode()+b'\n'
            journal.write(entry)
            value.update(entry)
            cursor, count = high, count+1
        _sync(journal)
        journal_end = journal.tell()
    state = dict(schema=1, signature=signature, cursor=cursor, windows=count,
        coverage_bytes=coverage_end, coverage_sha256=coverage_hash.hexdigest(),
        journal_bytes=journal_end, journal_sha256=value.hexdigest())
    # State publication precedes tail removal; recovery can always trim it later.
    _publish(output/CHECKPOINT, state)
    with (output/'survey.jsonl').open('r+b') as stream:
        stream.truncate(coverage_end)
        _sync(stream)
    return state


def survey_tape(source, output, *, start=0., duration=None, monitor_seconds=60.,
                context_seconds=4., candidates_per_channel=8, denoised_root=None,
                resume=False, max_windows=None):
    output = Path(output)
    if resume and not (output/CHECKPOINT).exists() and not (output/'survey_live_snapshot.json').exists():
        raise ValueError('No survey checkpoint; this older interrupted survey needs a fresh destination')
    if resume:
        denoised_root = saved_survey_root(output, denoised_root)
    signature = _signature(source, start, duration, monitor_seconds, context_seconds,
                           candidates_per_channel, denoised_root)
    if resume and not (output/CHECKPOINT).exists() and (output/'survey_live_snapshot.json').exists():
        import_live_snapshot(source, output, start=start, duration=duration,
            monitor_seconds=monitor_seconds, context_seconds=context_seconds,
            candidates_per_channel=candidates_per_channel, denoised_root=denoised_root)
    if resume:
        if not (output/CHECKPOINT).exists():
            raise ValueError('No survey checkpoint; this older interrupted survey needs a fresh destination')
        state, hashes, windows = _load(output, signature)
        print(f'{Path(source).name}: resuming survey at {state["cursor"]:.3f}s ({state["windows"]} committed windows)', flush=True)
    else:
        output.mkdir(parents=True, exist_ok=False)
        for name in ('survey.jsonl', JOURNAL):
            (output/name).touch()
        hashes = dict(coverage=hashlib.sha256(), journal=hashlib.sha256())
        state = dict(schema=1, signature=signature, cursor=start, windows=0,
            coverage_bytes=0, coverage_sha256=hashes['coverage'].hexdigest(),
            journal_bytes=0, journal_sha256=hashes['journal'].hexdigest())
        windows = []
        _publish(output/CHECKPOINT, state)
    scratch = output/'.survey_window'
    count = 0
    while True:
        # Only this private scratch folder may be discarded after a crash.
        if scratch.resolve().parent != output.resolve():
            raise ValueError('Invalid survey scratch destination')
        if scratch.exists():
            shutil.rmtree(scratch)
        cursor = state['cursor']
        if state.get('manifest') is not None and cursor >= state['manifest']['requested_end_seconds']-1e-8:
            break
        remaining = None if duration is None else start+duration-cursor
        if remaining is not None and remaining <= 1e-8:
            break
        interval = monitor_seconds if remaining is None else min(monitor_seconds, remaining)
        manifest = engine.survey_tape(source, scratch, start=cursor, duration=interval,
            monitor_seconds=monitor_seconds, context_seconds=context_seconds,
            candidates_per_channel=candidates_per_channel, max_windows=1, denoised_root=denoised_root)
        # The original engine writes an explicit tail for bounded research.
        # Only the analyzed window is appended to the durable coverage file.
        window = manifest['windows'][0]
        end = min(manifest['source_end_seconds'], start+duration) if duration is not None else manifest['source_end_seconds']
        template = {key:value for key,value in manifest.items() if key != 'windows'}
        template.update(start_seconds=start, requested_end_seconds=end)
        template['sources_before_timeline_origin'] = [dict(path=r['path'], start_seconds=r['start_seconds'],
            end_seconds=min(0.,r['end_seconds']), status='outside_requested_timeline')
            for r in template['unassigned_sources'] if r['start_seconds'] < 0]
        for row in template['unassigned_sources']:
            if row.get('error'):
                row.update(start_seconds=start, end_seconds=end)
        state['manifest'] = template
        coverage = bytearray()
        with (scratch/'survey.jsonl').open('rb') as stream:
            for line in stream:
                row = json.loads(line)
                if row['start_seconds'] >= window['end_seconds']-1e-8:
                    break
                coverage.extend(line)
        _commit(output, state, hashes, window, bytes(coverage))
        windows.append(window)
        count += 1
        print(f'{Path(source).name}: survey checkpoint through {state["cursor"]:.3f}s / {end:.3f}s', flush=True)
        if state['cursor'] >= end-1e-8:
            break
        if max_windows is not None and count >= max_windows:
            return dict(complete=False, surveyed_end_seconds=state['cursor'], windows=windows)
    # A checkpoint may already contain the entire interval after a crash.
    if 'manifest' not in state:
        raise ValueError('Survey endpoint metadata missing; resume requires a final window')
    manifest = dict(state['manifest'])
    manifest.update(start_seconds=start, windows=windows,
        complete=True, surveyed_end_seconds=state['cursor'],
        all_recorded_sources_covered=all(c['status']=='analyzed' for w in windows
            for c in w['channels'].values() if c['context_end']>c['context_start']),
        coverage_file='survey.jsonl')
    _publish(output/'survey_manifest.json', manifest)
    if scratch.exists():
        shutil.rmtree(scratch)
    return manifest


def plan_tape(source, output, *, resume=False, **settings):
    source, output = Path(source).resolve(), Path(output).resolve()
    if source == output or source in output.parents or output in source.parents:
        raise ValueError('Input and output must be non-overlapping')
    manifest_path = output/'survey_manifest.json'
    if resume or manifest_path.exists():
        settings['denoised_root'] = saved_survey_root(output, settings.get('denoised_root'))
    if resume and (output/'plan'/'plan.json').exists():
        repair_planner_tails(output)
        return engine.plan_tape(source, output, resume=True, **settings)
    survey_settings = {k:v for k,v in settings.items() if k in {
        'start','duration','monitor_seconds','context_seconds','candidates_per_channel','denoised_root'}}
    if not manifest_path.exists():
        survey_tape(source, output, resume=resume, **survey_settings)
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    # Validate completed surveys with the same immutable source/settings binding.
    expected = _signature(source, settings.get('start',0.), settings.get('duration'),
        settings.get('monitor_seconds',60.), settings.get('context_seconds',4.),
        settings.get('candidates_per_channel',8), settings.get('denoised_root'))
    if (output/CHECKPOINT).exists():
        state, _, committed_windows = _load(output, expected)
        if manifest['windows'] != committed_windows or manifest['surveyed_end_seconds'] != state['cursor']:
            raise ValueError('Completed survey and checkpoint journal disagree')
    if (manifest['inventory'] != expected['inventory'] or not manifest['complete']
        or manifest['start_seconds'] != expected['start']
        or manifest['monitor_seconds'] != expected['monitor_seconds']
        or manifest['context_seconds'] != expected['context_seconds']
        or manifest['candidates_per_channel'] != expected['candidates_per_channel']
        or engine.discovery_root(manifest.get('denoised_root'), manifest['windows']) != expected['denoised_root']):
        raise ValueError('V5 survey source or settings changed')
    expected_end = min(manifest['source_end_seconds'], expected['start']+expected['duration']) if expected['duration'] is not None else manifest['source_end_seconds']
    if manifest['requested_end_seconds'] != expected_end:
        raise ValueError('V5 survey duration changed')
    _validate_discovery(manifest['windows'], expected['denoised_identity'])
    kwargs = {k:v for k,v in settings.items() if k != 'denoised_root'}
    return engine.continuous_planning.plan_tape(source, output/'plan', **kwargs,
        candidate_provider=lambda ch,low,high:engine.surveyed_candidates(manifest,ch,low,high),
        methodology_override=engine.METHODOLOGY, reuse=False, resume=False,
        allow_transfer_group=False, tolerate_read_errors=True,
        survey_signature=dict(manifest_sha256=digest(manifest_path), coverage_sha256=digest(output/'survey.jsonl')))
