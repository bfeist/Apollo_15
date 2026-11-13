"""Conservative speech-level measurement and separate, constant-gain listening renders.

This is a voiced-speech activity heuristic, not P.56, LUFS, or a transcription
model. Only sustained, harmonic, non-tonal activity above the estimated floor
can request gain. Uncertain channels are deliberately left at their input level.
"""
from dataclasses import asdict, dataclass
import hashlib
import json
import math
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy.ndimage import maximum_filter1d, uniform_filter1d

from flutter_plan_store import planning_lock

DEFAULT_NORMALIZED_ROOT = Path('D:/Apollo_15_30-Track/speech_normalized')
METHOD = 'conservative_voiced_speech_rms_v4'
FRAME_SECONDS = .02


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
        return result
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
        return result
    level = float(np.median(db(features[mask, 0])))
    contrast = level-noise if noise is not None else None
    result.update(candidate_level_dbfs=level, candidate_contrast_db=contrast)
    # A speech-band margin alone can admit weak periodic background features.
    # Also require separation from the full-band background that gain would
    # amplify. Confirmed empty T510077 CH01/CH02 exposed this failure mode.
    if contrast is None or contrast < settings.min_snr_db:
        result['reason'] = 'insufficient_speech_contrast'
        return result
    result.update(speech_seconds=seconds, speech_intervals=result['candidate_intervals'])
    # Stage 07 is independent of the old denoise gain policy. Use one scalar,
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
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if (destination == source.parent or source.parent in destination.parents
            or destination in source.parents):
        raise ValueError('Input and output must be separate non-overlapping locations')
    if destination.exists() and any(destination.iterdir()):
        raise ValueError('Output must be new or empty; existing audio is immutable')
    analysis = analyze(source, settings, timing_only=timing_only, progress=progress)
    sidecars = {}
    for name in ('denoised.json', 'review.json', 'retime.json', 'markers.csv'):
        path = source.parent/name
        if path.is_file():
            sidecars[name] = path.read_bytes()
    record = dict(schema=1, stage='07', status='planned', analysis=analysis,
                  implementation_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                  source_metadata={name:hashlib.sha256(raw).hexdigest() for name, raw in sidecars.items()},
                  duration_and_offsets_preserved=True, listening_approved=False)
    if dry_run:
        return record
    with planning_lock(destination):
        if destination.exists() and any(destination.iterdir()):
            raise ValueError('Output must be new or empty')
        destination.mkdir(parents=True, exist_ok=True)
        manifest = destination/'normalization.json'
        record['status'] = 'running'
        write_json(manifest, record)
        try:
            binding = analysis['source']
            rate, frames = binding['sample_rate'], binding['frames']
            halo = 2*max(1, round(rate*.005))
            pending = destination/'normalized.partial.wav'
            in_hash, out_hash = hashlib.sha256(), hashlib.sha256()
            changed, min_gain, peak = 0, 1., 0.
            protected_limited_samples = 0
            eligible = analysis['speech_level_dbfs'] is not None
            with sf.SoundFile(source) as audio, sf.SoundFile(pending, 'w', samplerate=rate,
                    channels=1, subtype='FLOAT', format='RF64' if frames*4 >= 0xffffff00 else 'WAV') as output:
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
                    if eligible:
                        y, reduction = limit_peaks(y, rate, 10**(settings.peak_ceiling_dbfs/20))
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
                    y = y[crop].astype('<f4')
                    output.write(y)
                    out_hash.update(y.tobytes())
                    peak = max(peak, float(np.max(np.abs(y), initial=0)))
            expected = {k:v for k,v in binding.items() if k != 'decoded_float32_sha256'}
            if signature(source) != expected or in_hash.hexdigest() != binding['decoded_float32_sha256']:
                raise ValueError('Source changed between analysis and rendering')
            verified = hashlib.sha256()
            with sf.SoundFile(pending) as audio:
                if (audio.frames, audio.samplerate, audio.channels, audio.subtype) != (frames, rate, 1, 'FLOAT'):
                    raise ValueError('Output format or duration mismatch')
                for x in audio.blocks(blocksize=rate*30, dtype='float32'):
                    verified.update(x.astype('<f4', copy=False).tobytes())
            if verified.hexdigest() != out_hash.hexdigest():
                raise ValueError('Output verification failed')
            if eligible and peak > 10**(settings.peak_ceiling_dbfs/20)+1e-7:
                raise ValueError('Output exceeded peak ceiling')
            for name, raw in sidecars.items():
                if (source.parent/name).read_bytes() != raw:
                    raise ValueError('Source timing metadata changed')
                # Markers keep the same coordinates. Preserve the source stage
                # manifest under a distinct name rather than posing as its output.
                output_name = name if name == 'markers.csv' else 'source_'+name
                (destination/output_name).write_bytes(raw)
            pending.replace(destination/'normalized.wav')
            record.update(status='complete', output=dict(file='normalized.wav', frames=frames,
                sample_rate=rate, peak=peak, decoded_float32_sha256=verified.hexdigest()),
                limiter=dict(method='symmetric_5ms_peak_envelope', applied=eligible,
                    affected_seconds=changed/rate, affected_fraction=changed/frames,
                    maximum_reduction_db=float(-db(min_gain)),
                    protected_limited_samples=protected_limited_samples,
                    restricted_to_isolated_impulses=True, review_recommended=False))
            write_json(manifest, record)
            return record
        except BaseException as error:
            record.update(status='failed', error=f'{type(error).__name__}: {error}')
            write_json(manifest, record)
            raise
