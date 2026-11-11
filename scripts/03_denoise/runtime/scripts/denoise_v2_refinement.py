"""Tracked residual checks for the Apollo hum-subtraction model."""
import numpy as np
from scipy import ndimage, signal


def line_profile(power, bin_width, hz):
    center = round(hz/bin_width)
    narrow = power[center-2:center+3].max(axis=0)
    side = np.r_[np.arange(center-16, center-5), np.arange(center+6, center+17)]
    floor = np.median(power[side], axis=0)
    return narrow, floor, 10*np.log10((narrow+1e-30)/(floor+1e-30))


def span_mask(mask):
    return np.flatnonzero(np.diff(np.r_[False, mask, False])).reshape(-1, 2)


def bridge_reference(ref, mask, sr, max_seconds):
    """Bridge brief bad reference spans only where good endpoints agree."""
    phase = np.unwrap(np.angle(ref))
    bridged = []
    slope_frames = sr//8
    for a, b in span_mask(~mask):
        if b-a > max_seconds*sr or a < slope_frames or b+slope_frames >= len(ref):
            continue
        if not mask[a-slope_frames:a].all() or not mask[b:b+slope_frames].all():
            continue
        s0 = (phase[a-1]-phase[a-slope_frames])/(slope_frames-1)
        s1 = (phase[b+slope_frames-1]-phase[b])/(slope_frames-1)
        length = b-a+1
        predicted = phase[a-1]+.5*(s0+s1)*length
        difference = phase[b]-predicted
        slip = 2*np.pi*np.rint(difference/(2*np.pi))
        if abs(difference-slip) > .55 or abs(s0-s1)*length > 1.5:
            continue
        phase[b:] -= slip
        u = np.arange(1, length)/length
        # Hermite interpolation matches both endpoint phases and slopes.
        phase[a:b] = ((2*u**3-3*u*u+1)*phase[a-1]+(u**3-2*u*u+u)*length*s0
                      +(-2*u**3+3*u*u)*phase[b]+(u**3-u*u)*length*s1)
        mask[a:b] = True
        bridged.append([int(a), int(b)])
    return phase, mask, bridged


def supported_continuation(phase, valid, absent, step, seconds=16.):
    """Continue through masking only when the two observed trajectories agree."""
    apply = valid.copy()
    phase = phase.copy()
    for a, b in span_mask(~valid):
        if a < 3 or b+3 > len(valid) or (b-a)*step > seconds or absent[a:b].any():
            continue
        if not valid[a-3:a].all() or not valid[b:b+3].all():
            continue
        before = np.median(np.diff(phase[a-3:a]))
        after = np.median(np.diff(phase[b:b+3]))
        length = b-a+1
        predicted = phase[a-1]+.5*(before+after)*length
        slip = 2*np.pi*np.rint((phase[b]-predicted)/(2*np.pi))
        if abs(phase[b]-slip-predicted) > .65 or abs(before-after)*length > 1.6:
            continue
        phase[b:] -= slip
        phase[a:b] = np.linspace(phase[a-1], phase[b], length+1)[1:-1]
        apply[a:b] = True
    return phase, apply


def continue_coefficients(coefficients, fitted, groups, centers, apply, absent):
    """Fill missing updates between compatible fits, never across true absence."""
    continued = []
    known = np.flatnonzero(fitted)
    for left, right in zip(known, known[1:]):
        if right-left < 2 or groups[right]-groups[left] > 20:
            continue
        inside = (centers >= groups[left]) & (centers <= groups[right])
        a, b = coefficients[left], coefficients[right]
        if (not inside.any() or absent[inside].any() or np.mean(apply[inside]) < .80
                or abs(a-b) > .85*max(abs(a), abs(b))):
            continue
        coefficients[left+1:right] = np.linspace(a, b, right-left+1)[1:-1]
        continued.append([float(groups[left]), float(groups[right])])
    return continued


def smooth_track(time, centers, raw_observations, prominence, relative_floor):
    """Phase at the observed tone, with a guard against phase slips."""
    amp = abs(raw_observations)
    dphase = np.angle(raw_observations[1:]*raw_observations[:-1].conj())
    rate = np.r_[dphase[0], dphase]/(2*np.pi*(centers[1]-centers[0]))
    phase = np.unwrap(np.angle(raw_observations))
    valid = (prominence >= 10) & (amp >= 2.5*relative_floor) & (abs(rate) < 1.25)
    valid = ndimage.minimum_filter1d(valid, size=3, mode='nearest')
    nominal = np.median(amp[valid]) if valid.any() else 0.
    absent = (amp < .22*nominal) & (amp < 2.5*relative_floor)
    # A weak instantaneous observation can be destructive interference.
    absent = ndimage.minimum_filter1d(absent, size=5, mode='nearest')
    phase, apply = supported_continuation(phase, valid, absent, centers[1]-centers[0])
    smooth = ndimage.gaussian_filter1d(phase, sigma=1.25, mode='nearest')
    return np.interp(time, centers, smooth), rate, valid, apply, absent



def residual_refinement(samples, model, time, sr, candidates, starts, centers,
                        window, levels, training, validation, power, observations, rms_db,
                        max_passes):
    """Use short tracked phasor fits and keep only validated residual reductions."""
    records = []
    # Harmonic and saved off-grid candidates are individually gated. Strongest
    # original tones first so sidebands are assessed against the current sum.
    for hz, origin in candidates:
        _, floor, contrast = line_profile(power, .5, hz)
        if np.mean(contrast >= 12) < .28 or np.percentile(contrast, 75) < 14:
            continue
        spectrogram_centers = 1 + np.arange(power.shape[1])
        prominence = np.interp(centers, spectrogram_centers, contrast)
        # PSD -> approximate window-observation amplitude floor. Use the same
        # floor for validation and the local residual stopping threshold.
        amplitude_floor = np.sqrt(3*np.interp(centers, spectrogram_centers, floor))
        carrier = np.exp(2j*np.pi*hz*time)
        raw = observations(samples, carrier, starts, window)
        track, rate, valid, apply, absent = smooth_track(time, centers, raw, prominence, amplitude_floor)
        if np.mean(valid) < .25 or np.sum(valid & validation) < 12:
            records.append(dict(hz=hz, origin=origin, stop='unreliable_track'))
            continue
        oscillator = carrier*np.exp(1j*track)
        raw_track = observations(samples, oscillator, starts, window)
        original_level = abs(raw_track)
        original_median = float(np.median(original_level[valid & validation]))
        sample_guard = ndimage.uniform_filter1d((apply & ~absent).astype(float), size=3, mode='nearest')
        ramp = np.interp(time, centers, sample_guard)
        groups = np.arange(0., time[-1]+4, 4)
        accepted = []
        stop = 'pass_limit'
        for iteration in range(max_passes):
            residual = observations(samples-model, oscillator, starts, window)
            removed = raw_track-residual
            select = valid & validation
            pre = float(np.median(abs(residual[select])**2))
            local_coeffs = np.zeros(len(groups), dtype=complex)
            measured_zero = np.zeros(len(groups), dtype=bool)
            for index, center in enumerate(groups):
                local = valid & (abs(centers-center) <= 4)
                fit = local & training
                check = local & validation
                if min(fit.sum(), check.sum()) < 8:
                    continue
                # Select quieter observations separately on each side of the
                # split, so a louder late context is still represented.
                percentile = 60 if origin == 'harmonic' else 45
                fit &= levels <= np.percentile(levels[fit], percentile)
                check &= levels <= np.percentile(levels[check], percentile)
                # The residual can be out of phase if the first reference
                # had phase error. Both projections must pass separate checks.
                amount = np.median(residual[fit].real)+1j*np.median(residual[fit].imag)
                floor_level = float(np.median(amplitude_floor[fit]))
                if np.median(abs(residual[check])) <= 1.2*np.median(amplitude_floor[check]):
                    measured_zero[index] = True
                    continue
                scatter = float(np.median(abs(residual[fit]-amount)))
                threshold = 1.6 if origin == 'harmonic' else 2.5
                if abs(amount) <= max(threshold*floor_level, 1.25*scatter):
                    continue
                cap = .8*float(np.median(original_level[fit]))
                amount *= min(1., cap/(abs(amount)+1e-30))
                old = np.median(abs(residual[check])**2)
                new = np.median(abs(residual[check]-amount)**2)
                if new < (.84 if origin == 'harmonic' else .70)*old:
                    local_coeffs[index] = amount
            fitted = local_coeffs != 0
            if not fitted.any():
                stop = 'residual_near_floor_or_no_local_support'
                break

            continued = continue_coefficients(local_coeffs, fitted | measured_zero, groups, centers, apply, absent)
            proposal = ramp * np.interp(time, groups, local_coeffs)
            correction = np.real(proposal*oscillator)
            change = observations(correction, oscillator, starts, window)
            post = float(np.median(abs((residual-change)[select])**2))
            improved = abs(residual-change)**2 < abs(residual)**2
            # Improvement can be localized, e.g. in a previous gate outage.
            affected = select & (abs(change) > amplitude_floor)
            if not affected.any():
                stop = 'residual_near_floor'
                break
            before = abs(residual[affected])**2
            after = abs((residual-change)[affected])**2
            gain_db = rms_db(np.mean(before))-rms_db(np.mean(after))
            removed_after = removed+change
            excess = (abs(removed_after) > 1.15*original_level+3*amplitude_floor)
            excess &= abs(removed_after) > abs(removed)+amplitude_floor
            if (gain_db < 1.0 or np.mean(improved[affected]) < .65
                    or np.mean(excess[affected]) > .1
                    or post > 1.12*pre):
                stop = 'rollback_validation_or_oversubtraction'
                break
            model += correction
            accepted.append(dict(iteration=iteration+1, validation_improvement_db=gain_db,
                                 validation_frames=int(affected.sum()),
                                 input_dbfs=rms_db(np.mean(original_level[affected]**2)/2),
                                 residual_before_dbfs=rms_db(np.mean(before)/2),
                                 residual_after_dbfs=rms_db(np.mean(after)/2),
                                 removed_dbfs=rms_db(np.mean(abs(removed_after[affected])**2)/2),
                                 nearby_floor_dbfs=rms_db(np.mean(amplitude_floor[affected]**2)/2),
                                 held_intervals_seconds=continued,
                                 measured_zero_centers_seconds=groups[measured_zero].tolist(),
                                 coefficients_real=local_coeffs.real.tolist(),
                                 coefficients_imag=local_coeffs.imag.tolist()))
        records.append(dict(hz=hz, origin=origin, valid_fraction=float(np.mean(valid)),
                            application_phase_fraction=float(np.mean(apply)),
                            original_track_amplitude=original_median,
                            tracked_hz_p10_p90=(hz+np.percentile(rate[valid], [10, 90])).tolist(),
                            group_centers_seconds=groups.tolist() if accepted else [],
                            passes=accepted, stop=stop))
    return records


COMPACT_HALF_SECONDS = .05

def compact_line_projection(samples, oscillator, sr, half_seconds=COMPACT_HALF_SECONDS):
    """Finite, symmetric line projection with no response beyond its support."""
    half = round(sr*half_seconds)
    weights = np.blackman(2*half+1)
    weights /= weights.sum()
    return signal.fftconvolve(2*samples*oscillator.conj(), weights, mode='same')


def suppress_persistent_shifted_lines(samples, model, time, sr, shift,
                                      minimum_lines=5, apply_model=True):
    """Fixed short projection only at independently evidenced residual lines."""
    f, t, original_power = signal.spectrogram(samples, sr, nperseg=4*sr,
                                              noverlap=3*sr, window='hann')
    residual = samples-model
    _, _, residual_power = signal.spectrogram(residual, sr, nperseg=4*sr,
                                             noverlap=3*sr, window='hann')
    n = np.arange(len(t))
    if shift is None:
        # A drifting comb need not have one stationary frequency. Require
        # simultaneous agreement of many independently prominent harmonics.
        tracks = []
        for k in range(29,66):
            nominal = 60*k
            near = np.flatnonzero(abs(f-nominal)<=2.4)
            side = (abs(f-nominal)>=4)&(abs(f-nominal)<=10)
            peak = near[np.argmax(original_power[near],axis=0)]
            credible = original_power[peak,n] >= 8*np.median(original_power[side],axis=0)
            tracks.append(np.where(credible,(f[peak]-nominal)/k,np.nan))
        tracks = np.array(tracks)
        agree = np.sum(np.isfinite(tracks),axis=0) >= 8
        if agree.mean() < .60:
            return []
        medians = np.full(len(t),np.nan)
        medians[agree] = np.nanmedian(tracks[:,agree],axis=0)
        agrees = np.sum(abs(tracks-medians)<=.006,axis=0) >= 8
        if agrees.mean() < .60:
            return []
        anchors = medians[agrees]
        p10,p90 = np.percentile(anchors,[10,90])
        if p90-p10 < .012 and abs(np.median(anchors)) < .012:
            return []
        shift = np.interp(t,t[agrees],anchors)
        family = 'time_varying'
    else:
        family = 'stable_offset'
    candidates = []
    for k in range(13,66):
        nominal = 60*k
        expected = k*(60+shift)
        near = np.flatnonzero(abs(f-nominal) <= 3)
        side = (abs(f-nominal) >= 4) & (abs(f-nominal) <= 10)
        allowed = abs(f[near,None]-expected) <= .53
        scores = np.where(allowed, original_power[near], -1)
        peaks = near[np.argmax(scores,axis=0)]
        original = original_power[peaks,n]
        remaining = residual_power[peaks,n]
        original_floor = np.median(original_power[side],axis=0)
        residual_floor = np.median(residual_power[side],axis=0)
        stable = (original >= 10*original_floor) & (abs(f[peaks]-expected) <= .40)
        if np.mean(stable) < .53:
            continue
        excess = (remaining >= 5*residual_floor) & stable
        if np.sum(excess) < .43*np.sum(stable):
            continue
        if np.median(remaining[stable]) < .025*np.median(original[stable]):
            continue
        hz = float(np.median(f[peaks[stable]]))
        if np.median(remaining[excess]) < 1e-11:
            continue
        strength = float(np.median(remaining[excess]))
        remaining_fraction = min(1., float(np.median(remaining[stable])/np.median(original[stable])))
        priority = strength*(1+3*remaining_fraction)
        candidates.append((priority,strength,k,hz,stable,original))
    if len(candidates) < minimum_lines:
        return []
    records = []
    for _,strength,k,hz,stable,original in sorted(candidates,key=lambda r:r[0],reverse=True):
        # A dropout requires sustained absolute loss, independent of speech
        # contrast. Continuous FIR output otherwise has no phase/fit gate.
        nominal = float(np.median(original[stable]))
        absence = ndimage.minimum_filter1d(original < .04*nominal,size=3,mode='nearest')
        apply = np.interp(time,t,ndimage.uniform_filter1d(
            (~absence).astype(float),size=3,mode='nearest'))
        if apply_model:
            oscillator = np.exp(2j*np.pi*hz*time)
            env = compact_line_projection(residual,oscillator,sr)
            model += .95*apply*np.real(env*oscillator)
        records.append(dict(kind='compact_shifted_line',hz=hz,harmonic=k,
                            half_response_seconds=COMPACT_HALF_SECONDS,projection_gain=.95,
                            family=family,
                            original_support_fraction=float(np.mean(stable)),
                            residual_peak_power_per_hz=strength,
                            true_absence_intervals_seconds=[
                                [float(t[a]),float(t[b-1])] for a,b in span_mask(absence)]))
    return records


def suppress_persistent_low_residual(samples, model, time, sr, apply_model=True, corroborated=()):
    """Continue established low harmonics independently of the upper pilot."""
    freq, centers, original = signal.spectrogram(
        samples, sr, nperseg=4*sr, noverlap=3*sr, window='hann')
    residual = samples-model
    _, _, remaining = signal.spectrogram(
        residual, sr, nperseg=4*sr, noverlap=3*sr, window='hann')
    frames = np.arange(len(centers))
    records = []
    for harmonic in range(3,12):
        if harmonic >= 6 and (not records or harmonic not in corroborated):
            continue
        nominal_hz = 60*harmonic
        near = np.flatnonzero(abs(freq-nominal_hz) <= .75)
        sides = (abs(freq-nominal_hz) >= 2) & (abs(freq-nominal_hz) <= 10)
        peaks = near[np.argmax(original[near],axis=0)]
        source_line = original[peaks,frames]
        stable_hz = float(np.median(freq[peaks]))
        source_floor = np.median(original[sides],axis=0)
        source_support = (source_line >= 40*source_floor) & (abs(freq[peaks]-stable_hz) <= .25)
        if np.mean(source_support) < (.78 if harmonic <= 5 else .52):
            continue
        output_line = remaining[peaks,frames]
        output_floor = np.median(remaining[sides],axis=0)
        left_over = (source_support & (output_line >= 10*output_floor)
                     & (output_line >= .0225*source_line))
        overshoot = np.zeros_like(left_over)
        if harmonic >= 6:
            # A bad shared phase can add this already corroborated line even
            # where nearby speech masks the original local spectral contrast.
            ridge = abs(freq-stable_hz) <= .25
            source_band = original[ridge].sum(axis=0)
            output_band = remaining[ridge].sum(axis=0)
            overshoot = (output_band >= 1.35*source_band) & (output_band >= 8*output_floor)
        if np.sum(left_over) < max(4,.10*np.sum(source_support)) and np.sum(overshoot) < 4:
            continue
        nominal_power = float(np.median(source_line[source_support]))
        absence = ndimage.minimum_filter1d(source_line < .04*nominal_power,size=3,mode='nearest')
        if apply_model:
            oscillator = np.exp(2j*np.pi*stable_hz*time)
            env = compact_line_projection(residual,oscillator,sr)
            available = ndimage.uniform_filter1d((~absence).astype(float),size=3,mode='nearest')
            model += .985*np.interp(time,centers,available)*np.real(env*oscillator)
        records.append(dict(kind='compact_low_residual',harmonic=harmonic,hz=stable_hz,
                            half_response_seconds=COMPACT_HALF_SECONDS,projection_gain=.985,
                            original_support_fraction=float(np.mean(source_support)),
                            material_residual_fraction=float(np.mean(left_over)),
                            overshoot_frames=int(np.sum(overshoot)),
                            true_absence_intervals_seconds=[
                                [float(centers[a]),float(centers[b-1])] for a,b in span_mask(absence)]))
    return records


def compact_bank(record):
    return {row['harmonic']: row for row in record['components']
            if row['kind'] in {'compact_shifted_line','compact_low_residual'}}


def apply_compact_union(residual, frames, sr, center, step, left, right, cosine):
    """Apply one compact bank to the blended baseline in an anchor interval."""
    left_bank, right_bank = compact_bank(left), compact_bank(right)
    position = (frames-center)/step
    taper = min(6*sr, step//4)
    correction = np.zeros(len(residual))
    for harmonic in sorted(left_bank.keys() | right_bank.keys()):
        a, b = left_bank.get(harmonic), right_bank.get(harmonic)
        gain = (a or b).get('projection_gain', .95)
        if a is not None and b is not None:
            gain = (1-cosine(position))*gain+cosine(position)*b.get('projection_gain', .95)
        if a is None:
            hz = b['hz']
            gate = cosine((frames-center)/taper)
        elif b is None:
            hz = a['hz']
            gate = cosine((center+step-frames)/taper)
        else:
            hz = a['hz']
            gate = np.ones(len(residual))
        for row, node in ((a,left), (b,right)):
            if row is None:
                continue
            first = node['context_frames'][0]
            for onset, end in row['true_absence_intervals_seconds']:
                after_start = cosine((frames-(first+(onset-1)*sr))/sr)
                before_end = cosine(((first+(end+1)*sr)-frames)/sr)
                gate *= 1-after_start*before_end
        phase = hz*(frames-center)/sr
        if a is not None and b is not None:
            u = np.clip(position,0,1)
            integral = .5*(u-np.sin(np.pi*u)/np.pi)+np.maximum(0.,position-1)
            phase += (b['hz']-a['hz'])*step/sr*integral
        oscillator = np.exp(2j*np.pi*phase)
        env = compact_line_projection(residual, oscillator, sr)
        correction += gain*gate*np.real(env*oscillator)
    return correction
