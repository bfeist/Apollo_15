"""File handling for review audio and explicitly selected whole-channel denoising."""
import argparse
import json
from pathlib import Path

import numpy as np
import soundfile as sf

from audio_processing import clean_tones, denoise, DEFAULT_BROADBAND_CAP_DB
from flutter_plan_store import DEFAULT_DENOISED_ROOT


def main(stage):
    if stage != 'denoise':
        raise ValueError('Only stage 03 denoising is supported')
    description = 'Stage 03: tone/hum removal and 3 dB-capped broadband denoising'
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument('--input', type=Path, required=True,
                        help='Speech excerpt folder, or canonical tape folder with --channel')
    parser.add_argument('--output', type=Path, required=False)
    if stage == 'denoise':
        parser.add_argument('--channel', type=int, choices=range(1, 31), metavar='1-30',
                            help='Denoise one whole channel independently of deflutter planning')
        parser.add_argument('--denoised-root', type=Path, default=DEFAULT_DENOISED_ROOT,
                            help='Whole-channel cache root when --output is omitted')
        parser.add_argument('--cleanup-block-seconds', type=float, default=60)
        parser.add_argument('--workers', type=int, default=0,
                            help='Whole-channel cleanup workers; 0 selects up to 16, 1 is serial')
    args = parser.parse_args()
    if stage == 'denoise' and args.workers < 0:
        parser.error('--workers must be nonnegative')
    if stage == 'denoise' and args.channel is not None:
        from channel_review import ensure_denoised, denoised_directory
        from flutter_planning import inventory

        try:
            inv = inventory(args.input)
            row = next((r for r in inv['channels'] if r['channel'] == args.channel), None)
            if row is None:
                raise ValueError('Requested channel is unavailable')
            output = args.output or denoised_directory(args.denoised_root, inv['tape'], args.channel)
            ensure_denoised(row, output, 0, row['frames'], args.cleanup_block_seconds,
                            lambda s: print(s, flush=True), workers=args.workers)
        except (ValueError, KeyError, TypeError, OSError, RuntimeError) as exc:
            parser.error(str(exc))
        return
    if args.output is None:
        parser.error('Excerpt processing requires --output')
    source, destination = args.input.resolve(), args.output.resolve()
    if not source.is_dir() or source == destination or source in destination.parents or destination in source.parents:
        parser.error('Input and output must be separate, non-overlapping folders')
    files = sorted(source.rglob('*.wav'))
    if not files:
        parser.error('No WAV files found')
    # Preflight the entire batch before writing any audio. The accepted DSP is
    # excerpt-based; arbitrary chunking would change its filters and profiles.
    for path in files:
        info = sf.info(path)
        if 'IRIG' in str(path.relative_to(source)).upper():
            parser.error(f'Timing/reference audio must be kept out of speech cleanup: {path}')
        if info.channels != 1 or not 15 <= info.duration <= 120 or not 1000 <= info.samplerate <= 48000:
            parser.error(f'Only mono review excerpts of 15–120 seconds at 1–48 kHz are supported: {path}')
    for index, path in enumerate(files, 1):
        print(f'[{index}/{len(files)}] {stage}: {path.name}', flush=True)
        x, sr = sf.read(path, dtype='float64')
        if not np.all(np.isfinite(x)):
            raise ValueError(f'Non-finite input samples: {path}')
        metadata = dict(stage=stage, source=str(path), sample_rate=sr, frames=len(x))
        if stage == 'denoise':
            y, cleanup = clean_tones(x, sr)
            for p in cleanup['passes']:
                print(f'  Tone pass {p["iteration"]}/6: removed {len(p["lines"])} persistent tones', flush=True)
            for p in cleanup['hum_passes']:
                print(f'  Residual hum pass {p["iteration"]}/3: removed {len(p["lines"])} harmonics', flush=True)
            metadata.update(cleanup)
            y, profile = denoise(y, sr)
            metadata.update(noise_profile_seconds=profile, noise_profile_approved=False,
                            reduction_cap_db=DEFAULT_BROADBAND_CAP_DB,
                            noise_origin_classified=False)
        if len(y) != len(x) or not np.all(np.isfinite(y)):
            raise ValueError('Invalid output length or non-finite samples')
        target = destination / path.relative_to(source)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix('.partial')
        try:
            sf.write(temporary, y, sr, subtype='FLOAT', format='WAV')
            written, rate = sf.read(temporary, dtype='float64')
            if rate != sr or len(written) != len(x) or not np.all(np.isfinite(written)):
                raise ValueError('Written WAV verification failed')
            metadata['output_peak'] = float(np.max(np.abs(written)))
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
        residual = destination.parent / (destination.name + '_removed') / path.relative_to(source)
        residual.parent.mkdir(parents=True, exist_ok=True)
        sf.write(residual, x - y, sr, subtype='FLOAT')
        target.with_suffix('.json').write_text(json.dumps(metadata, indent=2), encoding='utf-8')
        print(f'  Saved {target}; verified peak {metadata["output_peak"]:.6f}', flush=True)

