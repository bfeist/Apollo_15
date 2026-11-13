"""Evidence-bounded crosstalk removal in canonical, pre-deflutter coordinates."""
from contextlib import ExitStack
import hashlib
import json
import math
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy import signal

from _bootstrap import REPO
from denoise_archive import atomic_json, sha256, validate_binding
from shared_tape import bound, verify_bindings
from crosstalk_model import RATE, CONTEXT, envelope, preliminary_path, subtract, trajectory

DEFAULT_CROSSTALK_ROOT = Path('D:/Apollo_15_30-Track/crosstalk_removed')
DEFAULT_EVIDENCE_ROOT = REPO/'reports/crosstalk_echo_Audition_20261108_v2'
ALGORITHM = 'bounded_two_path_crosstalk_v1'


def tape_key(name):
    return name.removeprefix('A15_')


def denoised_source(row, folder):
    folder = Path(folder).resolve()
    saved = json.loads((folder/'denoised.json').read_text(encoding='utf-8'))
    path = validate_binding(folder, saved, row, 0, row['frames'], version='v2')
    if saved['action'] != 'denoise_v2' or sha256(folder/'cleanup.jsonl') != saved['cleanup_log_sha256']:
        raise ValueError('Stage 04 requires completed v2 denoising and unchanged cleanup evidence')
    if row['sample_rate'] != RATE:
        raise ValueError('Stage 04 requires 8 kHz targets and donors')
    return path.resolve(), saved


def evidence_for(root, tape, channel):
    """Reuse explicit repeat intervals and the tape/channel direction registry.

    An absent directory is an error; an inspected catalog without evidence for
    this channel is an explicit bypass. Historical coefficients are never reused
    on different denoised audio.
    """
    root = Path(root).resolve()
    registry_path = root/'directions.json'
    registry = json.loads(registry_path.read_text(encoding='utf-8'))
    directions = [v['remove'] for k, v in registry.items()
                  if k in {f'{tape_key(tape)}/CH{channel:02d}', f'A15_{tape_key(tape)}/CH{channel:02d}'}]
    if len(set(directions)) > 1 or any(d not in {'earlier', 'later'} for d in directions):
        raise ValueError('Conflicting or invalid tape/channel echo direction')
    direction = directions[0] if directions else None
    bindings, events = [bound(registry_path)], []
    feedback = root/'listening_feedback.json'
    if feedback.exists():
        bindings.append(bound(feedback))
    for path in sorted(root.rglob('review.json')):
        review = json.loads(path.read_text(encoding='utf-8'))
        if tape_key(review.get('tape', '')) != tape_key(tape) or review.get('target') != channel:
            continue
        if direction is None or review['direction'] != direction:
            raise ValueError('Repeat evidence disagrees with the fixed tape/channel direction')
        if not review.get('direction_evidence'):
            raise ValueError('Repeat evidence must include within-target matches')
        start, duration = float(review['start']), float(review['duration'])
        a, b = map(float, review['application_interval'])
        ca, cb = map(float, review['calibration_interval'])
        if (not all(math.isfinite(v) for v in [start, duration, a, b, ca, cb])
                or not 0 < duration <= 60 or not start <= a < ca < cb <= b <= start+duration
                or cb-ca < .05 or b-a < .16):
            raise ValueError('Invalid bounded repeat/calibration interval')
        paths = {}
        for key in ('main_tracking', 'echo_tracking'):
            values = np.asarray(review[key]['donor_minus_target_seconds'], dtype=float)
            if values.ndim != 1 or not len(values) or not np.isfinite(values).all():
                raise ValueError('Missing finite measured donor alignment')
            polarity = None
            if key == 'echo_tracking' and 'time' in review[key]:
                times = np.asarray(review[key]['time'], dtype=float)+start
                correlations = np.asarray(review[key]['correlation'], dtype=float)
                if (times.shape != values.shape or correlations.shape != values.shape
                        or not np.isfinite(times).all() or not np.isfinite(correlations).all()):
                    raise ValueError('Invalid measured echo track coordinates')
                supported = (times >= a) & (times <= b)
                if not np.any(supported):
                    raise ValueError('No measured echo path inside application support')
                # Outside the evidenced phrase the historical tracker merely
                # held a path. Those weak windows must not widen the echo search
                # or reverse its measured polarity (notably CH17 is inverted).
                values = values[supported]
                positive = np.mean(correlations[supported] > 0)
                polarity = 1 if positive >= .8 else -1 if positive <= .2 else None
            center = float(np.median(values))
            radius = (max(.00035, float(np.max(abs(values-center)))+1/RATE) if key == 'echo_tracking'
                      else max(.0005, float(np.max(abs(values-center)))+.002))
            if radius > .05 or abs(center)+radius >= 1.9:
                raise ValueError('Measured path exceeds bounded tracking context')
            paths[key] = dict(nominal=center, radius=radius)
            if polarity is not None:
                paths[key]['polarity'] = polarity
        separation = paths['main_tracking']['nominal']-paths['echo_tracking']['nominal']
        if not .8 < abs(separation) < 1.9 or (separation < 0) != (direction == 'earlier'):
            raise ValueError('Measured main-to-echo separation contradicts the fixed direction')
        donor = review['donor']
        if not isinstance(donor, int) or not 1 <= donor <= 30 or donor == channel:
            raise ValueError('Invalid denoised donor channel')
        events.append(dict(start=start, duration=duration, application=[a, b], calibration=[ca, cb],
                           donor=donor, direction=direction, separation=separation,
                           paths=paths, evidence=bound(path), historical_sources=review['sources']))
        bindings.append(bound(path))
    events.sort(key=lambda e: e['application'][0])
    if any(a['application'][1] > b['application'][0] for a, b in zip(events, events[1:])):
        raise ValueError('Overlapping repeat intervals must be resolved before subtraction')
    return direction, events, bindings


def read_context(path, row, start, duration):
    offset = round((start-row['start_seconds'])*RATE)-CONTEXT
    frames = round(duration*RATE)+2*CONTEXT
    if offset < 0 or offset+frames > row['frames']:
        return None
    with sf.SoundFile(path) as stream:
        stream.seek(offset)
        audio = stream.read(frames, dtype='float64')
    if len(audio) != frames or not np.isfinite(audio).all():
        raise ValueError('Unreadable or nonfinite denoised context')
    return audio


def process_event(raw, donor, event):
    """Remeasure two paths on current v2 audio; bypass uncertain repeats."""
    kernel = signal.firwin(257, [300, 3000], fs=RATE, pass_zero=False)
    target = signal.fftconvolve(raw, kernel, mode='same')
    reference = signal.fftconvolve(donor, kernel, mode='same')
    times = event['start']+(np.arange(len(raw))-CONTEXT)/RATE
    ca, cb = event['calibration']
    calibration = (times >= ca) & (times < cb)
    # Establish the second occurrence within the target, independently of any
    # donor match. Search only the already established side of this channel.
    first, last = np.flatnonzero(calibration)[[0, -1]]
    separation = event['separation']
    center, radius = round(-separation*RATE), round(.03*RATE)
    low, high = first+center-radius, last+1+center+radius
    if low < 0 or high > len(target):
        return np.zeros(len(raw)), dict(status='bypassed', reason='insufficient within-target context')
    phrase, search = target[first:last+1], target[low:high]
    dots = signal.correlate(search, phrase, mode='valid', method='fft')
    energy = np.r_[0., np.cumsum(search*search)]
    correlation = dots/np.sqrt(np.maximum((energy[len(phrase):]-energy[:-len(phrase)])*
                                        np.dot(phrase, phrase), 1e-30))
    peak = int(np.argmax(abs(correlation)))
    match = float(correlation[peak])
    level = float(10*np.log10(max(np.sum(search[peak:peak+len(phrase)]**2), 1e-30)/
                              max(np.sum(phrase**2), 1e-30)))
    detail = dict(within_target_correlation=match, main_over_echo_db=level,
                  echo_to_main_seconds=(center-radius+peak)/RATE)
    if abs(match) < .25:
        return np.zeros(len(raw)), dict(detail, status='bypassed', reason='repeat not reestablished on v2 audio')
    main, main_track = trajectory(target-preliminary_path(target, reference), reference,
                                   **event['paths']['main_tracking'])
    echo, echo_track = trajectory(target, reference, **event['paths']['echo_tracking'])
    delta = np.array(main_track['donor_minus_target_seconds'])-np.array(echo_track['donor_minus_target_seconds'])
    if np.any((delta < 0) != (event['direction'] == 'earlier')):
        return np.zeros(len(raw)), dict(detail, status='bypassed', reason='local path contradicts fixed direction')
    support = envelope(times, *event['application'])
    # Use the frozen-coefficient B variant: positive feedback did not distinguish
    # B from C, so do not broaden coefficient adaptation by default.
    fixed, _, fits = subtract(target, main, echo, calibration, support)
    if not np.isfinite(fixed).all():
        raise ValueError('Nonfinite crosstalk prediction')
    return fixed, dict(detail, status='applied' if fits['fixed_accepted'] else 'bypassed',
        reason='withheld-block two-path check', main_tracking=main_track, echo_tracking=echo_track,
        fixed_coefficients=fits['fixed_coefficients'], fixed_accepted=fits['fixed_accepted'])


def prepared_crosstalk(row, folder):
    """Validate a stage-04 product for downstream shared-map rendering."""
    folder = Path(folder).resolve()
    saved = json.loads((folder/'crosstalk.json').read_text(encoding='utf-8'))
    if (saved['status'] != 'complete' or saved['algorithm'] != ALGORITHM
            or saved['source'] != row or saved['cleanup_version'] != 'v2'
            or saved['frames'] != row['frames'] or saved['sample_rate'] != row['sample_rate']
            or saved['start_seconds'] != row['start_seconds'] or saved['gain'] != 1.
            or saved['retimed']):
        raise ValueError('Incomplete, stale, or retimed stage-04 output')
    verify_bindings(saved['input_bindings'])
    path = folder/'crosstalk.wav'
    info = sf.info(path)
    if (info.frames, info.samplerate, info.channels, info.subtype) != (row['frames'], row['sample_rate'], 1, 'FLOAT'):
        raise ValueError('Stage-04 WAV header mismatch')
    return path, saved


def render_channel(inventory, channel, denoised_root, output, evidence_root=DEFAULT_EVIDENCE_ROOT,
                   *, dry_run=False, progress=print):
    tape = inventory['tape']
    rows = {r['channel']: r for r in inventory['channels']}
    if channel not in rows:
        raise ValueError('Requested channel is absent from tape')
    row = rows[channel]
    if row['sample_rate'] != RATE:
        raise ValueError('Stage 04 currently requires mono 8 kHz audio')
    direction, events, bindings = evidence_for(evidence_root, tape, channel)
    stagger_path = Path(evidence_root)/'stagger.json'
    staggers = json.loads(stagger_path.read_text(encoding='utf-8')) if stagger_path.exists() else {}
    if stagger_path.exists():
        bindings.append(bound(stagger_path))
    for ch in range(1, 31):
        staggers.setdefault(f'{tape_key(tape)}/CH{ch:02d}', dict(group='unknown', reference_channel=None,
            coordinate_domain='canonical source time before de-flutter',
            reason='Echo direction is not a main-utterance stagger measurement'))
    output = Path(output).resolve()
    protected = [Path(denoised_root).resolve(), Path(evidence_root).resolve(),
                 *[Path(r['path']).resolve().parent for r in rows.values()]]
    if any(output == p or output in p.parents or p in output.parents for p in protected):
        raise ValueError('Output must not overlap inputs or saved evidence')
    if output.exists():
        raise ValueError('Stage-04 destination must be new; existing results are immutable')
    sources = {}
    # A missing donor is a documented bypass, never a fallback to originals or
    # an already processed donor. Malformed completed inputs remain errors.
    missing = []
    for ch in sorted({channel, *[e['donor'] for e in events]}):
        folder = Path(denoised_root)/tape/f'CH{ch:02d}'
        if ch != channel and (ch not in rows or not (folder/'denoised.json').is_file()):
            missing.append(ch)
            continue
        path, saved = denoised_source(rows[ch], folder)
        sources[ch] = dict(path=path, saved=saved)
        bindings.extend([bound(folder/'denoised.json'), bound(folder/'cleanup.jsonl')])
    for event in events:
        for ch in (channel, event['donor']):
            if ch not in rows:
                continue
            old = event['historical_sources'][str(ch)]['signature']
            if (old['offset_frames'] != 0 or old['frames'] != rows[ch]['frames']
                    or any(old['source'][k] != rows[ch][k]
                           for k in ('path', 'bytes', 'mtime_ns', 'channel', 'frames', 'sample_rate'))):
                raise ValueError('Saved repeat evidence belongs to a different source timeline')
    summary = dict(schema=1, stage='04', algorithm=ALGORITHM, status='preflight',
        tape=tape, channel=channel, source=row, frames=row['frames'], sample_rate=RATE,
        start_seconds=row['start_seconds'], cleanup_version='v2', gain=1., retimed=False,
        direction=direction, variant='B_frozen_coefficients', input_bindings=bindings,
        sources={ch: dict(file=str(s['path']), audio_sha256=s['saved']['audio']['sha256'],
                          algorithm=s['saved']['algorithm']) for ch, s in sources.items()},
        events=[], eligible_seconds=sum(e['application'][1]-e['application'][0] for e in events),
        applied_seconds=0., missing_donors=missing,
        coordinate_domain='canonical tape seconds before shared de-flutter; original channel offsets retained',
        stagger_assignments=staggers,
        implementation=[bound(__file__), bound(Path(__file__).with_name('crosstalk_model.py'))],
        limitations=['Saved listening feedback covers a bounded phrase, not full-tape efficacy.',
                    'Withheld residual energy does not prove preservation of overlapping wanted speech.'])
    if dry_run:
        summary['planned_events'] = events
        return summary
    for source in sources.values():
        if sha256(source['path']) != source['saved']['audio']['sha256']:
            raise ValueError('Completed v2 audio hash changed')
    verify_bindings(bindings)
    output.mkdir(parents=True)
    atomic_json(output/'evidence.json', dict(direction=direction, events=events, stagger_assignments=staggers))
    summary['input_bindings'].append(bound(output/'evidence.json'))
    summary['status'] = 'running'
    atomic_json(output/'crosstalk.json', summary)
    try:
        # Whole channels stream unchanged; only small evidenced contexts are
        # fitted. Predictions are spooled, so memory never grows with tape size.
        patches = []
        for i, event in enumerate(events):
            result = dict(application_interval=event['application'], donor=event['donor'],
                          status='bypassed', reason='missing completed denoised donor')
            if event['donor'] not in missing:
                raw = read_context(sources[channel]['path'], row, event['start'], event['duration'])
                donor = read_context(sources[event['donor']]['path'], rows[event['donor']], event['start'], event['duration'])
                if raw is None or donor is None:
                    result['reason'] = 'insufficient source context'
                else:
                    prediction, detail = process_event(raw, donor, event)
                    result.update(detail)
                    if detail['status'] == 'applied':
                        a, b = [round((t-row['start_seconds'])*RATE) for t in event['application']]
                        local = round((event['application'][0]-event['start'])*RATE)+CONTEXT
                        path = output/f'removed_{i:04d}.npy'
                        np.save(path, prediction[local:local+b-a], allow_pickle=False)
                        patches.append((a, b, path))
                        summary['applied_seconds'] += (b-a)/RATE
            summary['events'].append(result)
        temporary = output/'crosstalk.wav.partial'
        digest, peak = hashlib.sha256(), 0.
        with ExitStack() as stack:
            src = stack.enter_context(sf.SoundFile(sources[channel]['path']))
            dest = stack.enter_context(sf.SoundFile(temporary, 'w', samplerate=RATE, channels=1,
                subtype='FLOAT', format='RF64' if row['frames']*4+4096 >= 2**32 else 'WAV'))
            for begin in range(0, row['frames'], 30*RATE):
                audio = src.read(min(30*RATE, row['frames']-begin), dtype='float64')
                if not len(audio) or not np.isfinite(audio).all():
                    raise ValueError('Invalid stage-04 source samples')
                for a, b, path in patches:
                    lo, hi = max(begin, a), min(begin+len(audio), b)
                    if lo < hi:
                        prediction = np.load(path, allow_pickle=False)
                        audio[lo-begin:hi-begin] -= prediction[lo-a:hi-a]
                audio = audio.astype('float32')
                if not np.isfinite(audio).all():
                    raise ValueError('Nonfinite stage-04 output')
                dest.write(audio)
                digest.update(audio.tobytes())
                peak = max(peak, float(np.max(abs(audio))))
                if begin % (600*RATE) == 0:
                    progress(f'04 {tape} CH{channel:02d}: {(begin+len(audio))/row["frames"]:.1%}')
        verify_bindings(bindings)
        for source in sources.values():
            if sha256(source['path']) != source['saved']['audio']['sha256']:
                raise ValueError('Denoised source changed during stage 04')
        verified = hashlib.sha256()
        with sf.SoundFile(temporary) as stream:
            if (len(stream), stream.samplerate, stream.channels, stream.subtype) != (row['frames'], RATE, 1, 'FLOAT'):
                raise ValueError('Stage-04 output header mismatch')
            for block in stream.blocks(blocksize=30*RATE, dtype='float32'):
                verified.update(block.tobytes())
        if verified.hexdigest() != digest.hexdigest():
            raise ValueError('Stage-04 write verification failed')
        temporary.replace(output/'crosstalk.wav')
        summary.update(status='complete', audio=dict(filename='crosstalk.wav', sha256=sha256(output/'crosstalk.wav'),
            decoded_float32_sha256=digest.hexdigest(), peak=peak),
            bypass_reason=None if summary['applied_seconds'] else 'no usable evidenced repeat on current v2 audio')
        atomic_json(output/'crosstalk.json', summary)
    except BaseException as error:
        summary.update(status='failed', error=str(error))
        atomic_json(output/'crosstalk.json', summary)
        raise
    return summary
