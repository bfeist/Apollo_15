"""Render a local, cross-tape listening review of stages 02-03.

Original fragments are read only. No production settings or approvals change.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import xml.etree.ElementTree as ET

import numpy as np
import soundfile as sf
from scipy import signal

from audio_processing import clean_tones, denoise, DEFAULT_BROADBAND_CAP_DB


TONAL = [('off', 0.0), ('half', 0.5), ('full', 1.0)]
CAPS = [0, 3, 6]


def read_excerpt(path, start, duration):
    with sf.SoundFile(path) as f:
        if f.channels != 1 or not 1000 <= f.samplerate <= 48000:
            raise ValueError(f'Unsupported speech source: {path}')
        sr = f.samplerate
        f.seek(round(start * sr))
        x = f.read(round(duration * sr), dtype='float64')
    if len(x) != round(duration * sr) or not np.isfinite(x).all():
        raise ValueError(f'Incomplete or invalid excerpt: {path} at {start}')
    return x, sr


def word_count(text):
    text = re.sub(r'\[[^]]*\]|<[^>]*>', '', text)
    return len(re.findall(r"[A-Za-z]+(?:'[A-Za-z]+)?", text))


def load_annotation(path):
    if path.suffix.lower() == '.trs':
        tree = ET.parse(path)
        return [dict(start_time=float(turn.attrib['startTime']),
                     end_time=float(turn.attrib['endTime']), words=' '.join(turn.itertext()))
                for turn in tree.iter('Turn') if turn.attrib.get('speaker')]
    rows = json.loads(path.read_text(encoding='utf-8-sig'))
    return list(rows.values()) if isinstance(rows, dict) else rows


def annotated_candidates(tape, annotations, duration):
    """Use supplied ASR only to find likely conversations, never as ground truth."""
    candidates = []
    files = {p.stem: p for p in tape.rglob('*.wav') if 'IRIG' not in p.name.upper()}
    # Read a bounded, size-ranked ASR shortlist per channel. Directory entry
    # sizes are cached by Windows; opening every original WAV would be costly.
    entries = []
    if annotations.is_dir():
        with os.scandir(annotations) as scan:
            entries = [(entry.stat().st_size, Path(entry.path)) for entry in scan
                       if entry.name.startswith(tape.name+'_') and Path(entry.name).suffix.lower() in {'.json', '.trs'}
                       and Path(entry.name).stem in files]
    per_channel = {}
    shortlist = []
    for _, annotation in sorted(entries, reverse=True):
        channel = re.search(r'_CH(\d+)_', annotation.name).group(1)
        if per_channel.get(channel, 0) < 2:
            shortlist.append(annotation)
            per_channel[channel] = per_channel.get(channel, 0)+1
    for annotation in sorted(shortlist):
        path = files.get(annotation.stem)
        if path is None:
            continue
        rows = load_annotation(annotation)
        rows = [r for r in rows if word_count(r.get('words', '')) >= 2
                and 0.25 <= float(r['end_time']) - float(r['start_time']) <= 60]
        if not rows:
            continue
        stamps = re.findall(r'(\d{3})-(\d{2})-(\d{2})-(\d{2})', path.name)
        if len(stamps) == 2:
            seconds = [sum(int(v)*scale for v, scale in zip(stamp, [86400, 3600, 60, 1])) for stamp in stamps]
            source_duration = seconds[1]-seconds[0]
        else:
            source_duration = sf.info(path).duration
        if source_duration < duration:
            continue
        # Fixed, overlapping windows avoid tuning excerpts to cleanup outcomes.
        bins = {}
        for row in rows:
            start, end = float(row['start_time']), float(row['end_time'])
            starts = {min(k*duration/2, source_duration-duration)
                      for k in range(max(0, int((start-duration)/(duration/2))), int(end/(duration/2))+1)}
            for left in sorted(starts):
                overlap = max(0, min(end, left+duration)-max(start, left))
                if overlap:
                    bins.setdefault(left, []).append((row, overlap))
        for left, matches in bins.items():
            coverage = min(duration, sum(overlap for _, overlap in matches))
            words = sum(word_count(r['words'])*overlap/(float(r['end_time'])-float(r['start_time']))
                        for r, overlap in matches)
            if coverage >= 6 and words >= 8:
                candidates.append(dict(source=str(path), offset_seconds=left,
                    annotation=str(annotation), annotated_speech_seconds=coverage,
                    annotated_words=round(words), transcript=' '.join(r['words'] for r, _ in matches),
                    selection='Supplied ASR conversation candidate; human speech verification pending',
                    rank=coverage+min(words, 60)/10))
    candidates.sort(key=lambda c: c['rank'], reverse=True)
    diverse = []
    seen = set()
    for c in candidates:
        # One candidate per fragment, at most two per channel in the shortlist.
        channel = Path(c['source']).parent.name
        if c['source'] in seen or sum(Path(d['source']).parent.name == channel for d in diverse) >= 2:
            continue
        diverse.append(c)
        seen.add(c['source'])
        if len(diverse) == 12:
            break
    return diverse


def measure_candidate(candidate, duration):
    x, sr = read_excerpt(candidate['source'], candidate['offset_seconds'], duration)
    band = signal.sosfilt(signal.butter(4, [300, min(3000, sr*.45)], fs=sr,
                                       btype='bandpass', output='sos'), x)
    energy = np.mean(band[:len(band)//sr*sr].reshape(-1, sr)**2, axis=1)
    candidate.update(speech_band_rms_dbfs=float(10*np.log10(np.mean(energy)+1e-30)),
                     activity_ratio=float(np.percentile(energy, 90)/(np.percentile(energy, 20)+1e-20)))
    return candidate


def fallback_candidates(tape, duration):
    """Sparse voice-role sampling for tapes without supplied ASR."""
    folders = [p for p in sorted(tape.iterdir()) if p.is_dir() and
               any(role in p.name for role in ['CAPCOM', 'FD_LT', 'FLT_DO', '3_FD_L', '3_GOSS_2', 'VOICE'])]
    candidates = []
    for folder in folders[:4]:
        files = sorted(folder.glob('*.wav'))
        for index in sorted(set([0, len(files)//2, len(files)-1])) if files else []:
            path = files[index]
            info = sf.info(path)
            if info.channels != 1 or info.duration < duration:
                continue
            for fraction in [.2, .5, .8]:
                start = round((info.duration-duration)*fraction, 3)
                c = dict(source=str(path), offset_seconds=start, annotation=None,
                         transcript='', annotated_speech_seconds=None, annotated_words=None,
                         selection='Voice-role channel / band-energy variation; human speech verification pending')
                measure_candidate(c, duration)
                if c['speech_band_rms_dbfs'] > -90:
                    candidates.append(c)
    return sorted(candidates, key=lambda c: c['activity_ratio'], reverse=True)


def tape_candidates(tape, annotations, duration):
    candidates = annotated_candidates(tape, annotations, duration)
    if candidates:
        for c in candidates:
            measure_candidate(c, duration)
    else:
        candidates = fallback_candidates(tape, duration)
    return candidates


def select_samples(delivery, annotations, duration, extra_quiet, tapes):
    selected, weak_pool, inventory = [], [], []
    # These read-only selections are independent. Bounded workers overlap slow
    # source seeks while the render itself remains sequential and reproducible.
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {tape: pool.submit(tape_candidates, tape, annotations, duration) for tape in tapes}
        for tape in tapes:
            candidates = futures[tape].result()
            if not candidates:
                inventory.append(dict(tape=tape.name, status='no eligible voice candidate'))
                print(f'{tape.name}: no eligible voice candidate', flush=True)
                continue
            primary = candidates[0]
            if primary['annotation']:
                alternatives = [c for c in candidates[1:] if c['source'] != primary['source']]
                if alternatives:
                    quiet = min(alternatives, key=lambda c: c['speech_band_rms_dbfs'])
                    weak_pool.append((tape.name, quiet))
            primary = dict(primary, tape=tape.name, category='conversation candidate')
            selected.append(primary)
            inventory.append(dict(tape=tape.name, status='sample selected', shortlist=candidates))
            print(f"Selected {tape.name}: {Path(primary['source']).parent.name}, "
                  f"{primary['offset_seconds']:.1f}s, ASR={bool(primary['annotation'])}", flush=True)
    if weak_pool and extra_quiet:
        indices = np.linspace(0, len(weak_pool)-1, min(extra_quiet, len(weak_pool)), dtype=int)
        for i in indices:
            tape, quiet = weak_pool[i]
            selected.append(dict(quiet, tape=tape, category='quieter annotated conversation candidate'))
    return sorted(selected, key=lambda c: (c['tape'], c['category'])), inventory


def render_sample(candidate, out, duration, index):
    x, sr = read_excerpt(candidate['source'], candidate['offset_seconds'], duration)
    before = Path(candidate['source']).stat()
    source_hash = hashlib.sha256(x.tobytes()).hexdigest()
    y, cleanup = clean_tones(x, sr)
    variants, metadata = {}, {}
    for name, mix in TONAL:
        tonal = y if mix == 1 else (x if mix == 0 else x + mix*(y-x))
        for cap in CAPS:
            key = f'{name}_b{cap}'
            clean, profile = denoise(tonal, sr, reduction_cap_db=cap) if cap else (tonal, None)
            variants[key] = clean
            metadata[key] = dict(tonal_wet_fraction=mix, broadband_cap_db=cap,
                noise_profile_excerpt_seconds=profile,
                noise_profile_source_seconds=[candidate['offset_seconds']+t for t in profile] if profile else None,
                noise_profile_approved=False)
    residuals = {k: x-v for k, v in variants.items() if k != 'off_b0'}
    peak = max(float(np.max(np.abs(v))) for v in [*variants.values(), *residuals.values()])
    gain = min(10**(30/20), 10**(-3/20)/peak) if peak else 1.
    channel = re.search(r'_CH(\d+)_', Path(candidate['source']).name).group(1)
    sample_id = f'{index:02d}_{candidate["tape"]}_CH{channel}'
    folder = out / sample_id
    folder.mkdir()
    files, errors = {}, []
    for key, clean in variants.items():
        files[key] = dict(audio=f'{sample_id}/{key}.wav', removed=None)
        sf.write(out/files[key]['audio'], clean*gain, sr, subtype='FLOAT')
        written, rate = sf.read(out/files[key]['audio'])
        if rate != sr or len(written) != len(x) or not np.isfinite(written).all() or abs(written).max() > 10**(-3/20)+1e-6:
            raise ValueError('Audio export verification failed')
        if key in residuals:
            files[key]['removed'] = f'{sample_id}/{key}_removed.wav'
            sf.write(out/files[key]['removed'], residuals[key]*gain, sr, subtype='FLOAT')
            removed, rate = sf.read(out/files[key]['removed'])
            error = float(np.max(abs(written+removed-x*gain)))
            errors.append(error)
            if rate != sr or len(removed) != len(x) or not np.isfinite(removed).all() or error > 2e-7:
                raise ValueError('Written residual reconstruction failed')
    # Verify the cell matching the defaults in force when this review is rendered.
    current, _ = denoise(y, sr)
    current_variant = f'full_b{DEFAULT_BROADBAND_CAP_DB}'
    if not np.array_equal(current, variants[current_variant]):
        raise ValueError('Current-pipeline baseline changed')
    check, _ = read_excerpt(candidate['source'], candidate['offset_seconds'], duration)
    after = Path(candidate['source']).stat()
    if before.st_size != after.st_size or before.st_mtime_ns != after.st_mtime_ns or hashlib.sha256(check.tobytes()).hexdigest() != source_hash:
        raise ValueError('Source changed during review rendering')
    result = dict(candidate, id=sample_id, sample_rate=sr, frames=len(x), duration_seconds=duration,
        source_excerpt_sha256_float64=source_hash, source_size=before.st_size,
        source_mtime_ns=before.st_mtime_ns, shared_gain_db=float(20*np.log10(gain)),
        stage02_analysis_only=True, stage03_passes=cleanup['passes'],
        stage03_hum_passes=cleanup['hum_passes'],
        stage03_residual_lines=cleanup['residual_lines'],
        stage03_residual_hum_lines=cleanup['residual_hum_lines'],
        stage03_method=cleanup['tonal_cleanup_method'],
        variants=metadata, files=files, max_written_reconstruction_error=max(errors),
        current_pipeline_exact=True, current_variant=current_variant, human_verified=False)
    (folder/'sample.json').write_text(json.dumps(result, indent=2, allow_nan=False), encoding='utf-8')
    return result


def write_page(out, manifest):
    template = Path(__file__).with_name('noise_review.html').read_text(encoding='utf-8')
    payload = json.dumps(manifest, ensure_ascii=True, allow_nan=False).replace('<', '\\u003c')
    (out/'index.html').write_text(template.replace('/* REVIEW_DATA */', 'const REVIEW = '+payload+';'), encoding='utf-8')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--delivery', type=Path, default=Path('D:/Apollo_15_30-Track/orig_delivery'))
    parser.add_argument('--annotations', type=Path, default=Path('D:/Apollo_15_30-Track/orig_delivery/Apollo15_trs'),
                        help='Folder of supplied TRS or JSON speech annotations')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--duration', type=int, default=30, choices=range(20, 61))
    parser.add_argument('--extra-quiet', type=int, default=8)
    parser.add_argument('--tape', action='append')
    args = parser.parse_args()
    if args.extra_quiet < 0:
        parser.error('--extra-quiet must be nonnegative')
    delivery, out = args.delivery.resolve(), args.output.resolve()
    if out.exists() or delivery == out or delivery in out.parents or out in delivery.parents:
        parser.error('--output must be a new folder outside the delivery tree')
    tapes = sorted(p for p in delivery.glob('A15_T*') if p.is_dir())
    if args.tape:
        tapes = [p for p in tapes if p.name in args.tape]
        if {p.name for p in tapes} != set(args.tape):
            parser.error('Requested tape not found')
    if not tapes:
        parser.error('No tape folders found')
    out.mkdir(parents=True)
    selected, inventory = select_samples(delivery, args.annotations, args.duration, args.extra_quiet, tapes)
    manifest = dict(run_id=out.name, created=datetime.now().astimezone().isoformat(),
        duration_seconds=args.duration, delivery=str(delivery), requested_tapes=[p.name for p in tapes],
        current_variant=f'full_b{DEFAULT_BROADBAND_CAP_DB}',
        selection_inventory='selection.json', status='rendering', samples=[],
        notes='ASR and energy-based selection are unverified. No intelligibility or SNR claim. No flutter or per-version normalization.')
    (out/'selection.json').write_text(json.dumps(dict(selected=selected, inventory=inventory), indent=2, allow_nan=False), encoding='utf-8')
    for i, candidate in enumerate(selected, 1):
        print(f'[{i}/{len(selected)}] Render {candidate["tape"]} {candidate["category"]}', flush=True)
        manifest['samples'].append(render_sample(candidate, out, args.duration, i))
        (out/'results.json').write_text(json.dumps(manifest, indent=2, allow_nan=False), encoding='utf-8')
        write_page(out, manifest)
    manifest['status'] = 'ready for human review'
    manifest['verification'] = dict(samples=len(selected), tapes=len({c['tape'] for c in selected}),
        wavs=len(list(out.rglob('*.wav'))), current_pipeline_exact=all(s['current_pipeline_exact'] for s in manifest['samples']),
        max_written_reconstruction_error=max((s['max_written_reconstruction_error'] for s in manifest['samples']), default=0))
    (out/'results.json').write_text(json.dumps(manifest, indent=2, allow_nan=False), encoding='utf-8')
    write_page(out, manifest)
    print(json.dumps(manifest['verification']), flush=True)
    print(f'Open {out / "index.html"}', flush=True)


if __name__ == '__main__':
    main()
