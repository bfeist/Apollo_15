"""Original-coordinate IRIG decoding and explicit Apollo 15 clock annotations.

Carrier findings never authenticate a clock. Sequential decodes remain
provisional until a separate reviewed clock-basis record authenticates them.
No legacy duration/frame correction is applied to audio.
"""
import argparse
import hashlib
from datetime import datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path
import sys

import numpy as np
from scipy import signal
import soundfile as sf

from carrier_survey import load_survey, source_record, write_json
from flutter_policy import digest

RANGE_ZERO = datetime(1971, 7, 26, 13, 34, tzinfo=timezone.utc)
SETTINGS = dict(version=1, minimum_sequential_frames=4, sample_interval_seconds=[.8, 1.25],
                historical_year_field=0, filters=['raw', 'highpass_400', 'bandpass_650_1350'],
                polarities=[1, -1], context_seconds=4., maximum_decode_chunk_seconds=60.,
                timing_uncertainty_seconds=.01)


def sequential_runs(frames, sr):
    runs, current, discontinuities = [], [], []
    previous = None
    for frame in frames:
        clock = ((frame['day_of_year']-1)*86400+frame['hours']*3600+frame['minutes']*60+frame['seconds'])
        if previous is not None:
            delta = clock-previous[0]
            if delta == 1-365*86400:
                delta = 1  # 1971 year rollover; retained, not relabelled as 1971.
            sample_delta = (frame['original_sample']-previous[1])/sr
            if delta != 1 or not SETTINGS['sample_interval_seconds'][0] <= sample_delta <= SETTINGS['sample_interval_seconds'][1]:
                if current:
                    runs.append(current)
                discontinuities.append(dict(original_sample=frame['original_sample'], clock_delta_seconds=delta,
                                            sample_delta_seconds=sample_delta,
                                            reason='repeated_frame' if delta == 0 else 'clock_or_sample_discontinuity'))
                current = []
        current.append(frame)
        previous = clock, frame['original_sample']
    if current:
        runs.append(current)
    return runs, discontinuities


def legacy_decoder(repo):
    repo = Path(repo).resolve()
    # Import in place; do not copy or modify the legacy implementation.
    if 'jdsp' not in sys.modules:
        path = repo/'py-jdsp/lib/__init__.py'
        spec = importlib.util.spec_from_file_location('jdsp', path, submodule_search_locations=[str(path.parent)])
        module = importlib.util.module_from_spec(spec)
        sys.modules['jdsp'] = module
        spec.loader.exec_module(module)
    sys.path.insert(0, str(repo/'processing_scripts'))
    from irig_b import IRIGBDecoder
    return IRIGBDecoder


def legacy_hashes():
    result = {}
    for name, module in sorted(sys.modules.items()):
        if name == 'irig_b' or name.startswith(('jdsp', 'apollo')):
            path = getattr(module, '__file__', None)
            if path and Path(path).is_file():
                result[name] = dict(path=str(Path(path).resolve()), sha256=digest(path))
    return result


def decode_excerpt(x, sr, start_sample, decoder):
    variants = [('raw', x), ('highpass_400', signal.sosfiltfilt(signal.butter(4, 400, fs=sr, btype='highpass', output='sos'), x)),
                ('bandpass_650_1350', signal.sosfiltfilt(signal.butter(4, [650, 1350], fs=sr, btype='bandpass', output='sos'), x))]
    attempts = []
    for name, samples in variants:
        for polarity in SETTINGS['polarities']:
            raw = []
            receiver = decoder(sr, raw.append)
            receiver.set_sample_number(start_sample)
            for i in range(0, len(samples), sr):
                receiver.process(polarity*samples[i:i+sr])
            frames = []
            for frame in raw:
                valid = bool(frame.ok_sbs_tm and frame.years == 0 and 1 <= frame.days <= 365
                             and 0 <= frame.hours < 24 and 0 <= frame.minutes < 60 and 0 <= frame.seconds < 60)
                frames.append(dict(day_of_year=int(frame.days), hours=int(frame.hours), minutes=int(frame.minutes),
                    seconds=int(frame.seconds), historical_year_bits=int(frame.years), control=int(frame.control),
                    sbs=int(frame.sbs), valid_fields=valid, original_sample=float(frame.absolute_sample.i)+float(frame.absolute_sample.f)))
            runs, breaks = sequential_runs([f for f in frames if f['valid_fields']], sr)
            attempts.append(dict(filter=name, polarity=polarity, frames=frames, discontinuities=breaks,
                                 sequential_runs=runs, longest_run=max(map(len, runs), default=0),
                                 status='decoded_provisional' if any(f['valid_fields'] for f in frames) else 'no_decode'))
    return attempts


def decode_survey(survey, output, legacy_repo):
    index, sources = load_survey(survey)
    output = Path(output).resolve()
    for row in sources:
        parent = Path(row['source']['path']).parent.resolve()
        if output == parent or parent in output.parents or output in parent.parents:
            raise ValueError('Decoder cache must be separate from originals')
    decoder = legacy_decoder(legacy_repo)
    provenance = dict(settings=SETTINGS, implementation_sha256=digest(__file__), legacy_code=legacy_hashes())
    output.mkdir(parents=True, exist_ok=True)
    records = []
    for number, row in enumerate(sources):
        source = row['source']
        cache = output/f'decode_{number:03d}.json'
        signature = dict(source=source, provenance=provenance, islands=row['islands'])
        if cache.exists():
            result = json.loads(cache.read_text(encoding='utf-8'))
            if result['signature'] != signature:
                raise ValueError('Decoder cache changed; choose a new decoder output')
        else:
            result = dict(signature=signature, status='screened_no_candidate', excerpts=[],
                          time_basis='unverified recorded clock; no GMT/GET authentication',
                          timing='Original samples; diagnostic zero-phase filtering, crossing bias uncalibrated',
                          mission=dict(name='Apollo 15', year=1971, range_zero_gmt=RANGE_ZERO.isoformat()))
            sr, n = source['sample_rate'], source['frames']
            with sf.SoundFile(source['path']) as stream:
                for island in row['islands']:
                    low, high = max(0, island['start_sample']-4*sr), min(n, island['end_sample']+4*sr)
                    for a in range(low, high, 52*sr):
                        b = min(high, a+60*sr)
                        if b-a < sr:
                            continue
                        stream.seek(a)
                        x = stream.read(b-a, dtype='float64')
                        attempts = decode_excerpt(x, sr, a, decoder)
                        result['excerpts'].append(dict(start_sample=a, end_sample=b, attempts=attempts,
                            excerpt_float64_sha256=hashlib.sha256(x.tobytes()).hexdigest()))
            if any(a['status'] == 'decoded_provisional' for e in result['excerpts'] for a in e['attempts']):
                result['status'] = 'decoded_provisional'
            if source_record(source['path']) != source:
                raise ValueError('Original changed during decoding')
            write_json(cache, result)
        records.append(dict(path=str(cache), sha256=digest(cache), status=result['status']))
        print(f'{Path(source["path"]).name}: {result["status"]}', flush=True)
    write_json(output/'clock.json', dict(tape=index['tape'], records=records, provenance=provenance))


def promote_run(frames, sr, authentication):
    """Human-reviewed basis is mandatory; no default Apollo 11 mission labels."""
    if (authentication.get('recorded_clock_basis') != 'GMT' or not authentication.get('reviewer')
            or not authentication.get('evidence') or authentication.get('status') != 'accepted'):
        raise ValueError('Recorded clock basis has not been authenticated')
    runs, breaks = sequential_runs(frames, sr)
    if breaks or len(runs) != 1 or len(frames) < SETTINGS['minimum_sequential_frames'] or any(not f.get('valid_fields') for f in frames):
        raise ValueError('Anchor needs several sequential valid original-coordinate frames')
    if any(b['day_of_year'] < a['day_of_year'] for a, b in zip(frames, frames[1:])):
        raise ValueError('Year rollover requires an explicit year basis for each side')
    return [dict(f, status='clock_anchor_validated',
                 get_seconds=((datetime(1971, 1, 1, tzinfo=timezone.utc)+timedelta(days=f['day_of_year']-1,
                    hours=f['hours'], minutes=f['minutes'], seconds=f['seconds']))-RANGE_ZERO).total_seconds(),
                 uncertainty_seconds=SETTINGS['timing_uncertainty_seconds']) for f in frames]


def carry_anchors(anchors, original_rate, original_start, render_start, tt, nominal):
    """The renderer maps original input time t to output nominal(t); never invert twice."""
    result = []
    for anchor in anchors:
        t = original_start+anchor['original_sample']/original_rate-render_start
        if tt[0] <= t <= tt[-1]:
            result.append(dict(anchor, defluttered_seconds=float(np.interp(t, tt, nominal)),
                               mapping_uncertainty_seconds=float(np.max(np.diff(tt)))))
    return result


def clock_constraints(anchors, model, tolerance_seconds=.05):
    """Check authenticated intervals against a slow curve without forcing clocks together.

An isolated anchor establishes an offset only. Unsupported gaps and clock
conflicts remain explicit; no extrapolated absolute clock is created.
"""
    from quindar_retime import timing_map
    t, out, _ = timing_map(model)
    accepted = sorted((a for a in anchors if a.get('status') == 'clock_anchor_validated'), key=lambda a: a['defluttered_seconds'])
    intervals = []
    for left, right in zip(accepted, accepted[1:]):
        if left.get('run_id') != right.get('run_id') or not left.get('run_id'):
            continue
        a, b = left['defluttered_seconds'], right['defluttered_seconds']
        if not t[0] <= a < b <= t[-1]:
            raise ValueError('Clock anchor outside slow model coverage')
        measured = right['get_seconds']-left['get_seconds']
        uncertainty = sum(v.get('uncertainty_seconds', 0)+v.get('mapping_uncertainty_seconds', 0) for v in (left, right))
        residual = float(np.interp(b, t, out)-np.interp(a, t, out)-measured)
        intervals.append(dict(start_seconds=a, end_seconds=b, measured_seconds=measured,
                              residual_seconds=residual, uncertainty_seconds=uncertainty,
                              status='consistent' if abs(residual) <= tolerance_seconds+uncertainty else 'conflict'))
    return dict(status='conflict' if any(r['status'] == 'conflict' for r in intervals) else 'consistent',
                anchors=accepted, intervals=intervals, extrapolation='none',
                single_anchor_determines_drift=False)


def authenticate(request_path, output):
    request_path = Path(request_path).resolve()
    request = json.loads(request_path.read_text(encoding='utf-8'))
    cache = (request_path.parent/request['decoder_cache']['path']).resolve()
    if digest(cache) != request['decoder_cache']['sha256']:
        raise ValueError('Decoder evidence changed')
    decoded = json.loads(cache.read_text(encoding='utf-8'))
    source = decoded['signature']['source']
    if source_record(source['path']) != source:
        raise ValueError('Original clock source changed')
    e, a, r = (request[k] for k in ('excerpt_index', 'attempt_index', 'run_index'))
    frames = decoded['excerpts'][e]['attempts'][a]['sequential_runs'][r]
    anchors = promote_run(frames, source['sample_rate'], request['authentication'])
    run_id = f'{digest(cache)}:{e}:{a}:{r}'
    anchors = [dict(v, run_id=run_id) for v in anchors]
    write_json(output, dict(source=source, anchors=anchors, authentication=request['authentication'],
                            decoder_cache=dict(path=str(cache), sha256=digest(cache)),
                            request_sha256=digest(request_path), status='clock_anchor_validated'))


def map_anchor_file(anchor_path, review_path, output):
    anchor_path, review_path = Path(anchor_path).resolve(), Path(review_path).resolve()
    anchors = json.loads(anchor_path.read_text(encoding='utf-8'))
    review = json.loads(review_path.read_text(encoding='utf-8'))
    source = anchors['source']
    if (review['status'] != 'complete' or review['experimental_transfer']
            or source_record(source['path']) != source
            or Path(source['path']).resolve() != Path(review['source']['path']).resolve()):
        raise ValueError('Anchors require the actual completed map of their original source; cross-channel clock alignment is not inferred')
    nominal_path = review_path.parent/'nominal_seconds.npy'
    if digest(nominal_path) != review['timing_artifacts']['nominal_seconds.npy']:
        raise ValueError('Applied timing map changed')
    nominal = np.load(nominal_path, allow_pickle=False)
    last = (review['frames']-1)/review['sample_rate']
    tt = np.arange(int(np.floor(last*200))+1)/200
    if last > tt[-1]+1e-12:
        tt = np.r_[tt, last]
    if nominal.shape != tt.shape or not np.isfinite(nominal).all() or np.any(np.diff(nominal) <= 0):
        raise ValueError('Invalid applied map')
    carried = carry_anchors(anchors['anchors'], source['sample_rate'], review['source']['start_seconds'],
                            review['start_seconds'], tt, nominal)
    if not carried:
        raise ValueError('No clock anchors overlap the applied flutter map')
    for row in carried:
        row['defluttered_seconds'] += review['start_seconds']  # Shared tape coordinates.
    write_json(output, dict(status='clock_anchor_validated', tape=review['tape'], channel=review['channel'],
        anchors=carried, anchor_source=dict(path=str(anchor_path), sha256=digest(anchor_path)),
        applied_review=dict(path=str(review_path), sha256=digest(review_path)),
        applied_map=dict(path=str(nominal_path), sha256=digest(nominal_path))))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--survey', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--legacy-repo', type=Path)
    parser.add_argument('--authenticate', type=Path, help='Reviewed clock-basis request bound to a saved decoder run')
    parser.add_argument('--map-anchors', type=Path, help='Authenticated original-sample anchors')
    parser.add_argument('--review', type=Path, help='Completed stage-05b review.json for the same original')
    args = parser.parse_args()
    try:
        if args.output.exists():
            if args.authenticate or args.map_anchors:
                raise ValueError('Clock annotation output must be a new JSON file')
        if args.authenticate:
            authenticate(args.authenticate, args.output)
        elif args.map_anchors:
            if args.review is None:
                raise ValueError('--map-anchors requires --review')
            map_anchor_file(args.map_anchors, args.review, args.output)
        elif args.survey and args.legacy_repo:
            decode_survey(args.survey, args.output, args.legacy_repo)
        else:
            raise ValueError('Choose --survey with --legacy-repo, --authenticate, or --map-anchors with --review')
    except (ValueError, OSError, KeyError, ImportError) as exc:
        parser.error(str(exc))


if __name__ == '__main__':
    main()
