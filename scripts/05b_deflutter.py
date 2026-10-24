"""Stage 05b: deflutter denoised audio, then apply bounded constant peak normalization."""
import argparse
import hashlib
import json
from pathlib import Path
import numpy as np
import soundfile as sf
from normalization import DEFAULT_TARGET_DBFS, DEFAULT_MAX_BOOST_DB, normalize_array, verify_peak, validate_settings
from flutter import estimate, positions, read_excerpt, resample_at
from flutter_plan_store import DEFAULT_PLANNING_ROOT, DEFAULT_DEFLUTTERED_ROOT, DEFAULT_DENOISED_ROOT, load_saved_plan, tape_directory


def process_saved_region(source, destination, plan_path, targets_path, tape=None, target_dbfs=DEFAULT_TARGET_DBFS, max_boost_db=DEFAULT_MAX_BOOST_DB):
    """Render aligned review excerpts using frozen maps, with no estimation.

One region per invocation keeps this within stage 05's existing excerpt boundary.
Continuous full-tape map joins remain a separate renderer.
"""
    validate_settings(target_dbfs, max_boost_db)
    source, destination = Path(source).resolve(), Path(destination).resolve()
    plan_path, targets_path = Path(plan_path).resolve(), Path(targets_path).resolve()
    if plan_path.is_dir():
        plan_path /= 'plan.json'
    if (not source.is_dir() or source == destination or source in destination.parents
            or destination in source.parents):
        raise ValueError('Input/output must be separate non-overlapping folders')
    if destination == plan_path.parent or plan_path.parent in destination.parents or destination in plan_path.parents:
        raise ValueError('Rendered audio must be outside the saved plan directory')
    if destination.exists() and any(destination.iterdir()):
        raise ValueError('Output folder must be empty')
    plan = load_saved_plan(plan_path)
    config = json.loads(targets_path.read_text(encoding='utf-8'))
    if config['tape'] != plan['inventory']['tape'] or (tape is not None and tape != config['tape']):
        raise ValueError('Target manifest and saved plan must identify the same tape')
    region = next((r for r in plan['regions'] if r['id'] == config['region_id']), None)
    if region is None or region['status'] != 'analyzed':
        raise ValueError('Requested region has not been planned')
    start, duration = float(config['start_seconds']), float(config['duration_seconds'])
    if not np.isfinite([start, duration]).all() or not 15 <= duration <= 120:
        raise ValueError('Saved-plan rendering supports aligned excerpts of 15-120 seconds')
    if start < region['start_seconds'] or start+duration > region['end_seconds']+1e-8:
        raise ValueError('Excerpt must fit within one planned region; use render_channel_review.py for continuous tape joins')
    originals = {r['channel']: r for r in plan['inventory']['channels']}
    decisions = {r['channel']: r for r in region['targets']}
    curves = {}
    if region.get('maps'):
        with np.load(plan_path.parent/region['maps']['file']) as archive:
            curves = {key: archive[key].copy() for key in archive.files}
    files, seen = [], set()
    for target in config['targets']:
        relative = Path(target['file'])
        path = (source/relative).resolve()
        channel = target['channel']
        if relative.is_absolute() or source not in path.parents or path in seen or path.suffix.lower() != '.wav':
            raise ValueError('Targets must be unique WAV files inside input')
        if channel not in decisions or channel not in originals or 'IRIG' in str(relative).upper():
            raise ValueError('Target channel is not a planned speech target')
        if float(target['start_seconds']) != start:
            raise ValueError('Every target must declare the same source start as the excerpt')
        original = Path(originals[channel]['path']).resolve()
        if path == original or destination == original.parent or original.parent in destination.parents:
            raise ValueError('Keep cleaned target input and rendered outputs separate from original channels')
        decision = decisions[channel]
        if decision['action'] not in ('render_and_review', 'leave_uncorrected'):
            raise ValueError(f'CH{channel} still requires transfer validation in {region["id"]}; its saved map is not validated for this target')
        info = sf.info(path)
        sr = info.samplerate
        if info.channels != 1 or sr != originals[channel]['sample_rate'] or info.frames != round(duration*sr):
            raise ValueError('Target headers must match the declared source channel and excerpt duration')
        audio, _ = sf.read(path, dtype='float64')
        if not np.isfinite(audio).all():
            raise ValueError('Non-finite target audio')
        tt = np.arange(round(duration*200))/200
        e = np.zeros(len(tt))
        if decision['action'] == 'render_and_review':
            key = decision['selected']
            full = curves.get(key)
            if (full is None or full.ndim != 1 or len(full) != round((region['analysis_end']-region['analysis_start'])*200)
                    or not np.isfinite(full).all() or np.max(abs(full)) > .05):
                raise ValueError('Invalid saved correction map')
            e = np.interp(start+tt, region['analysis_start']+np.arange(len(full))/200, full)
            # Local export fade and anchoring match the existing listening workflow.
            e *= np.clip(np.minimum(tt, tt[-1]-tt)/.1, 0, 1)
        pos = positions(e, sr, info.frames)
        files.append((relative, path, channel, decision, sr, e, pos))
        seen.add(path)
    all_wavs = {p.resolve() for p in source.rglob('*') if p.suffix.lower() == '.wav'}
    if not files or seen != all_wavs:
        raise ValueError('Target manifest must declare exactly all WAV files in the input folder')
    destination.mkdir(parents=True, exist_ok=True)
    provenance = dict(method='saved-regional-map-v1', tape=config['tape'], region_id=region['id'],
        saved_plan=str(plan_path), plan_sha256=hashlib.sha256(plan_path.read_bytes()).hexdigest(),
        region_sha256=region['detail_sha256'], maps_sha256=region.get('maps', {}).get('sha256'),
        planning_recomputed=False, config=config, targets=[],
        boundary='One-region excerpt export, 100 ms correction fades; not continuous full-tape rendering')
    for relative, path, channel, decision, sr, e, pos in files:
        x, _ = sf.read(path, dtype='float64')
        y = resample_at(x, pos) if decision['action'] == 'render_and_review' else x
        y, normalization = normalize_array(y, target_dbfs, max_boost_db)
        output = destination/relative
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.with_suffix('.partial')
        try:
            sf.write(temporary, y, sr, format='WAV', subtype='FLOAT')
            check, rate = sf.read(temporary)
            if rate != sr or len(check) != len(x) or not np.isfinite(check).all():
                raise ValueError('Written audio verification failed')
            verify_peak(float(np.max(abs(check))), normalization)
            temporary.replace(output)
        finally:
            temporary.unlink(missing_ok=True)
        map_output = output.with_suffix('.timing_map.npz')
        np.savez_compressed(map_output, source_seconds=start+np.arange(len(e))/200,
                            speed_error=e, input_sample_positions=pos)
        record = dict(source=str(path), output=str(output), channel=channel, action=decision['action'],
            selected=decision['selected'], start_seconds=start, frames=len(x), sample_rate=sr, gain=normalization['gain'], normalization=normalization,
            input_sha256=hashlib.sha256(x.tobytes()).hexdigest(), output_peak=float(np.max(abs(check))),
            timing_map=str(map_output), planning_recomputed=False)
        output.with_suffix('.json').write_text(json.dumps(record, indent=2), encoding='utf-8')
        provenance['targets'].append(record)
        print(f'{relative}: {decision["action"]} from saved {region["id"]}; no reference analysis', flush=True)
    (destination/'deflutter.json').write_text(json.dumps(provenance, indent=2), encoding='utf-8')
    return provenance


def process(source, destination, config_path, target_dbfs=DEFAULT_TARGET_DBFS, max_boost_db=DEFAULT_MAX_BOOST_DB):
    validate_settings(target_dbfs, max_boost_db)
    source=source.resolve();destination=destination.resolve();config_path=config_path.resolve()
    if not source.is_dir() or source==destination or source in destination.parents or destination in source.parents:
        raise ValueError('Input/output must be separate non-overlapping folders')
    if destination.exists() and any(destination.iterdir()):
        raise ValueError('Output folder must be empty')
    config=json.loads(config_path.read_text(encoding='utf-8'))
    if not isinstance(config.get('tape'),str) or not config['tape'].strip():
        raise ValueError('Configuration must identify one tape')
    start=float(config['start_seconds']);duration=float(config['duration_seconds'])
    if not np.isfinite([start,duration]).all() or start<0 or not 15<=duration<=120:
        raise ValueError('Only finite aligned excerpts of 15–120 seconds are supported')
    ref=Path(config['reference']['path'])
    if not ref.is_absolute():ref=config_path.parent/ref
    ref=ref.resolve()
    if ref==destination or destination in ref.parents:
        raise ValueError('Reference cannot be inside output')
    original,sr=read_excerpt(ref,start,duration)
    if sr%200 or not 1000<=sr<=48000:raise ValueError('Reference sample rate must be 1–48 kHz and divisible by 200')
    targets=config['targets']
    if not targets:raise ValueError('No target tracks declared')
    files=[];seen=set()
    for item in targets:
        relative=Path(item['file']);path=(source/relative).resolve()
        if relative.is_absolute() or source not in path.parents or path in seen:
            raise ValueError('Targets must be unique files inside input')
        if path==ref or 'IRIG' in str(relative).upper():raise ValueError('Reference/timing channels cannot be speech targets')
        if float(item['start_seconds'])!=start:raise ValueError('Target and reference must have the same source start; offsets are not implemented')
        info=sf.info(path)
        if info.channels!=1 or info.samplerate!=sr or info.frames!=len(original):
            raise ValueError(f'Target must match reference excerpt rate and frame count: {relative}')
        audio,_=sf.read(path,dtype='float64')
        if not np.isfinite(audio).all():raise ValueError(f'Non-finite target: {relative}')
        seen.add(path);files.append((relative,path))
    if seen!={p.resolve() for p in source.rglob('*.wav')}:
        raise ValueError('Configuration must declare exactly all WAV files in the input folder')
    tt,error,stats,frequency=estimate(original,sr,
        carrier_hz=float(config['reference'].get('carrier_hz',631.5)),
        hum_hz=tuple(config['reference'].get('hum_hz',[540,600,660,720])))
    sample_positions=positions(error,sr,len(original))
    # Hash the actual reference excerpt and freeze configuration with the map.
    provenance=dict(method='accepted-reference-tone-v1',accepted_listening_date='2026-10-16',
        config=config,config_path=str(config_path),reference_path=str(ref),
        reference_excerpt_sha256=hashlib.sha256(original.tobytes()).hexdigest(),
        sample_rate=sr,frames=len(original),tracking=stats,
        alignment='Same start/rate/length explicitly declared; no head-offset estimation',targets=[])
    destination.mkdir(parents=True,exist_ok=True)
    np.savez_compressed(destination/'timing_map.npz',source_seconds=tt+start,
        speed_error=error,input_sample_positions=sample_positions,raw_frequency_hz=frequency)
    for relative,path in files:
        x,_=sf.read(path,dtype='float64');y=resample_at(x,sample_positions)
        if not np.isfinite(y).all() or len(y)!=len(x):raise ValueError('Invalid corrected audio')
        y, normalization = normalize_array(y, target_dbfs, max_boost_db)
        output=destination/relative;output.parent.mkdir(parents=True,exist_ok=True)
        temporary=output.with_suffix('.partial')
        try:
            sf.write(temporary,y,sr,format='WAV',subtype='FLOAT')
            check,rate=sf.read(temporary)
            if rate!=sr or len(check)!=len(x) or not np.isfinite(check).all():raise ValueError('Written audio verification failed')
            verify_peak(float(np.max(abs(check))), normalization)
            temporary.replace(output)
        finally:temporary.unlink(missing_ok=True)
        record=dict(source=str(path),output=str(output),stage='deflutter',method=provenance['method'],
            tape=config['tape'],start_seconds=start,sample_rate=sr,frames=len(y),
            input_sha256=hashlib.sha256(x.tobytes()).hexdigest(),
            output_peak=float(np.max(abs(check))),timing_map=str(destination/'timing_map.npz'),gain=normalization['gain'],normalization=normalization)
        output.with_suffix('.json').write_text(json.dumps(record,indent=2),encoding='utf-8')
        provenance['targets'].append(record)
        print(f'Corrected {relative} with shared reference map; {len(y)} frames verified',flush=True)
    (destination/'deflutter.json').write_text(json.dumps(provenance,indent=2),encoding='utf-8')
    return provenance


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input',type=Path,help='Cleaned excerpt folder; omit in whole-channel mode')
    parser.add_argument('--output',type=Path)
    parser.add_argument('--channel',type=int,help='Render a whole channel using its stage-03 intermediate')
    parser.add_argument('--denoised-input',type=Path,help='Prepared stage-03 folder containing denoised.wav and manifest')
    parser.add_argument('--experimental-transfer',action='store_true')
    parser.add_argument('--production-approval', type=Path, help='Accepted plan/channel and continuous listening evidence')
    parser.add_argument('--flutter-config',type=Path,help='One-tape JSON reference and target alignment declaration')
    parser.add_argument('--flutter-plan', type=Path, help='Saved regional plan; defaults to the persistent tape folder')
    parser.add_argument('--flutter-targets', type=Path, help='Region, target channels and excerpt alignment JSON')
    parser.add_argument('--tape', help='Tape name for the persistent saved plan')
    parser.add_argument('--planning-root', type=Path, default=DEFAULT_PLANNING_ROOT)
    parser.add_argument('--target-dbfs',type=float,default=DEFAULT_TARGET_DBFS,help='Final sample-peak target in dBFS (default: -3)')
    parser.add_argument('--max-boost-db',type=float,default=DEFAULT_MAX_BOOST_DB,help='Maximum constant gain boost in dB (default: 12)')
    args=parser.parse_args()
    if args.flutter_config and (args.flutter_plan or args.flutter_targets):
        parser.error('Choose legacy --flutter-config or saved-plan rendering, not both')
    try:
        if args.channel is not None:
            if not args.tape or args.input or args.flutter_config or args.flutter_targets:
                parser.error('Whole-channel mode requires --tape and uses the prepared denoised intermediate')
            from channel_review import render_channel_review, denoised_directory
            plan=args.flutter_plan or tape_directory(args.tape,args.planning_root)/'plan.json'
            index=plan/'plan.json' if plan.is_dir() else plan
            from flutter_policy import require_production
            require_production(args.production_approval, index, args.channel, args.experimental_transfer)
            inventory=json.loads(index.read_text(encoding='utf-8'))['inventory']
            if inventory['tape'] != args.tape:
                raise ValueError('Requested tape and saved plan disagree')
            row=next((r for r in inventory['channels'] if r['channel']==args.channel),None)
            if row is None:
                raise ValueError('Requested channel is unavailable in the saved plan')
            output=args.output or tape_directory(args.tape,DEFAULT_DEFLUTTERED_ROOT)/f'CH{args.channel:02d}'
            denoised=args.denoised_input or denoised_directory(DEFAULT_DENOISED_ROOT,args.tape,args.channel)
            render_channel_review(plan,args.channel,output,start=row['start_seconds'],experimental=args.experimental_transfer,
                                  denoised_input=denoised,require_prepared=True,target_dbfs=args.target_dbfs,max_boost_db=args.max_boost_db,progress=lambda s:print(s,flush=True))
            return
        if args.input is None or args.output is None:
            parser.error('Excerpt mode requires --input and --output')
        if args.flutter_config:
            process(args.input,args.output,args.flutter_config,args.target_dbfs,args.max_boost_db)
        else:
            if args.flutter_targets is None:
                parser.error('Saved-plan rendering requires --flutter-targets declaring region/channel/source alignment')
            tape = args.tape or json.loads(args.flutter_targets.read_text(encoding='utf-8'))['tape']
            plan = args.flutter_plan or tape_directory(tape, args.planning_root)/'plan.json'
            process_saved_region(args.input, args.output, plan, args.flutter_targets, tape,args.target_dbfs,args.max_boost_db)
    except (ValueError,KeyError,TypeError,OSError,RuntimeError) as exc:parser.error(str(exc))

if __name__=='__main__':main()
