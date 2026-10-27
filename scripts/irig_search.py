"""Search the full originals of CH01-CH04 for provisional IRIG-B clock snapshots.

Independent of deflutter planning, denoising, rendering and acceptance. Screening
finds candidate carriers; decoding establishes provisional clock evidence. A
negative result describes only the declared search sensitivity and bandwidth.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re

from carrier_survey import SETTINGS, scan_file, write_json
from flutter_plan_store import planning_lock
from flutter_policy import digest
from irig_clock import SETTINGS as DECODE_SETTINGS, decode_survey

CHANNEL = re.compile(r'_CH(\d+)_', re.I)
CHANNELS = (1, 2, 3, 4)
DEFAULT_ROOT = Path('D:/Apollo_15_30-Track/irig_search')


def first_four(source):
    """Select by channel number, regardless of IRIG/voice labels or head name."""
    source = Path(source).resolve()
    if not source.is_dir():
        raise ValueError('Input must be a canonical tape folder')
    selected = {}
    for path in sorted(source.rglob('*')):
        if path.suffix.lower() != '.wav':
            continue
        match = CHANNEL.search(path.name)
        if not match or int(match[1]) not in CHANNELS:
            continue
        channel = int(match[1])
        if channel in selected:
            raise ValueError(f'Expected one concatenated original for CH{channel:02d}')
        selected[channel] = path
    if any((match := CHANNEL.search(path.name)) and int(match[1]) in CHANNELS
           for path in source.rglob('*.partial')):
        raise ValueError('First four channels contain incomplete .partial files')
    return selected


def clock_findings(output):
    """Index independent decoded runs; full frames stay in bound decoder caches."""
    clock = json.loads((output/'decoding/clock.json').read_text(encoding='utf-8'))
    snapshots, provisional_frames = [], 0
    for record in clock['records']:
        cache = Path(record['path'])
        if digest(cache) != record['sha256']:
            raise ValueError('Decoder evidence changed')
        decoded = json.loads(cache.read_text(encoding='utf-8'))
        source = decoded['signature']['source']
        for ei, excerpt in enumerate(decoded['excerpts']):
            for ai, attempt in enumerate(excerpt['attempts']):
                provisional_frames += sum(f['valid_fields'] for f in attempt['frames'])
                for ri, run in enumerate(attempt['sequential_runs']):
                    if len(run) < DECODE_SETTINGS['minimum_sequential_frames']:
                        continue
                    snapshots.append(dict(
                        source=source, decoder_cache=dict(path=str(cache), sha256=record['sha256']),
                        excerpt_index=ei, attempt_index=ai, run_index=ri,
                        filter=attempt['filter'], polarity=attempt['polarity'], frame_count=len(run),
                        first_frame=run[0], last_frame=run[-1],
                        status='decoded_provisional', clock_basis='unverified'))
    return snapshots, provisional_frames


def search_tape(source, output, legacy_repo=None, *, max_windows=None, screen_only=False):
    """Resumable full-timeline search with an explicit unknown/positive/negative result."""
    source, output = Path(source).resolve(), Path(output).resolve()
    if source == output or source in output.parents or output in source.parents:
        raise ValueError('Search output must be separate from originals')
    selected = first_four(source)
    if not screen_only and legacy_repo is None:
        raise ValueError('Decoding requires --legacy-repo; use --screen-only for carrier screening')
    output.mkdir(parents=True, exist_ok=True)
    with planning_lock(output):
        screening = output/'screening'
        screening.mkdir(exist_ok=True)
        report = dict(schema=1, tape=source.name, source=str(source), channels=list(CHANNELS),
                      missing_channels=sorted(set(CHANNELS)-selected.keys()), status='incomplete',
                      coverage_complete=False, decoding_complete=False, timecode_detected=None,
                      snapshots=[], implementation_sha256=digest(__file__),
                      screening_settings=SETTINGS, decoding_settings=DECODE_SETTINGS,
                      caveats=[
                          'Carrier candidates alone do not establish IRIG-B identity.',
                          'Detection requires at least four sequential valid decoded frames.',
                          'Negative results are bounded by screening bandwidth, sensitivity and island length.',
                          'Clock basis and absolute time remain unauthenticated.',
                          'Snapshots retain original sample coordinates; no cross-channel alignment is inferred.',
                          'Overlapping chunks, filters and polarities can report the same clock run.'])
        write_json(output/'search.json', report)
        index = dict(schema=1, tape=source.name, source=str(source), settings=SETTINGS,
                     scope='IRIG search on CH01-CH04 only; not an all-channel flutter survey',
                     complete=False, files=[])
        for channel, path in sorted(selected.items()):
            key = hashlib.sha256(str(path.relative_to(source)).encode()).hexdigest()[:16]
            cache = screening/(key+'.json')
            result = scan_file(path, cache, max_windows)
            index['files'].append(dict(path=str(path), channel=channel, cache=cache.name,
                                       sha256=digest(cache), complete=result['complete'],
                                       status=result['carrier_status']))
            write_json(screening/'survey.json', index)
            print(f'{source.name} CH{channel:02d}: {result["carrier_status"]}', flush=True)
        index['complete'] = bool(index['files']) and all(r['complete'] for r in index['files'])
        write_json(screening/'survey.json', index)
        report['coverage_complete'] = index['complete'] and not report['missing_channels']
        report['screening'] = dict(path=str(screening/'survey.json'), sha256=digest(screening/'survey.json'))
        if index['complete'] and not screen_only:
            decode_survey(screening, output/'decoding', legacy_repo)
            report['snapshots'], report['provisional_frame_observations'] = clock_findings(output)
            report['decoding_complete'] = True
            clock = output/'decoding/clock.json'
            report['decoding'] = dict(path=str(clock), sha256=digest(clock))
            if report['snapshots']:
                report.update(status='decoded_provisional', timecode_detected=True)
            elif report['coverage_complete']:
                report.update(status='no_timecode_detected', timecode_detected=False)
        elif index['complete']:
            report['status'] = 'screened_only'
        write_json(output/'search.json', report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, required=True, help='Original tape folder, or root with --all-tapes')
    parser.add_argument('--output', type=Path, default=DEFAULT_ROOT, help='Search root; each tape gets a subfolder')
    parser.add_argument('--all-tapes', action='store_true')
    parser.add_argument('--legacy-repo', type=Path, help='Apollo_Audio_Processing checkout containing the IRIG decoder')
    parser.add_argument('--screen-only', action='store_true', help='Find candidates without claiming decoded timecode')
    parser.add_argument('--max-windows', type=int, help='Per-file screening budget; omit to resume full coverage')
    args = parser.parse_args()
    if not args.screen_only and args.legacy_repo is None:
        parser.error('--legacy-repo is required unless --screen-only is selected')
    folders = sorted(p for p in args.input.glob('A15_T*') if p.is_dir()) if args.all_tapes else [args.input]
    if not folders:
        parser.error('No canonical tapes found')
    failures = []
    for folder in folders:
        try:
            report = search_tape(folder, args.output/folder.name, args.legacy_repo,
                                 max_windows=args.max_windows, screen_only=args.screen_only)
            print(f'{folder.name}: {report["status"]}; {len(report["snapshots"])} provisional runs', flush=True)
        except (ValueError, OSError, KeyError, ImportError) as exc:
            failures.append(f'{folder.name}: {exc}')
    if failures:
        parser.exit(1, '\n'.join(failures)+'\n')


if __name__ == '__main__':
    main()
