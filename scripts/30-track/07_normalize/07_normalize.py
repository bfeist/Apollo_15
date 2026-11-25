"""Stage 07: constant gain to a speech target, bypassing uncertain channels."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _bootstrap import install
install()

import argparse
import json
from shared_tape import DEFAULT_DEFLUTTERED_ROOT
from speech_normalization import DEFAULT_NORMALIZED_ROOT, Settings, normalize


def resolve_source(path, require_complete=False):
    path = Path(path)
    if path.is_dir():
        choices = [path/name for name in ('deflutter.wav', 'retimed.wav') if (path/name).is_file()]
        if not choices:
            choices = list(path.glob('*.wav'))
        if len(choices) != 1:
            raise ValueError(f'Select one WAV explicitly: {path}')
        path = choices[0]
    timing = 'irig' in path.name.lower()
    metadata = path.parent/('retime.json' if path.name == 'retimed.wav' else 'review.json')
    if require_complete and not metadata.is_file():
        raise ValueError(f'Missing completed stage manifest: {metadata}')
    if metadata.is_file():
        saved = json.loads(metadata.read_text(encoding='utf-8'))
        if saved.get('status') != 'complete':
            raise ValueError(f'Input stage is incomplete: {metadata}')
        origin = saved.get('source', {})
        timing |= origin.get('role') == 'timing_only' or 'irig' in str(origin.get('path', '')).lower()
    return path, timing


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--input', type=Path, help='One mono WAV or completed channel folder')
    p.add_argument('--output', type=Path, help='New destination for one channel')
    p.add_argument('--input-root', type=Path, default=DEFAULT_DEFLUTTERED_ROOT)
    p.add_argument('--output-root', type=Path, default=DEFAULT_NORMALIZED_ROOT)
    tapes = p.add_mutually_exclusive_group()
    tapes.add_argument('--tape')
    tapes.add_argument('--all-tapes', action='store_true')
    channels = p.add_mutually_exclusive_group()
    channels.add_argument('--channel', type=int)
    channels.add_argument('--all-channels', action='store_true')
    p.add_argument('--exclude-tape', action='append', default=[])
    p.add_argument('--dry-run', action='store_true', help='Measure and print proposed gain; write nothing')
    p.add_argument('--timing-only', action='store_true', help='Explicitly bypass speech gain and peak limiting')
    defaults = Settings()
    for name, value in vars(defaults).items():
        help_text = 'Optional limit; disabled by default' if name in {'max_boost_db', 'max_noise_dbfs'} else None
        p.add_argument('--'+name.replace('_', '-'), type=float, default=value, help=help_text)
    args = p.parse_args(argv)
    try:
        settings = Settings(**{name:getattr(args, name) for name in vars(defaults)})
        settings.validate()
        if args.channel is not None and not 1 <= args.channel <= 30:
            p.error('--channel must be 1-30')
        if any(Path(t).name != t or t in {'.', '..'} for t in [*args.exclude_tape, *([args.tape] if args.tape else [])]):
            p.error('Tape must be a simple folder name')
        if args.input:
            if not args.output or args.tape or args.all_tapes or args.channel or args.all_channels or args.exclude_tape:
                p.error('--input requires --output and no tape/channel selectors')
            jobs = [(args.input, args.output, False)]
        else:
            if not (args.tape or args.all_tapes) or not (args.channel or args.all_channels):
                p.error('Use --input/--output or select tapes and channels')
            if args.output and (args.all_tapes or args.all_channels):
                p.error('Batch output uses --output-root')
            if args.exclude_tape and not args.all_tapes:
                p.error('--exclude-tape requires --all-tapes')
            folders = sorted(args.input_root.glob('A15_T*')) if args.all_tapes else [args.input_root/args.tape]
            jobs = []
            for folder in folders:
                if folder.name in args.exclude_tape:
                    continue
                sources = sorted(folder.glob('CH[0-9][0-9]')) if args.all_channels else [folder/f'CH{args.channel:02d}']
                for source in sources:
                    jobs.append((source, args.output or args.output_root/folder.name/source.name, True))
            if not jobs:
                raise ValueError('No input channels found')
        failures = []
        for source, output, complete in jobs:
            try:
                source, timing = resolve_source(source, complete)
                result = normalize(source, output, settings, dry_run=args.dry_run,
                                   timing_only=args.timing_only or timing)
                analysis = result['analysis']
                print(json.dumps(dict(source=str(source), output=str(output), status=result['status'],
                    reason=analysis['reason'], gain_db=analysis['gain_db'],
                    speech_seconds=analysis['speech_seconds'], speech_level_dbfs=analysis['speech_level_dbfs'],
                    noise_level_dbfs=analysis['noise_level_dbfs'])), flush=True)
            except (OSError, ValueError, RuntimeError, KeyError, TypeError) as error:
                failures.append(f'{source}: {error}')
                print(f'FAILED {failures[-1]}', file=sys.stderr, flush=True)
        if failures:
            raise ValueError('; '.join(failures))
        return 0
    except (OSError, ValueError, RuntimeError, KeyError, TypeError) as error:
        p.error(str(error))


if __name__ == '__main__':
    sys.exit(main())
