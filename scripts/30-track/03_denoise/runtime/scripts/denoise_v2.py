"""Persistent hum subtraction for original 8 kHz mono Apollo recordings."""
from dataclasses import asdict, dataclass
import hashlib

import numpy as np
from scipy import ndimage, signal


from denoise_v2_refinement import bridge_reference, line_profile, residual_refinement, span_mask, suppress_persistent_shifted_lines
from denoise_v2_refinement import (COMPACT_HALF_SECONDS, apply_compact_union, compact_bank,
                                   suppress_persistent_low_residual)


ALGORITHM = 'apollo15-hum-subtraction-v2.16'


@dataclass(frozen=True)
class Settings:
    sample_rate: int = 8000
    step_seconds: int = 64
    filter_guard_seconds: int = 4
    quiet_percentile: float = 35
    reference_hz: float = 1
    low_envelope_hz: float = 4
    minimum_coherence: float = 1.2
    bridge_seconds: float = 1.25
    residual_passes: int = 2
    residual_candidate_limit: int = 24


SETTINGS = Settings()


def rms_db(power):
    return float(10 * np.log10(max(power, 1e-30)))


def line_evidence(samples, sr):
    """Occupancy and prominence within 1 Hz of the 60 Hz grid."""
    if sr != 8000 or len(samples) < 2*sr:
        raise ValueError('Line evidence requires at least two seconds at 8 kHz')
    freq, _, power = signal.spectrogram(
        samples, sr, nperseg=2*sr, noverlap=sr, window='hann')
    centers = np.arange(1, 66) * 120
    nearby = np.array([-16, -14, -12, -10, -8, -6, 6, 8, 10, 12, 14, 16])
    local = np.median(power[centers[:, None] + nearby], axis=1)
    narrow = power[centers[:, None] + np.arange(-2, 3)]
    peak = narrow.max(axis=1)
    contrast = 10*np.log10((peak + 1e-30)/(local + 1e-30))
    occupancy = np.mean(contrast >= 10, axis=1)
    prominence = np.median(contrast, axis=1)
    strength = np.median(peak, axis=1)
    return freq, power, occupancy, prominence, strength


def robust_mean(z):
    center = np.median(z.real) + 1j*np.median(z.imag)
    for _ in range(6):
        distance = abs(z-center)
        scale = max(float(np.median(distance)), 1e-12)
        weights = np.minimum(1., 1.5*scale/(distance+1e-30))
        center = np.sum(z*weights)/np.sum(weights)
    return center


def rising_cosine(fraction):
    return .5 - .5*np.cos(np.pi*np.clip(fraction, 0, 1))


def observations(samples, oscillator, starts, window):
    demod = 2 * samples * oscillator.conj()
    frames = np.lib.stride_tricks.sliding_window_view(demod, len(window))
    return np.einsum('ij,j->i', frames[::starts[1]-starts[0]], window)


def envelope(samples, oscillator, cutoff, sr, order):
    lp = signal.butter(order, cutoff, fs=sr, output='sos')
    return signal.sosfiltfilt(lp, 2*samples*oscillator.conj())


def validated_fit(z, training, validation):
    coef = robust_mean(z[training])
    scatter = np.median(abs(z[training]-coef))
    coherence = float(abs(coef)/(scatter+1e-30))
    before = np.median(abs(z[validation])**2)
    after = np.median(abs(z[validation]-coef)**2)
    improvement = rms_db(before) - rms_db(after)
    return coef, coherence, improvement


def component(kind, hz, coef, coherence, improvement):
    return dict(kind=kind, hz=float(hz), coefficient_real=float(coef.real),
                coefficient_imag=float(coef.imag), coherence=coherence,
                validation_improvement_db=improvement)


def stationary_shifted_comb(samples, model, time, sr, starts, centers, window,
                            training, validation, settings, family=None):
    """Fit residual lines with a stable, nonzero common shift from mains."""
    size = min(32*sr, len(samples)//4)
    if size < 12*sr or min(training.sum(), validation.sum()) < 12:
        return []
    taper = np.hanning(size)
    offsets = np.linspace(0, len(samples)-size, 4, dtype=int)
    residual = samples-model
    original_power = np.array([abs(np.fft.rfft(samples[a:a+size]*taper))**2 for a in offsets])
    residual_power = np.array([abs(np.fft.rfft(residual[a:a+size]*taper))**2 for a in offsets])
    bin_hz = sr/size
    groups = []
    for k in range(10, 66):
        nom = 60*k
        indices = np.arange(round((nom-2.5)/bin_hz), round((nom+2.5)/bin_hz)+1)
        sides = np.r_[np.arange(indices[0]-round(3/bin_hz), indices[0]),
                      np.arange(indices[-1]+1, indices[-1]+round(3/bin_hz)+1)]
        peak = indices[np.argmax(original_power[:,indices],axis=1)]
        f = peak*bin_hz
        center = float(np.median(f))
        if np.max(abs(f-center)) > max(.11,3*bin_hz):
            continue
        floor = np.median(original_power[:,sides],axis=1)
        value = original_power[np.arange(len(offsets)),peak]
        if np.median(value/(floor+1e-30)) < 30:
            continue
        change = (center-nom)/k
        if not .012 <= abs(change) <= .04:
            continue
        remaining = residual_power[np.arange(len(offsets)),peak]
        if np.median(remaining/(floor+1e-30)) >= 8:
            groups.append((k,change,center,float(np.median(remaining)),float(np.median(value))))
    if len(groups) < 5:
        return []
    anchor = max(groups, key=lambda item:sum(abs(r[1]-item[1]) <= .0018 for r in groups))
    supported = [r for r in groups if abs(r[1]-anchor[1]) <= .0018]
    if len(supported) < 5 or np.ptp([r[0] for r in supported]) < 8:
        return []

    if family is not None:
        family['shift_hz'] = float(np.median([row[1] for row in supported]))
    # If the comb is drifting, use a measured upper carrier at the actual
    # offset. Restrict the alternate to this spectrally established family.
    phase_ref = None
    phase_gate = np.ones(len(samples))
    references = sorted(supported, key=lambda r:r[4]*r[0]**2, reverse=True)
    for ref_k, _, ref_hz, _, _ in references[:4]:
        if ref_k < 20:
            continue
        carrier = np.exp(2j*np.pi*ref_hz*time)
        ref = envelope(samples, carrier, .65, sr, 4)
        amp = abs(ref)
        nominal = np.median(amp)
        rate = np.r_[0., np.angle(ref[1:]*ref[:-1].conj())]*sr/(2*np.pi)
        valid = (amp > .30*nominal) & (amp < 2.8*nominal) & (abs(rate) < .8)
        if np.mean(valid) < .86:
            continue
        phi, valid, _ = bridge_reference(ref, valid, sr, settings.bridge_seconds)
        phase_ref = (2*np.pi*ref_hz*time+phi)/ref_k
        phase_gate = rising_cosine(ndimage.uniform_filter1d(
            valid.astype(float), size=sr, mode='nearest'))
        break

    records = []
    for k, _, center, _, _ in sorted(supported, key=lambda r:r[3], reverse=True):
        nominal = np.exp(2j*np.pi*center*time)
        z = observations(samples-model, nominal, starts, window)
        grid = np.linspace(-.085,.085,101)
        rotation = np.exp(-2j*np.pi*grid[:,None]*centers)
        score = abs(np.mean(rotation[:,training]*z[training],axis=1))
        best = int(np.argmax(score))
        hz = center+grid[best]
        trials = [(np.exp(2j*np.pi*hz*time), 1., 'stationary')]
        if phase_ref is not None:
            trials.append((np.exp(1j*k*phase_ref), phase_gate, 'tracked_comb'))
        best_fit = None
        for osc, phase_support, kind in trials:
            z = observations(samples-model, osc, starts, window)
            coef, coherence, gain = validated_fit(z, training, validation)
            if coherence < settings.minimum_coherence or gain < 1.5:
                continue
            raw = observations(samples, osc, starts, window)
            absence = (abs(raw) < .28*abs(coef)) & (abs(z) < .28*abs(coef))
            absence = ndimage.minimum_filter1d(absence,size=7,mode='nearest')
            visible = ndimage.uniform_filter1d((~absence).astype(float),size=7,mode='nearest')
            gate = np.interp(time,centers,rising_cosine(visible))
            applied = np.real(coef*osc)*gate*phase_support
            change = observations(applied, osc, starts, window)
            before = abs(z[validation])**2
            after = abs((z-change)[validation])**2
            improve = np.mean(after < before)
            if improve < .65 or np.percentile(after,90) > 1.08*np.percentile(before,90):
                continue
            if best_fit is None or gain > best_fit[0]:
                best_fit = gain, coef, coherence, applied, kind, improve
        if best_fit is None:
            continue
        gain, coef, coherence, applied, kind, improve = best_fit
        model += applied
        records.append(dict(component('shifted_comb', hz, coef, coherence, gain),
                            harmonic=k, shared_fundamental_offset_hz=float(anchor[1]),
                            phase_kind=kind, validation_improved_fraction=float(improve)))
    return records


def model_window(samples, seeds=(), settings=SETTINGS, defer_compact=False):
    """Estimate a hum waveform, with separate evidence for each local model."""
    sr = settings.sample_rate
    if sr != 8000:
        raise ValueError('Expected 8 kHz original audio')
    model = np.zeros(len(samples))
    record = dict(components=[], references_tried=[])
    if len(samples) < 16*sr:
        return model, dict(record, bypass='insufficient_context')
    freq, power, occupancy, prominence, strength = line_evidence(samples, sr)
    supported = (occupancy >= .70) & (prominence >= 13) & (strength >= 1e-13)
    reference_candidates = np.flatnonzero(supported & (np.arange(1, 66) >= 3))


    time = np.arange(len(samples))/sr
    n, hop = sr//2, sr//4
    starts = np.arange(0, len(samples)-n+1, hop)
    centers = (starts+n//2)/sr
    window = np.hanning(n)
    window /= window.sum()
    speech = signal.sosfilt(signal.butter(3, [350, 2500], btype='bandpass',
                                          fs=sr, output='sos'), samples)
    levels = np.mean(np.lib.stride_tricks.sliding_window_view(speech, n)[starts]**2, axis=1)
    quiet = levels <= np.percentile(levels, settings.quiet_percentile)
    interior = (centers % 2 >= .25) & (centers % 2 <= 1.75)
    training = quiet & interior & ((centers//2) % 2 == 0)
    validation = quiet & interior & ((centers//2) % 2 == 1)
    record.update(training_frames=int(training.sum()), validation_frames=int(validation.sum()))
    fit_allowed = min(training.sum(), validation.sum()) >= 12

    # Stronger, higher carriers convey more phase resolution for lower harmonics.
    order = np.argsort(strength[reference_candidates] * (reference_candidates+1)**2)
    ranked = reference_candidates[order[::-1]]
    # Prefer a supported carrier above the radio speech band. Use the
    # remaining candidates if a high reference lacks phase agreement.
    candidates = ([(index, position) for group in (ranked[ranked >= 49], ranked[ranked < 49])
                   for position, index in enumerate(group[:3])]
                  if fit_allowed and supported.sum() >= 3 and len(ranked) >= 2 else ())
    references = []
    for index, position in candidates:
        k = int(index+1)
        hz = 60*k
        carrier = np.exp(2j*np.pi*hz*time)
        ref = envelope(samples, carrier, settings.reference_hz, sr, 4)
        amplitude = abs(ref)
        nominal = float(np.median(amplitude[starts[quiet]+n//2]))
        phase_rate = np.angle(ref[1:] * ref[:-1].conj())*sr/(2*np.pi)
        phase_rate = np.r_[phase_rate[0], phase_rate]
        mask = (amplitude >= .35*nominal) & (amplitude <= 2.5*nominal) & (abs(phase_rate) <= 1.25)
        confidence = float(np.mean(mask[starts[quiet]+n//2]))
        # Give the preferred above-speech-band reference a modest preference
        # where a second track offers comparable continuity.
        # A tiny change in activity should not replace a stronger reference
        # with a comparably continuous weaker one.
        strength_priority = .01*(2-position)
        rank_score = float(mask.mean()) + (.07 if hz >= 3000 else 0.) + strength_priority
        references.append((rank_score, k, ref, mask, nominal, confidence))
    related = []
    phase = None
    for _, k, ref, mask, nominal, confidence in sorted(references, key=lambda v:v[0], reverse=True):
        hz = 60*k
        if confidence < .78:
            continue
        ref_phase, mask, bridged = bridge_reference(ref, mask, sr, settings.bridge_seconds)
        trial_phase = (2*np.pi*hz*time+ref_phase)/k
        related = []
        for other in ranked[:16]:
            if other == k-1:
                continue
            j = int(other+1)
            z = observations(samples, np.exp(1j*j*trial_phase), starts, window)
            coef, coherence, improvement = validated_fit(z, training, validation)
            if coherence >= settings.minimum_coherence and improvement >= 3:
                related.append((j, coef, coherence, improvement, z))
        record['references_tried'].append(dict(hz=hz, quiet_valid_fraction=confidence,
                                               valid_fraction=float(mask.mean()),
                                               occupancy=float(occupancy[k-1]),
                                               prominence_db=float(prominence[k-1]),
                                               related_harmonics=[r[0] for r in related]))
        if related:
            phase = trial_phase
            break
    if phase is not None:
        # Broaden the bad-reference guard by half a second. Ramp smoothly back to
        # full subtraction, even when the reference changes abruptly.
        valid = ndimage.minimum_filter1d(mask, size=sr, mode='nearest')
        gate = ndimage.uniform_filter1d(valid.astype(float), size=sr//2, mode='nearest')
        gate = rising_cosine(gate)
        bad = np.flatnonzero(np.diff(np.r_[False, gate < .999, False]))
        off = np.flatnonzero(np.diff(np.r_[False, gate <= 1e-12, False]))
        record.update(reference_hz=hz, reference_bridged_intervals_frames=bridged, reference_nominal_amplitude=nominal,
                      reference_taper_intervals_frames=bad.reshape(-1, 2).tolist(),
                      reference_off_intervals_frames=off.reshape(-1, 2).tolist())
        related = {r[0]: r[1:] for r in related}
        for j in range(3, 66):
            osc = np.exp(1j*j*phase)
            fit = related.get(j)
            if fit is None:
                z = observations(samples, osc, starts, window)
                fit = (*validated_fit(z, training, validation), z)
            coef, coherence, improvement, observed = fit
            if coherence >= settings.minimum_coherence and improvement >= 3:
                # Reject sustained loss of a component even when the shared
                # reference still exists. Brief spectral masking won't turn
                # off an otherwise coherent line.
                n_low = ndimage.uniform_filter1d(
                    (abs(observed) < .25*abs(coef)).astype(float), size=7, mode='nearest')
                presence = rising_cosine((1-n_low)/.55)
                component_gate = np.interp(time, centers, presence)
                model += gate * component_gate * np.real(coef*osc)
                intervals = span_mask(presence < .999)
                record['components'].append(dict(
                    component('harmonic', 60*j, coef, coherence, improvement),
                    local_taper_intervals_seconds=[
                        [float(centers[a]), float(centers[b-1])] for a,b in intervals]))

    else:
        record['upper_bypass'] = 'no_coherent_reference'

    # Low hum is supported independently of the upper carrier. A sustained
    # sharp 60 Hz line can stand on its own. 120 Hz also requires 60 Hz or
    # coherent upper-comb evidence to avoid following a voiced fundamental.
    low_supported = False
    for j in (1, 2):
        hz = 60*j
        offsets = np.arange(-5, 6) if j == 2 else np.arange(-2, 3)
        line = power[2*hz+offsets].max(axis=0)
        _, floor, _ = line_profile(power, .5, hz)
        contrast = 10*np.log10((line+1e-30)/(floor+1e-30))
        persist = float(np.mean(contrast >= (15 if j == 1 else 11)))
        if persist < (.70 if j == 1 else .50) or (j == 2 and not low_supported and phase is None):
            continue
        if np.median(line) < 1e-13:
            continue
        osc = np.exp(2j*np.pi*hz*time)
        env = envelope(samples, osc, settings.low_envelope_hz, sr, 3)
        cap = 1.65*float(np.percentile(abs(env[starts+n//2]), 95))
        # A wide envelope follows low sidebands only where the local line
        # remains demonstrably above the surrounding floor.
        threshold = 9 if j == 1 else 6
        established = float(np.percentile(line[contrast >= threshold+4], 35))
        relative = line/max(established, 1e-30)
        # Speech raises the neighboring floor. A still-strong projection is
        # permission to apply the established low model, not to learn noise.
        local = rising_cosine((relative-.015)/.18)
        local = ndimage.gaussian_filter1d(local, sigma=.75, mode='nearest', truncate=2)
        low_gate = np.interp(time, 1+np.arange(len(local)), local)
        # Leave the projection of the neighboring floor in place. This avoids
        # chasing a nearly inaudible line solely because its phase is known.
        amplitude = abs(env)
        local_floor = ndimage.percentile_filter(np.sqrt(3*floor), 30, size=13, mode='nearest')
        floor_fraction = .18 if j == 1 else .30
        floor_amp = floor_fraction*np.interp(time, 1+np.arange(len(local_floor)), local_floor)
        subtract = np.maximum(0., np.minimum(amplitude, cap)-floor_amp)
        model += low_gate * np.real(env * (subtract/(amplitude+1e-30)) * osc)
        low_supported = True
        record['components'].append(dict(kind='low_envelope', hz=hz, cap=cap,
                                         occupancy=persist, confidence_mean=float(low_gate.mean()),
                                         source_local_support_db=float(np.median(contrast))))

    # Saved detections are hints only. Fit a single oscillator to quieter
    # context, then require that it improves separate quiet observations.
    for seed in sorted(set(seeds)):
        if not fit_allowed or not 250 < seed < freq[-1]-5 or abs(seed-60*round(seed/60)) <= 1.5:
            continue
        osc = np.exp(2j*np.pi*seed*time)
        z = observations(samples-model, osc, starts, window)
        delta = np.linspace(-.9, .9, max(181, round(len(samples)/sr*4)+1))
        rotate = np.exp(-2j*np.pi*delta[:, None]*centers)
        # Coherent coarse grid across the full context. A robust estimate and
        # independent validation decide if the best frequency is usable.
        scores = abs(np.mean(rotate[:, training] * z[training], axis=1))
        best = int(np.argmax(scores))
        coef, coherence, improvement = validated_fit(z*rotate[best], training, validation)
        if coherence >= settings.minimum_coherence and improvement >= 3:
            response = abs(np.sum(window*np.exp(2j*np.pi*delta[best]*(np.arange(n)-n//2)/sr)))
            coef /= response
            hz = seed + delta[best]
            model += np.real(coef*np.exp(2j*np.pi*hz*time))
            record['components'].append(component('other_tone', hz, coef, coherence, improvement))
    family = {}
    if phase is not None or low_supported:
        record['components'].extend(stationary_shifted_comb(
            samples, model, time, sr, starts, centers, window, training, validation, settings, family))

    # Search the remaining waveform only near evidenced harmonics or saved
    # noncomb seeds. Each proposed contribution is validated against the
    # original minus the entire accepted model at that point.
    _, residual_power, _, _, _ = line_evidence(samples-model, sr)
    refine = [(60*int(i+1), 'harmonic') for i in np.argsort(strength)[::-1]
              if i >= 1 and (phase is not None or low_supported)
              and occupancy[i] >= .28 and np.percentile(
                  line_profile(power, .5, 60*int(i+1))[2], 75) >= 14]
    for seed in sorted(set(seeds)):
        if not 45 <= seed < freq[-1]-9 or abs(seed-60*round(seed/60)) <= 1.5:
            continue
        center = round(seed/.5)
        offsets = np.arange(-2, 3)
        best = center+offsets[int(np.argmax(np.median(power[center+offsets], axis=1)))]
        hz = float(freq[best])
        if abs(hz-60*round(hz/60)) <= 1.5 or any(abs(hz-v[0]) < 1.5 for v in refine):
            continue
        _, _, contrast = line_profile(power, .5, hz)
        if np.mean(contrast >= 15) >= .65:
            refine.append((hz, 'saved_noncomb'))
    ranked_residual = []
    record['residual_candidate_stops'] = []
    stationary = [c['hz'] for c in record['components'] if c['kind'] == 'other_tone']
    for hz, origin in refine:
        residual_line, _, residual_contrast = line_profile(residual_power, .5, hz)
        if origin == 'saved_noncomb' and any(abs(hz-other) < 1.5 for other in stationary):
            initial_line, _, original_contrast = line_profile(power, .5, hz)
            supported_frames = original_contrast >= 15
            persistent_residual = (residual_line >= .08*initial_line) & supported_frames
            if np.count_nonzero(persistent_residual) < .25*np.count_nonzero(supported_frames):
                record['residual_candidate_stops'].append(
                    dict(hz=hz, stop='stationary_fit_left_no_persistent_residual'))
                continue
        if np.mean(residual_contrast >= 7) >= .20:
            ranked_residual.append((float(np.percentile(residual_line, 75)), hz, origin))
    ranked_residual.sort(reverse=True)
    refine = [(hz, origin) for _, hz, origin in ranked_residual]
    record['residual_candidates_above_floor'] = len(refine)
    refinement_train = interior & ((centers//2) % 2 == 0)
    refinement_check = interior & ((centers//2) % 2 == 1)
    record['refinement'] = residual_refinement(
        samples, model, time, sr, refine[:settings.residual_candidate_limit],
        starts, centers, window, levels, refinement_train, refinement_check,
        power, observations, rms_db, settings.residual_passes)
    compact = (suppress_persistent_shifted_lines(
        samples, model, time, sr, family.get('shift_hz'), apply_model=not defer_compact)
               if phase is not None or low_supported else [])
    if low_supported:
        corroborated = {round(c['hz']/60) for c in record['components']
                        if c['kind'] == 'harmonic' and c['coherence'] >= 2.5
                        and c['validation_improvement_db'] >= 8}
        compact.extend(suppress_persistent_low_residual(
            samples, model, time, sr, apply_model=not defer_compact,
            corroborated=corroborated))
    record['components'].extend(compact)
    if compact and defer_compact:
        record['compact_application'] = 'union_after_baseline_blend'
    if family:
        record['shifted_family_hz'] = family['shift_hz']
    if not record['components'] and not any(row.get('passes') for row in record['refinement']):
        record['bypass'] = 'no_supported_components'
    return model, record


def corrected_chunks(read, source_frames, start, end, seeds=None, settings=SETTINGS):
    """Yield (absolute frame, original, corrected, left model, right model).

    read(start, count) is a finite mono random access reader. Window anchors
    live on the source frame grid, independent of the requested crop.
    """
    if not 0 <= start < end <= source_frames:
        raise ValueError('Requested interval is outside the source')
    if settings.sample_rate != 8000 or settings.step_seconds < 16 or settings.filter_guard_seconds < 4:
        raise ValueError('Expected an 8 kHz source and adequate model context')
    step = settings.step_seconds * settings.sample_rate
    guard = settings.filter_guard_seconds * settings.sample_rate

    def estimate(center):
        a = max(0, center-step-guard)
        b = min(source_frames, center+step+guard)
        x = read(a, b-a)
        if x.shape != (b-a,) or not np.isfinite(x).all():
            raise ValueError('Expected finite original mono samples')
        prediction, record = model_window(x, seeds(a, b) if seeds else (), settings, defer_compact=True)
        return prediction, dict(record, anchor_frame=center, context_frames=[a, b],
                                context_float64_sha256=hashlib.sha256(x.astype('<f8', copy=False)).hexdigest())

    center = start//step * step
    left, left_record = estimate(center)
    while center < end:
        right, right_record = estimate(center+step)
        a, b = max(start, center), min(end, center+step)
        use_compact = compact_bank(left_record) or compact_bank(right_record)
        extension = round(COMPACT_HALF_SECONDS*settings.sample_rate) if use_compact else 0
        # Use the same convolution input for full and arbitrary crop renders.
        lo, hi = ((max(0,center-extension),min(source_frames,center+step+extension))
                  if use_compact else (a,b))
        x = read(lo, hi-lo)
        frames = np.arange(lo, hi)
        weight = rising_cosine((frames-center)/step)
        left_start = lo-left_record['context_frames'][0]
        right_start = lo-right_record['context_frames'][0]
        baseline = ((1-weight)*left[left_start:left_start+hi-lo]
                    + weight*right[right_start:right_start+hi-lo])
        corrected = x-baseline
        if use_compact:
            corrected -= apply_compact_union(
                corrected, frames, settings.sample_rate, center, step,
                left_record, right_record, rising_cosine)
        crop = slice(a-lo,b-lo)
        yield a, x[crop], corrected[crop], left_record, right_record
        center += step
        left, left_record = right, right_record


def configuration():
    return asdict(SETTINGS)



