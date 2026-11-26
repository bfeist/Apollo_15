"""Stage 08: conservative speech-level gain and the final 8 kHz, 16-bit delivery WAV.

The level measurement is a voiced-speech activity heuristic, not P.56, LUFS, or a
transcription model. Only sustained, harmonic, non-tonal activity above the estimated
floor can request gain. Uncertain channels (silence, noise, tones, too little speech)
get no boost, so tape noise on empty channels is not amplified.

Every channel then becomes the delivery format: the earlier stages' working float audio is
resampled to 8 kHz if it is not already at 8 kHz, scaled by its one constant gain,
kept below the peak ceiling (-1 dBFS) and converted to 16-bit PCM with TPDF dither.
The peak ceiling also applies to bypassed channels. Isolated sharp
impulses (very short spikes) never set the gain; a 5 ms limiter holds them at the
ceiling and may touch nothing else. So neither the gain change nor the conversion
clips. The resampler, dither and conversion settings are recorded in ``normalization.json``.
"""
from dataclasses import asdict, dataclass
import hashlib
import json
import math
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy.ndimage import maximum_filter1d, uniform_filter1d
from scipy.signal import resample_poly

from flutter_plan_store import planning_lock

DEFAULT_NORMALIZED_ROOT = Path('D:/Apollo_15_30-Track/normalized')
METHOD = 'conservative_voiced_speech_rms_v5'
FRAME_SECONDS = .02
DELIVERY_RATE = 8000
DELIVERY_SUBTYPE = 'PCM_16'
FULL_SCALE = 32768.  # PCM_16 code = round(sample * 32768 + dither), as soundfile decodes it
RESAMPLER = dict(function='scipy.signal.resample_poly', window=('kaiser', 5.0), padtype='constant',
                 half_length='10 x max(up, down) taps (scipy default)', streaming='30 s blocks with input halos; '
                 'equal to one whole-file resample_poly call')
DITHER = dict(type='TPDF', amplitude='+/-1 LSB (difference of two uniform [0, 1) LSB variates)', noise_shaping=None,
              quantization='round half to even after dither', generator='numpy PCG64',
              seed='first 8 bytes of the decoded float32 sha256 of the delivered-rate input, big-endian')


@dataclass(frozen=True)
class Settings:
    target_speech_dbfs: float = -23.
    max_boost_db: float | None = None
    max_noise_dbfs: float | None = None
    min_snr_db: float = 9.
    min_speech_seconds: float = 3.
    peak_ceiling_dbfs: float = -1.

    def validate(self):
        for name, value in asdict(self).items():
            if value is None and name in {'max_boost_db', 'max_noise_dbfs'}:
                continue
            if value is None or not math.isfinite(value):
                raise ValueError('Normalization settings must be finite')
        if not -60 <= self.target_speech_dbfs <= -12:
            raise ValueError('Speech RMS target must be between -60 and -12 dBFS')
        if self.max_boost_db is not None and not 0 <= self.max_boost_db <= 60:
            raise ValueError('Optional maximum boost must be between 0 and 60 dB')
        if (self.max_noise_dbfs is not None and not -100 <= self.max_noise_dbfs <= -20
                or not 6 <= self.min_snr_db <= 30):
            raise ValueError('Invalid noise ceiling or speech/noise margin')
        if not 1 <= self.min_speech_seconds <= 60 or not -12 <= self.peak_ceiling_dbfs < 0:
            raise ValueError('Invalid speech duration or peak ceiling')


def db(value):
    return 20*np.log10(np.maximum(value, 1e-9))


def signature(path):
    info, stat = sf.info(path), Path(path).stat()
    if info.channels != 1 or info.frames == 0 or info.samplerate < 8000:
        raise ValueError('Require nonempty mono audio at 8000 Hz or higher')
    return dict(path=str(Path(path).resolve()), bytes=stat.st_size, mtime_ns=stat.st_mtime_ns,
                frames=info.frames, sample_rate=info.samplerate, subtype=info.subtype)


def ranges(mask):
    changes = np.diff(np.r_[False, mask, False].astype(np.int8))
    return list(zip(np.flatnonzero(changes == 1), np.flatnonzero(changes == -1)))


def speech_mask(features, floor, settings):
    """Reject impulses, stationary tones and noise before joining voiced syllables."""
    rms, band_rms, peak, harmonic, concentration, fraction = features.T
    impulse = peak > np.maximum(rms*8., 1e-8)
    impulse = maximum_filter1d(impulse.astype(np.uint8), size=13) > 0
    candidate = ((db(band_rms) >= floor+settings.min_snr_db) & (db(rms) > -65)
                 & (harmonic >= .35) & (concentration < .65) & (fraction > .45)
                 & ~impulse)
    joined = candidate.copy()
    # Bridge at most 120 ms between voiced syllables; never count the gaps as
    # speech or let one click and detector hangover become a transmission.
    for a, b in ranges(~candidate):
        if a and b < len(candidate) and b-a <= 6 and not impulse[a:b].any():
            joined[a:b] = True
    accepted = np.zeros(len(candidate), dtype=bool)
    for a, b in ranges(joined):
        if b-a >= 15 and candidate[a:b].mean() >= .5:
            accepted[a:b] = candidate[a:b]
    return accepted, impulse


def program_headroom(source, features, rate, frames):
    """Protect every ordinary sample; only isolated sharp impulses are exempt.

    Candidate frames have a peak/RMS ratio above 8 and exceed neighboring
    peaks by 3x. The reference excludes the central 100 ms and covers 0.5 s
    either side. Long events and events without both context sides stay protected.
    """
    size = round(rate*FRAME_SECONDS)
    rms, peaks = features[:, 0], features[:, 2]
    neighbor = np.zeros_like(peaks)
    for lag in range(3, 26):
        np.maximum(neighbor[lag:], peaks[:-lag], out=neighbor[lag:])
        np.maximum(neighbor[:-lag], peaks[lag:], out=neighbor[:-lag])
    candidate = (peaks > 8*np.maximum(rms, 1e-9)) & (peaks > 3*np.maximum(neighbor, 1e-9))
    candidate[:25] = False
    candidate[-25:] = False
    for a, b in ranges(candidate):
        if b-a > 2:
            candidate[a:b] = False
    peak = float(np.max(peaks[~candidate], initial=0))
    impulses = []
    with sf.SoundFile(source) as audio:
        for i in np.flatnonzero(candidate):
            start = int(i)*size
            audio.seek(start)
            x = audio.read(size, dtype='float32')
            # Include the short impulse's decay above the surrounding peak,
            # rather than treating that decay as a new program-level peak.
            exceptional = np.abs(x) > max(float(neighbor[i]), 1e-8)
            peak = max(peak, float(np.max(np.abs(x[~exceptional]), initial=0)))
            impulses.extend([start+int(a), start+int(b)] for a, b in ranges(exceptional))
        # Incomplete final frames are never exempt from headroom protection.
        audio.seek(len(features)*size)
        tail = audio.read(frames-len(features)*size, dtype='float32')
        peak = max(peak, float(np.max(np.abs(tail), initial=0)))
    return dict(program_peak=peak, isolated_impulse_intervals=impulses,
                isolated_impulse_samples=sum(b-a for a,b in impulses),
                method='isolated_impulse_exclusion_with_all_other_samples_protected')


def analyze(source, settings=Settings(), *, timing_only=False, progress=print):
    settings.validate()
    source = Path(source).resolve()
    binding = signature(source)
    rate = binding['sample_rate']
    size = round(rate*FRAME_SECONDS)
    nfft = 2**math.ceil(math.log2(2*size))
    freq = np.fft.rfftfreq(nfft, 1/rate)
    band = (freq >= 150) & (freq <= 3400)
    window = np.hanning(size)
    window_power = np.sum(window**2)
    rows, digest = [], hashlib.sha256()
    raw_peak = 0.
    with sf.SoundFile(source) as audio:
        for block in audio.blocks(blocksize=size*1500, dtype='float32'):
            if not np.isfinite(block).all():
                raise ValueError('Non-finite input audio')
            digest.update(block.astype('<f4', copy=False).tobytes())
            raw_peak = max(raw_peak, float(np.max(np.abs(block), initial=0)))
            count = len(block)//size
            if not count:
                continue
            x = block[:count*size].reshape(count, size).astype(np.float64)
            rms = np.sqrt(np.mean(x*x, axis=1))
            peak = np.max(np.abs(x), axis=1)
            centered = x-x.mean(axis=1, keepdims=True)
            power = np.abs(np.fft.rfft(centered*window, n=nfft))**2
            total = power.sum(axis=1)+1e-30
            selected = power[:, band]
            energy = selected.sum(axis=1)+1e-30
            band_rms = np.sqrt(2*energy/(nfft*window_power))
            # A 150 Hz concentration rejects narrow tones even when they
            # have strong periodicity (hum, Quindar and timing carriers).
            width = max(3, int(150*nfft/rate)//2*2+1)
            concentration = np.max(uniform_filter1d(selected, width, axis=1)*width, axis=1)/energy
            power[:, ~band] = 0
            corr = np.fft.irfft(power, n=nfft)
            harmonic = np.max(corr[:, max(1, rate//400):rate//75+1], axis=1)/(corr[:, 0]+1e-30)
            rows.append(np.column_stack((rms, band_rms, peak, harmonic, concentration,
                                         energy/total)).astype(np.float32))
            if audio.tell() % (size*1500*20) == 0:
                progress(f'Measured {audio.tell()/rate/60:.1f} minutes')
    if signature(source) != binding:
        raise ValueError('Source changed during measurement')
    features = np.concatenate(rows) if rows else np.empty((0, 6), dtype=np.float32)
    binding['decoded_float32_sha256'] = digest.hexdigest()
    result = dict(method=METHOD, settings=asdict(settings), source=binding,
                  input_peak=raw_peak, speech_seconds=0., speech_intervals=[],
                  speech_level_dbfs=None, noise_level_dbfs=None, gain_db=0., gain=1.,
                  reason='timing_only' if timing_only else 'insufficient_speech',
                  measurement='Median full-band 20 ms RMS of accepted voiced speech; not LUFS or P.56',
                  detector='Spectral periodicity, tone rejection, energy margin and sustained activity')
    if not len(features) or timing_only:
        return bypass_headroom(result, source, features, settings, timing_only)
    floor = float(np.percentile(db(features[:, 1]), 20))
    mask, impulses = speech_mask(features, floor, settings)
    seconds = float(mask.sum()*size/rate)
    background = ~(mask | impulses)
    # Silence remains part of the floor estimate. There is no minimum activity
    # percentage: a few real transmissions on a long tape can qualify.
    noise = float(np.percentile(db(features[background, 0]), 50)) if background.any() else None
    result.update(candidate_seconds=seconds, noise_level_dbfs=noise,
                  speech_band_floor_dbfs=floor, rejected_impulse_seconds=float(impulses.sum()*size/rate),
                  candidate_intervals=[[int(a*size), int(b*size)] for a, b in ranges(mask)])
    if seconds < settings.min_speech_seconds:
        return bypass_headroom(result, source, features, settings, timing_only)
    level = float(np.median(db(features[mask, 0])))
    contrast = level-noise if noise is not None else None
    result.update(candidate_level_dbfs=level, candidate_contrast_db=contrast)
    # A speech-band margin alone can admit weak periodic background features.
    # Also require separation from the full-band background that gain would
    # amplify. Confirmed empty T510077 CH01/CH02 exposed this failure mode.
    if contrast is None or contrast < settings.min_snr_db:
        result['reason'] = 'insufficient_speech_contrast'
        return bypass_headroom(result, source, features, settings, timing_only)
    result.update(speech_seconds=seconds, speech_intervals=result['candidate_intervals'])
    # Stage 08 is independent of the old denoise gain policy. Use one scalar,
    # protecting ordinary peaks instead of forcing quiet median speech to its
    # target through broad limiting. Optional policy caps are caller-selected.
    desired_gain_db = settings.target_speech_dbfs-level
    headroom = program_headroom(source, features, rate, binding['frames'])
    if signature(source) != {k:v for k,v in binding.items() if k != 'decoded_float32_sha256'}:
        raise ValueError('Source changed during headroom measurement')
    headroom_db = settings.peak_ceiling_dbfs-float(db(headroom['program_peak']))
    gain_db = min(desired_gain_db, headroom_db)
    if settings.max_boost_db is not None:
        gain_db = min(gain_db, settings.max_boost_db)
    if settings.max_noise_dbfs is not None:
        gain_db = min(gain_db, max(0., settings.max_noise_dbfs-noise))
    reached = abs(level+gain_db-settings.target_speech_dbfs) < .01
    result.update(speech_level_dbfs=level, gain_db=gain_db, gain=10**(gain_db/20),
                  reason='speech_normalized' if gain_db else ('speech_at_target' if reached else 'gain_limited'),
                  expected_speech_dbfs=level+gain_db,
                  target_reached=reached, gain_policy='constant_speech_target_with_program_headroom',
                  desired_gain_db=desired_gain_db, headroom_gain_db=headroom_db,
                  headroom_limited=headroom_db < desired_gain_db, headroom=headroom)
    return result


def bypass_headroom(result, source, features, settings, timing_only):
    """A channel without speech gain keeps unity gain unless its ordinary peak exceeds the ceiling.

    Only attenuation is possible, never a boost. Isolated impulses are exempt exactly as for speech
    channels, and the renderer limits them. Timing-only channels (IRIG) are never limited: their whole
    sample peak sets the attenuation.
    """
    if timing_only or not len(features):
        peak, headroom = result['input_peak'], None
    else:
        binding = result['source']
        headroom = program_headroom(source, features, binding['sample_rate'], binding['frames'])
        peak = headroom['program_peak']
    gain_db = min(0., settings.peak_ceiling_dbfs-float(db(peak))) if peak > 0 else 0.
    result.update(gain_db=gain_db, gain=10**(gain_db/20), headroom=headroom, headroom_limited=gain_db < 0,
                  gain_policy='bypass: unity gain, attenuated only to keep the peak ceiling')
    return result


def resample_file(source, output, rate_out=DELIVERY_RATE, block_seconds=30, progress=print):
    """Write ``source`` (mono float) at ``rate_out`` as float32; blocks give the same samples as one call."""
    with sf.SoundFile(source) as audio:
        rate, frames = audio.samplerate, audio.frames
        g = math.gcd(rate, rate_out)
        up, down = rate_out//g, rate//g
        half = 10*max(up, down)
        halo = down*(math.ceil((half/up+2)/down)+1)  # input samples, a multiple of down
        block = down*max(1, round(block_seconds*rate/down))
        total = -(-frames*up//down)
        written = 0
        fmt = 'RF64' if total*4 >= 0xffffff00 else 'WAV'
        with sf.SoundFile(output, 'w', samplerate=rate_out, channels=1, subtype='FLOAT', format=fmt) as out:
            for start in range(0, frames, block):
                end = min(frames, start+block)
                left, right = start-halo, end+halo
                audio.seek(max(0, left))
                x = audio.read(min(frames, right)-max(0, left), dtype='float64')
                x = np.r_[np.zeros(max(0, -left)), x, np.zeros(max(0, right-frames))]
                if not np.isfinite(x).all():
                    raise ValueError('Non-finite input audio')
                y = resample_poly(x, up, down, window=RESAMPLER['window'], padtype='constant')
                skip = halo*up//down
                count = min(total, -(-end*up//down) if end == frames else end*up//down)-start*up//down
                out.write(y[skip:skip+count].astype('<f4'))
                written += count
                progress(f'Resampled {end/rate/60:.1f} minutes')
    if written != total:
        raise ValueError('Resampled frame count mismatch')
    return dict(RESAMPLER, input_rate=rate, output_rate=rate_out, up=up, down=down, input_frames=frames,
                output_frames=total)


def dither_and_quantize(samples, rng):
    """TPDF dither of +/-1 LSB, then round to 16-bit codes; the caller keeps the peak below full scale."""
    noise = rng.random(len(samples))-rng.random(len(samples))
    codes = np.rint(samples*FULL_SCALE+noise)
    if codes.size and (codes.max() > 32767 or codes.min() < -32768):
        raise ValueError('16-bit conversion would clip')
    return codes.astype('<i2')


def limit_peaks(samples, rate, ceiling):
    """Symmetric 5 ms peak-envelope limiter, no delay or duration change.

    Averaging maxima with the same radius keeps the envelope above every input
    magnitude. Call with a 10 ms halo on each side for block-independent output.
    """
    radius = max(1, round(rate*.005))
    peaks = maximum_filter1d(np.abs(samples), 2*radius+1, mode='nearest')
    envelope = uniform_filter1d(peaks, 2*radius+1, mode='nearest')
    envelope = np.maximum(envelope, np.abs(samples))  # floating-point safety
    reduction = np.minimum(1., ceiling/np.maximum(envelope, 1e-30))
    return samples*reduction, reduction


def write_json(path, record):
    pending = path.with_suffix('.json.partial')
    pending.write_text(json.dumps(record, indent=2, allow_nan=False)+'\n', encoding='utf-8')
    pending.replace(path)


def normalize(source, destination, settings=Settings(), *, dry_run=False, timing_only=False, progress=print):
    """Measure, then write ``normalized.wav`` (8 kHz, 16-bit, dithered) and ``normalization.json``."""
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if (destination == source.parent or source.parent in destination.parents
            or destination in source.parents):
        raise ValueError('Input and output must be separate non-overlapping locations')
    if destination.exists() and any(destination.iterdir()):
        raise ValueError('Output must be new or empty; existing audio is immutable')
    input_binding = signature(source)
    sidecars = {}
    for name in ('denoised.json', 'review.json', 'retime.json', 'markers.csv',
                 'timed_transcript.tsv', 'timed_transcript.json'):
        path = source.parent/name
        if path.is_file():
            sidecars[name] = path.read_bytes()
    record = dict(schema=2, stage='08', status='planned', input=input_binding,
                  implementation_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                  source_metadata={name:hashlib.sha256(raw).hexdigest() for name, raw in sidecars.items()},
                  delivery=dict(sample_rate=DELIVERY_RATE, subtype=DELIVERY_SUBTYPE, container='WAV',
                                full_scale=FULL_SCALE, peak_ceiling_dbfs=settings.peak_ceiling_dbfs),
                  duration_and_offsets_preserved=True, listening_approved=False)
    if dry_run:
        if input_binding['sample_rate'] != DELIVERY_RATE:
            record['resampling'] = dict(RESAMPLER, input_rate=input_binding['sample_rate'], output_rate=DELIVERY_RATE,
                                        note='dry run measured the input rate; the run measures the resampled audio')
        record['analysis'] = analyze(source, settings, timing_only=timing_only, progress=progress)
        return record
    # At the delivery rate, measure before creating the destination (a bad input leaves nothing behind).
    measured = (analyze(source, settings, timing_only=timing_only, progress=progress)
                if input_binding['sample_rate'] == DELIVERY_RATE else None)
    with planning_lock(destination):
        if destination.exists() and any(destination.iterdir()):
            raise ValueError('Output must be new or empty')
        destination.mkdir(parents=True, exist_ok=True)
        manifest = destination/'normalization.json'
        record['status'] = 'running'
        write_json(manifest, record)
        resampled = destination/'resampled.partial.wav'
        try:
            # Resample first, so the measurement, the headroom and the limiter all see the delivered samples.
            work = source
            if input_binding['sample_rate'] != DELIVERY_RATE:
                record['resampling'] = resample_file(source, resampled, progress=progress)
                if signature(source) != input_binding:
                    raise ValueError('Source changed during resampling')
                work = resampled
            else:
                record['resampling'] = dict(applied=False, reason=f'input is already {DELIVERY_RATE} Hz')
            analysis = measured or analyze(work, settings, timing_only=timing_only, progress=progress)
            record['analysis'] = analysis
            binding = analysis['source']
            rate, frames = binding['sample_rate'], binding['frames']
            seed = int(binding['decoded_float32_sha256'][:16], 16)
            rng = np.random.Generator(np.random.PCG64(seed))
            record['dither'] = dict(DITHER, seed=seed)
            ceiling = 10**(settings.peak_ceiling_dbfs/20)
            halo = 2*max(1, round(rate*.005))
            pending = destination/'normalized.partial.wav'
            in_hash, out_hash = hashlib.sha256(), hashlib.sha256()
            changed, min_gain, peak = 0, 1., 0.
            protected_limited_samples = 0
            limited = analysis.get('headroom') is not None
            with sf.SoundFile(work) as audio, sf.SoundFile(pending, 'w', samplerate=rate, channels=1,
                    subtype=DELIVERY_SUBTYPE, format='RF64' if frames*2 >= 0xffffff00 else 'WAV') as output:
                for start in range(0, frames, rate*30):
                    end = min(frames, start+rate*30)
                    left, right = max(0, start-halo), min(frames, end+halo)
                    audio.seek(left)
                    x = audio.read(right-left, dtype='float32')
                    if len(x) != right-left or not np.isfinite(x).all():
                        raise ValueError('Source changed or became non-finite')
                    crop = slice(start-left, end-left)
                    in_hash.update(x[crop].astype('<f4', copy=False).tobytes())
                    y = x.astype(np.float64)*analysis['gain']
                    if limited:
                        y, reduction = limit_peaks(y, rate, ceiling)
                        altered = reduction[crop] < 1.-1e-7
                        allowed = np.zeros(end-start, dtype=bool)
                        for a, b in analysis['headroom']['isolated_impulse_intervals']:
                            a, b = max(start, a-halo), min(end, b+halo)
                            if b > a:
                                allowed[a-start:b-start] = True
                        protected_limited_samples += int(np.count_nonzero(altered & ~allowed))
                        if protected_limited_samples:
                            raise ValueError('Headroom verification failed: limiting would alter protected audio')
                        changed += int(np.count_nonzero(altered))
                        min_gain = min(min_gain, float(reduction[crop].min()))
                    codes = dither_and_quantize(y[crop], rng)
                    output.write(codes)
                    out_hash.update(codes.tobytes())
                    peak = max(peak, float(np.max(np.abs(codes.astype(np.int32)), initial=0))/FULL_SCALE)
            expected = {k:v for k,v in binding.items() if k != 'decoded_float32_sha256'}
            if signature(work) != expected or in_hash.hexdigest() != binding['decoded_float32_sha256']:
                raise ValueError('Source changed between analysis and rendering')
            if work != source and signature(source) != input_binding:
                raise ValueError('Source changed between analysis and rendering')
            verified = hashlib.sha256()
            with sf.SoundFile(pending) as audio:
                if (audio.frames, audio.samplerate, audio.channels, audio.subtype) != (frames, rate, 1, DELIVERY_SUBTYPE):
                    raise ValueError('Output format or duration mismatch')
                for x in audio.blocks(blocksize=rate*30, dtype='int16'):
                    verified.update(x.astype('<i2', copy=False).tobytes())
            if verified.hexdigest() != out_hash.hexdigest():
                raise ValueError('Output verification failed')
            # The dither may add at most 1 LSB to a sample held at the ceiling.
            if peak > ceiling+1.5/FULL_SCALE:
                raise ValueError('Output exceeded peak ceiling')
            for name, raw in sidecars.items():
                if (source.parent/name).read_bytes() != raw:
                    raise ValueError('Source timing metadata changed')
                # Markers and the timed transcript keep their coordinates (seconds; resampling keeps the
                # duration). The source stage manifest is kept under a distinct name.
                keep = name in ('markers.csv', 'timed_transcript.tsv', 'timed_transcript.json')
                (destination/(name if keep else 'source_'+name)).write_bytes(raw)
            pending.replace(destination/'normalized.wav')
            resampled.unlink(missing_ok=True)
            record.update(status='complete', output=dict(file='normalized.wav', frames=frames, sample_rate=rate,
                subtype=DELIVERY_SUBTYPE, peak=peak, peak_dbfs=float(db(peak)), decoded_int16_sha256=verified.hexdigest()),
                limiter=dict(method='symmetric_5ms_peak_envelope', applied=limited,
                    affected_seconds=changed/rate, affected_fraction=changed/frames,
                    maximum_reduction_db=float(-db(min_gain)),
                    protected_limited_samples=protected_limited_samples,
                    restricted_to_isolated_impulses=True, review_recommended=False))
            write_json(manifest, record)
            return record
        except BaseException as error:
            resampled.unlink(missing_ok=True)
            record.update(status='failed', error=f'{type(error).__name__}: {error}')
            write_json(manifest, record)
            raise
