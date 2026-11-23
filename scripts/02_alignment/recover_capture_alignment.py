"""Resume the archive selectively using measured capture offsets and prior qualified releases."""
import argparse
from collections import Counter, defaultdict
from importlib import import_module
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

for variable in ('OPENBLAS_NUM_THREADS','OMP_NUM_THREADS','MKL_NUM_THREADS'):
    os.environ[variable]='1'
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from _bootstrap import command, install
install()

import flutter_analysis as analysis
import flutter_detection as detection
import flutter_followup as followup
import flutter_qualification as qualification
from capture_highband import check
from capture_publication import PublicationBlocked, archive_renderer_hash, release_coordinator_hash, rename_for_publication
from denoise_archive import atomic_json, sha256
from flutter_alignment import ORIGINALS, ROOT
from flutter_planning import inventory
from flutter_plan_store import planning_lock
from repeat_realign import immutable_json, recover_stage04
from tape_coordinates import aligned_inventory

DATA=ORIGINALS.parent
PRIOR=Path('census-0a7f22d8282355c1/analysis-1d46441629e82576')
OLD_ANALYSIS=DATA/'deflutter_analysis'/PRIOR
OLD_PLANS=DATA/'deflutter_planning_qualified'/PRIOR
REPORTS=DATA/'capture_alignment_recovery'


def log(message):
    print(message,flush=True)


def inspect_archive():
    prior=analysis.read_json(OLD_ANALYSIS/'input_manifest.json')
    release=analysis.read_json(DATA/'crosstalk_qualification/repeat-v2/complete.json')
    if release['status']!='complete':
        raise ValueError('Stage-04 release is incomplete')
    published=defaultdict(set)
    for row in release['results']:
        published[row['tape']].add(row['channel'])
    rows={}
    for tape in sorted(published):
        folder=ORIGINALS/tape
        path,report=check(folder,ROOT)
        state=report['action']
        if tape in prior['excluded']:
            state='excluded'
        inv=inventory(folder)
        if state in ('reuse','retrace'):
            resolved=aligned_inventory(inv,ROOT)
            if resolved['alignment']['sha256']!=sha256(path):
                raise ValueError(f'Current coordinates and audit disagree: {tape}')
            if set(published[tape])!={r['channel'] for r in resolved['channels']}:
                raise ValueError(f'Stage-04 release has different source channels: {tape}')
        offsets={k:v['delayed_frames'] for k,v in report['channels'].items() if v['delayed_frames']}
        rows[tape]=dict(action=state,report=str(path),sha256=sha256(path),offset_frames=offsets,
                        unresolved=report['unresolved'] if state=='review' else [],
                        stage04_targets=len(published[tape]),previously_rendered=sum(
                            (DATA/'defluttered_shared'/tape/f'CH{ch:02d}'/'review.json').is_file() for ch in published[tape]))
        log(f"{tape} {state}  measured offset channels={list(offsets)}  held={rows[tape]['unresolved']}")
    return rows


def find_trace(tape, inv):
    for path in sorted(detection.DEFAULT_DETECTION_ROOT.glob('census-*/'+tape+'/inventory.json')):
        run=path.parents[1]
        record=analysis.read_json(path)
        if record['inventory']!=inv or not (path.parent/'evidence.json').exists():
            continue
        config=detection.load_run(run)
        if (config['identity']['implementation']!=detection.implementation() or
                config['settings_sha256']!=detection.census.settings_digest(detection.census.SETTINGS)):
            continue
        detection.load_census(run,tape)
        return run
    return None


def run_stage(name,*args):
    log(f"Running {name} {' '.join(map(str,args))}")
    subprocess.run(command(name,*args),check=True)


def qualify_selected(run, tapes, all_tapes, workers):
    bundle=analysis.assess(run,exclude=sorted(set(all_tapes)-set(tapes)),progress=log)
    folder=bundle['dir']
    if not (folder/'qualification/summary.json').is_file():
        status=followup.run_followup(folder,workers=min(workers,followup.MAX_WORKERS),progress=log)
        if not status['complete']:
            raise ValueError(f'Follow-up remains incomplete: {folder}')
    result=qualification.qualify(folder,progress=log)
    if not result['complete']:
        raise ValueError(f'Qualification remains incomplete: {folder}')
    return folder


def validate_release(folders, destination):
    partitions={}
    inputs={}
    checked=set()
    for path,tapes in folders.items():
        if checked.intersection(tapes):
            raise ValueError('A tape was scheduled twice for validation')
        checked.update(tapes)
        bundle=analysis.open_analysis(path)
        config=bundle['config']['identity']
        if config['policy']!=qualification.rules.POLICY or config['seed']!=analysis.DEFAULT_SEED:
            raise ValueError(f'Different qualification policy or partition seed: {path}')
        summary=qualification.verify_qualification(bundle)
        if not summary['complete'] or set(tapes)-{r['tape'] for r in summary['tapes'] if r['status']=='qualified'}:
            raise ValueError(f'Selected qualification incomplete: {path}')
        inputs[str(path)]=dict(tapes=sorted(tapes),sha256=summary['record_sha256'])
        qualification.collect_validation(bundle,summary,partitions,set(tapes))
    result=dict(kind='aligned_archive_validation',inputs=inputs,criteria=qualification.rules.POLICY['validation'],
                qualification_complete=True,partitions={})
    qualification.score_validation(result,partitions)
    immutable_json(destination,result)
    if not result['passed']:
        raise ValueError(f'Held-out archive validation failed: {destination}')


def tape_directory(root, tape):
    root = root.resolve()
    path = root/tape
    if not tape.startswith('A15_T') or not tape[5:].isdigit() or path.resolve() != path:
        raise ValueError(f'Unexpected tape path: {path}')
    return path


def publish_current_tape(tape, folder, release_path, *, check_existing=False):
    """Promote a whole verified tape by same-volume rename. Preserve the previous edition."""
    current_root = DATA/'defluttered_shared'
    current = tape_directory(current_root, tape)
    staged = tape_directory(folder/'defluttered_shared', tape)
    previous = tape_directory(folder/'previous_defluttered_shared', tape)
    plan_path = folder/'plans'/tape/'plan.json'
    plan = analysis.read_json(plan_path)
    plan_hash = sha256(plan_path)
    release = analysis.read_json(release_path)
    channels = {r['channel']:r for r in release['results'] if r['tape'] == tape}
    sources = {r['channel']:r for r in plan['inventory']['channels']}
    if (release['status'] != 'complete' or plan['inventory']['tape'] != tape or not plan['full_tape']
            or not plan['qualified'] or plan['inventory']['alignment']['action'] != 'retrace'
            or not channels or set(channels) != set(sources)):
        raise ValueError(f'Incomplete aligned plan or stage-04 release for {tape}')

    def verify(path):
        if {p.name for p in path.glob('CH*')} != {f'CH{ch:02d}' for ch in channels}:
            raise ValueError(f'Incomplete channel set at {path}')
        valid_result = import_module('05b_render_archive').valid_result
        for ch, row in channels.items():
            expected = dict(tape=tape, channel=ch, plan_sha256=plan_hash, map_sha256=plan['map']['sha256'],
                            source=sources[ch], source_audio_sha256=row['sha256'], frames=row['frames'],
                            sample_rate=sources[ch]['sample_rate'],
                            crosstalk_removed=True)
            valid_result(path/f'CH{ch:02d}', expected)

    with planning_lock(current_root):
        if current.exists():
            reviews = sorted(current.glob('CH*/review.json'))
            hashes = {analysis.read_json(path)['plan_sha256'] for path in reviews}
            if hashes == {plan_hash}:
                verify(current)
                return True
            if not reviews or hashes != {sha256(OLD_PLANS/tape/'plan.json')} or previous.exists():
                raise ValueError(f'Unrecognized current tape or existing backup: {current}')
        if check_existing:
            return False
        log(f'{tape}: verifying corrected WAVs before publishing to {current_root}')
        verify(staged)
        if current.exists():
            previous.parent.mkdir(parents=True, exist_ok=True)
            rename_for_publication(current, previous)
        try:
            rename_for_publication(staged, current)
        except (OSError, PublicationBlocked):
            if previous.exists() and not current.exists():
                previous.rename(current)
            raise
        log(f'Published corrected tape: {current}')
        return True


def recover(rows, folder, local, genus, finish):
    shifted=[t for t,v in rows.items() if v['action']=='retrace']
    clean=[t for t,v in rows.items() if v['action']=='reuse']
    grouped=defaultdict(list)
    missing=[]
    clocks={}
    for tape in shifted:
        clocks[tape]=aligned_inventory(inventory(ORIGINALS/tape),ROOT)
        trace=find_trace(tape,clocks[tape])
        if trace:
            log(f'Reusing verified aligned trace: {tape} {trace.name}')
            grouped[trace].append(tape)
        else:
            missing.append(tape)
    if missing:
        run_stage('05_detect_flutter.py','run',*[arg for tape in missing for arg in ('--tape',tape)],
                  '--input-mode','ssd','--local-workers',local,'--genus-workers',genus,'--genus-cpus','none')
        for tape in missing:
            trace=find_trace(tape,clocks[tape])
            if not trace:
                raise ValueError(f'No verified aligned trace for {tape}')
            grouped[trace].append(tape)
    sources={OLD_ANALYSIS:clean} if clean else {}
    for run,tapes in grouped.items():
        path=qualify_selected(run,tapes,rows,max(1,local))
        sources[path]=tapes
        for tape in tapes:
            plans = folder/'plans'
            plans.mkdir(exist_ok=True)
            output = plans/tape
            options = ('--input', ORIGINALS/tape, '--qualification-root', path)
            if output.exists():
                run_stage('05a_plan_deflutter.py', *options, '--output', output)
            else:
                # Publish only after the entire plan is committed; an interrupted
                # compilation can be resumed without editing an immutable plan.
                with tempfile.TemporaryDirectory(dir=plans) as staging:
                    staged = Path(staging)/tape
                    run_stage('05a_plan_deflutter.py', *options, '--output', staged)
                    staged.rename(output)
    validate_release(sources,folder/'validation.json')
    deferred = {}
    for tape in shifted:
        name=folder.name+'-'+tape
        log(f'{tape}: reusing stage-03 audio and target-only scans; checking all donor matches')
        release=recover_stage04(tape,clocks[tape],name,min(local,6),min(genus,6))
        if publish_current_tape(tape, folder, release, check_existing=True):
            log(f'{tape}: verified corrected audio already in defluttered_shared')
            continue
        run_stage('05b_render_archive.py','run','--tape',tape,'--planning-root',folder/'plans',
                  '--release',release,'--output-root',folder/'defluttered_shared',
                  '--local-workers',local,'--genus-workers',genus)
        if finish:
            try:
                publish_current_tape(tape, folder, release)
            except PublicationBlocked as error:
                deferred[tape] = release
                log(f'{tape}: publication deferred. {error} Continuing with the other tapes.')
    if finish and clean:
        run_stage('05b_render_archive.py','run',*[v for tape in clean for v in ('--tape',tape)],
                  '--local-workers',local,'--genus-workers',genus)
    pending = []
    for tape, release in deferred.items():
        try:
            publish_current_tape(tape, folder, release)
        except PublicationBlocked as error:
            pending.append(tape)
            log(str(error))
    if pending:
        raise SystemExit(f'Publication pending for {", ".join(pending)}. Corrected WAVs are staged. '
                         'Release the directory locks and repeat the same command to publish them.')


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('action',choices=('plan','run'))
    p.add_argument('--finish-archive',action='store_true',
                   help='Publish corrected tapes in defluttered_shared and finish outstanding channels on unchanged tapes')
    p.add_argument('--local-workers',type=int,default=8)
    p.add_argument('--genus-workers',type=int,default=8)
    args=p.parse_args()
    if not (0<=args.local_workers<=16 and 0<=args.genus_workers<=16 and args.local_workers+args.genus_workers):
        p.error('Select at least one active worker and at most 16 per host')
    rows=inspect_archive()
    # Bind scientific inputs and processing code; a changed run cannot claim old audio.
    from _bootstrap import source_path
    names=('recover_capture_alignment.py','repeat_realign.py','repeat_archive.py','repeat_scan.py','repeat_cluster.py',
           'repeat_validate.py','repeat_periodicity.py','repeat_render.py','crosstalk_model.py',
           'repeat_qualification.py','repeat_catalog.py','crosstalk_stage.py','shared_render.py','shared_tape.py',
           'flutter_qualification.py','flutter_analysis.py','flutter_followup.py','flutter_detection.py',
           'flutter_census.py','flutter.py','tape_coordinates.py','05b_render_archive.py')
    key=dict(alignment={t:r['sha256'] for t,r in rows.items()},code={n:sha256(source_path(n)) for n in names},
             prior_stage04=sha256(DATA/'crosstalk_qualification/repeat-v2/complete.json'),qualified=str(PRIOR))
    key['code']['recover_capture_alignment.py'] = release_coordinator_hash(key['code']['recover_capture_alignment.py'])
    key['code']['05b_render_archive.py'] = archive_renderer_hash(key['code']['05b_render_archive.py'])
    folder=REPORTS/('aligned-'+detection.digest(key)[:16])
    immutable_json(folder/'identity.json',key)
    held={t:r['unresolved'] for t,r in rows.items() if r['action']=='review'}
    atomic_json(folder/'scope.json',dict(tapes=rows,counts=dict(Counter(r['action'] for r in rows.values())),held=held))
    changed = [r for r in rows.values() if r['action']=='retrace']
    log(f"{len(changed)} tapes with capture offsets, {sum(len(r['offset_frames']) for r in changed)} offset tracks, "
        f"{sum(r['stage04_targets'] for r in changed)} outputs on changed maps; "
        f"{sum(r['action']=='reuse' for r in rows.values())} tapes can reuse their maps.")
    log(f"Scope: {folder/'scope.json'}")
    if args.action=='run':
        with planning_lock(folder/'run'):
            recover(rows,folder,args.local_workers,args.genus_workers,args.finish_archive)
    if held:
        log(f'Unresolved tape clocks held: {held}')
        return 2
    return 0


if __name__=='__main__':
    raise SystemExit(main())
