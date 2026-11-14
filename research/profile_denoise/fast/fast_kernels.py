"""Fused single-thread Numba kernels for the v2.16 hum model (prototype)."""
import numba
import numpy as np

from fast_sincos import _sincos

TWO_PI = 2*np.pi


@numba.njit(cache=True, fastmath=False)
def tone(hz, n, sr, block=256):
    """exp(2j*pi*hz*arange(n)/sr) via block-reseeded phasor recurrence."""
    out = np.empty(n, dtype=np.complex128)
    w = np.exp(1j*(TWO_PI*hz/sr))
    for start in range(0, n, block):
        z = np.exp(1j*(TWO_PI*hz*(start/sr)))
        stop = min(n, start+block)
        for i in range(start, stop):
            out[i] = z
            z = z*w
    return out


@numba.njit(cache=True, fastmath=False)
def tone_phase(scale, phase, offset):
    """exp(1j*(scale*phase + offset)) for an arbitrary phase array."""
    out = np.empty(len(phase), dtype=np.complex128)
    for i in range(len(phase)):
        s, c = _sincos(scale*phase[i]+offset)
        out[i] = complex(c, s)
    return out


@numba.njit(cache=True, fastmath=False)
def _fill_tone(buffer, hz, sr, start, count):
    """buffer[:count] = exp(2j*pi*hz*(start+arange(count))/sr), reseeded every 256."""
    w = np.exp(1j*(TWO_PI*hz/sr))
    for block in range(0, count, 256):
        z = np.exp(1j*(TWO_PI*hz*((start+block)/sr)))
        stop = min(count, block+256)
        for i in range(block, stop):
            buffer[i] = z
            z = z*w


@numba.njit(cache=True, fastmath=False)
def _tone_observations(samples, hz, sr, starts, window):
    n = len(window)
    hop = starts[1]-starts[0]
    count = len(starts)
    result = np.empty(count, dtype=np.complex128)
    buffer = np.empty(n, dtype=np.complex128)
    _fill_tone(buffer, hz, sr, starts[0], n)
    for index in range(count):
        start = starts[index]
        if index:
            for i in range(hop):
                buffer[i] = buffer[hop+i]
            _fill_tone(buffer[hop:], hz, sr, start+hop, hop)
        real, imag = 0., 0.
        for offset in range(n):
            sample = 2.*samples[start+offset]*window[offset]
            o = buffer[offset]
            real += sample*o.real
            imag -= sample*o.imag
        result[index] = complex(real, imag)
    return result


@numba.njit(cache=True, fastmath=False)
def _tone_observations_residual(samples, model, hz, sr, starts, window):
    n = len(window)
    hop = starts[1]-starts[0]
    count = len(starts)
    result = np.empty(count, dtype=np.complex128)
    buffer = np.empty(n, dtype=np.complex128)
    _fill_tone(buffer, hz, sr, starts[0], n)
    for index in range(count):
        start = starts[index]
        if index:
            for i in range(hop):
                buffer[i] = buffer[hop+i]
            _fill_tone(buffer[hop:], hz, sr, start+hop, hop)
        real, imag = 0., 0.
        for offset in range(n):
            k = start+offset
            sample = 2.*(samples[k]-model[k])*window[offset]
            o = buffer[offset]
            real += sample*o.real
            imag -= sample*o.imag
        result[index] = complex(real, imag)
    return result


def tone_observations(samples, model, use_model, hz, sr, starts, window):
    """observations(samples[-model], tone(hz)) using a hop-sized oscillator buffer."""
    if use_model:
        return _tone_observations_residual(samples, model, hz, sr, starts, window)
    return _tone_observations(samples, hz, sr, starts, window)


@numba.njit(cache=True, fastmath=False)
def phase_observations(samples, scale, phase, starts, window):
    """observations(samples, exp(1j*scale*phase)) without materializing the oscillator."""
    n = len(window)
    hop = starts[1]-starts[0]
    count = len(starts)
    result = np.empty(count, dtype=np.complex128)
    buffer = np.empty(n, dtype=np.complex128)
    for i in range(n):
        s_, c_ = _sincos(scale*phase[starts[0]+i])
        buffer[i] = complex(c_, s_)
    for index in range(count):
        start = starts[index]
        if index:
            buffer[:hop] = buffer[hop:]
            for i in range(hop):
                s_, c_ = _sincos(scale*phase[start+hop+i])
                buffer[hop+i] = complex(c_, s_)
        real, imag = 0., 0.
        for offset in range(n):
            sample = 2.*samples[start+offset]
            o = buffer[offset]
            real += (sample*o.real)*window[offset]
            imag += (-sample*o.imag)*window[offset]
        result[index] = complex(real, imag)
    return result


@numba.njit(cache=True, fastmath=False)
def accumulate_tone(model, coef, hz, sr):
    """model += real(coef*tone(hz))."""
    w = np.exp(1j*(TWO_PI*hz/sr))
    n = len(model)
    for block in range(0, n, 256):
        z = np.exp(1j*(TWO_PI*hz*(block/sr)))
        stop = min(n, block+256)
        for i in range(block, stop):
            model[i] += coef.real*z.real-coef.imag*z.imag
            z = z*w


@numba.njit(cache=True, fastmath=False)
def demodulate_tone(samples, hz, sr):
    """2*samples*conj(tone(hz))."""
    n = len(samples)
    out = np.empty(n, dtype=np.complex128)
    w = np.exp(1j*(TWO_PI*hz/sr))
    for block in range(0, n, 256):
        z = np.exp(1j*(TWO_PI*hz*(block/sr)))
        stop = min(n, block+256)
        for i in range(block, stop):
            s = 2.*samples[i]
            out[i] = complex(s*z.real, -s*z.imag)
            z = z*w
    return out


@numba.njit(cache=True, fastmath=False)
def multiply_inplace(a, b):
    for i in range(len(a)):
        a[i] = a[i]*b[i]


@numba.njit(cache=True, fastmath=False)
def observations(samples, oscillator, starts, window):
    """Projection of 2*samples*conj(osc) onto window frames (production kernel)."""
    result = np.empty(len(starts), dtype=np.complex128)
    for index in range(len(starts)):
        real, imag = 0., 0.
        start = starts[index]
        for offset in range(len(window)):
            sample = 2.*samples[start+offset]
            real += (sample*oscillator[start+offset].real)*window[offset]
            imag += (-sample*oscillator[start+offset].imag)*window[offset]
        result[index] = complex(real, imag)
    return result


@numba.njit(cache=True, fastmath=False)
def observations_residual(samples, model, oscillator, starts, window):
    """observations(samples-model, ...) without materializing the residual."""
    result = np.empty(len(starts), dtype=np.complex128)
    for index in range(len(starts)):
        real, imag = 0., 0.
        start = starts[index]
        for offset in range(len(window)):
            k = start+offset
            sample = 2.*(samples[k]-model[k])
            real += (sample*oscillator[k].real)*window[offset]
            imag += (-sample*oscillator[k].imag)*window[offset]
        result[index] = complex(real, imag)
    return result


@numba.njit(cache=True, fastmath=False)
def observations_real_product(coef, oscillator, gate, starts, window):
    """observations(real(coef*osc)*gate, osc) without materializing the product."""
    result = np.empty(len(starts), dtype=np.complex128)
    for index in range(len(starts)):
        real, imag = 0., 0.
        start = starts[index]
        for offset in range(len(window)):
            k = start+offset
            o = oscillator[k]
            applied = (coef.real*o.real-coef.imag*o.imag)*gate[k]
            sample = 2.*applied
            real += (sample*o.real)*window[offset]
            imag += (-sample*o.imag)*window[offset]
        result[index] = complex(real, imag)
    return result


@numba.njit(cache=True, fastmath=False)
def accumulate(model, coef, oscillator, gate_a, gate_b):
    """model += gate_a*gate_b*real(coef*osc)."""
    for i in range(len(model)):
        o = oscillator[i]
        model[i] += gate_a[i]*gate_b[i]*(coef.real*o.real-coef.imag*o.imag)


@numba.njit(cache=True, fastmath=False)
def accumulate_gated(model, coef, oscillator, gate):
    """model += gate*real(coef*osc)."""
    for i in range(len(model)):
        o = oscillator[i]
        model[i] += gate[i]*(coef.real*o.real-coef.imag*o.imag)


@numba.njit(cache=True, fastmath=False)
def accumulate_plain(model, coef, oscillator):
    for i in range(len(model)):
        o = oscillator[i]
        model[i] += coef.real*o.real-coef.imag*o.imag


@numba.njit(cache=True, fastmath=False)
def real_product_gated(coef, oscillator, gate):
    """real(coef*osc)*gate as a new array."""
    out = np.empty(len(oscillator))
    for i in range(len(oscillator)):
        o = oscillator[i]
        out[i] = (coef.real*o.real-coef.imag*o.imag)*gate[i]
    return out


@numba.njit(cache=True, fastmath=False)
def interp_uniform(n, sr, xp, fp):
    """np.interp(arange(n)/sr, xp, fp) for increasing xp; segment-wise evaluation."""
    out = np.empty(n)
    m = len(xp)
    first = min(n, max(0, int(np.ceil(xp[0]*sr))))
    for i in range(first):
        out[i] = fp[0]
    i = first
    for j in range(m-1):
        x0, x1 = xp[j], xp[j+1]
        stop = min(n, int(np.ceil(x1*sr)))
        if stop <= i:
            continue
        slope = (fp[j+1]-fp[j])/(x1-x0)
        for k in range(i, stop):
            out[k] = fp[j]+slope*(k/sr-x0)
        i = stop
    for k in range(i, n):
        out[k] = fp[m-1]
    return out


@numba.njit(cache=True, fastmath=False)
def interp_uniform_complex(n, sr, xp, fp):
    out = np.empty(n, dtype=np.complex128)
    m = len(xp)
    first = min(n, max(0, int(np.ceil(xp[0]*sr))))
    for i in range(first):
        out[i] = fp[0]
    i = first
    for j in range(m-1):
        x0, x1 = xp[j], xp[j+1]
        stop = min(n, int(np.ceil(x1*sr)))
        if stop <= i:
            continue
        slope = (fp[j+1]-fp[j])/(x1-x0)
        for k in range(i, stop):
            out[k] = fp[j]+slope*(k/sr-x0)
        i = stop
    for k in range(i, n):
        out[k] = fp[m-1]
    return out


@numba.njit(cache=True, fastmath=False)
def tone_piecewise(hz, n, sr, xp, fp, block=256):
    """exp(1j*(2*pi*hz*t + interp(t, xp, fp))) for t = arange(n)/sr.

    The interpolated phase is linear within each knot interval, so each
    interval is a pure tone generated by a block-reseeded recurrence.
    """
    out = np.empty(n, dtype=np.complex128)
    m = len(xp)
    # Segment boundaries in samples: [0, ceil(xp[0]*sr)), knots..., [ceil(xp[-1]*sr), n)
    bounds = np.empty(m+2, dtype=np.int64)
    bounds[0] = 0
    for j in range(m):
        bounds[j+1] = min(n, max(0, int(np.ceil(xp[j]*sr))))
    bounds[m+1] = n
    for seg in range(m+1):
        a, b = bounds[seg], bounds[seg+1]
        if b <= a:
            continue
        if seg == 0:
            offset, slope = fp[0], 0.
            x0 = 0.
        elif seg == m:
            offset, slope = fp[m-1], 0.
            x0 = 0.
        else:
            j = seg-1
            x0 = xp[j]
            slope = (fp[j+1]-fp[j])/(xp[j+1]-x0)
            offset = fp[j]
        rate = TWO_PI*hz+slope
        w = np.exp(1j*(rate/sr))
        for start in range(a, b, block):
            t = start/sr
            z = np.exp(1j*(TWO_PI*hz*t+offset+slope*(t-x0)))
            stop = min(b, start+block)
            for i in range(start, stop):
                out[i] = z
                z = z*w
    return out


@numba.njit(cache=True, fastmath=False)
def envelope_apply(model, env, osc, low_gate, cap, floor_amp):
    """model += low_gate*real(env*(subtract/(|env|+eps))*osc) with subtract = max(0,min(|env|,cap)-floor_amp)."""
    for i in range(len(model)):
        e = env[i]
        amplitude = abs(e)
        subtract = min(amplitude, cap)-floor_amp[i]
        if subtract < 0.:
            subtract = 0.
        scaled = e*(subtract/(amplitude+1e-30))
        o = osc[i]
        model[i] += low_gate[i]*(scaled.real*o.real-scaled.imag*o.imag)


@numba.njit(cache=True, fastmath=False)
def demodulate(samples, oscillator):
    """2*samples*conj(osc)."""
    out = np.empty(len(samples), dtype=np.complex128)
    for i in range(len(samples)):
        o = oscillator[i]
        s = 2.*samples[i]
        out[i] = complex(s*o.real, -s*o.imag)
    return out


def warmup():
    n = 4000
    osc = tone(60., n, 8000.)
    tone_phase(1., np.zeros(n), 0.)
    multiply_inplace(osc.copy(), osc)
    starts = np.array([0, 1000, 2000]); w = np.ones(2000)/2000
    x = np.zeros(n); m = np.zeros(n); g = np.ones(n)
    observations(x, osc, starts, w)
    tone_observations(x, m, True, 60., 8000., starts, w); tone_observations(x, m, False, 60., 8000., starts, w)
    phase_observations(x, 2., np.zeros(n), starts, w); accumulate_tone(m, 1+0j, 60., 8000.); demodulate_tone(x, 60., 8000.)
    observations_residual(x, m, osc, starts, w)
    observations_real_product(1+0j, osc, g, starts, w)
    accumulate(m, 1+0j, osc, g, g); accumulate_gated(m, 1+0j, osc, g); accumulate_plain(m, 1+0j, osc)
    real_product_gated(1+0j, osc, g)
    interp_uniform(n, 8000., np.array([0., .5]), np.array([0., 1.]))
    tone_piecewise(60., n, 8000., np.array([.1, .3]), np.array([0., 1.]))
    interp_uniform_complex(n, 8000., np.array([0., .5]), np.array([0j, 1j]))
    envelope_apply(m, osc, osc, g, 1., g)
    demodulate(x, osc)
    compact_weight_and_oscillator(0, n, 8000., 0, 512000, 6*8000, 60., 60.1, 2, .95, .95, np.array([[0., .5, 1.]]))
    harmonic_observations(x, np.zeros(n), 3, 5, starts, w)
    accumulate_harmonics(m, np.zeros(n), g, np.array([3, 4]), np.ones(2, dtype=np.complex128), np.array([.25, .5]), np.ones((2, 2)), 8000.)


@numba.njit(cache=True, fastmath=False)
def real_complex_product_gated(env, oscillator, gate):
    """real(env*osc)*gate as a new array."""
    out = np.empty(len(oscillator))
    for i in range(len(oscillator)):
        e, o = env[i], oscillator[i]
        out[i] = (e.real*o.real-e.imag*o.imag)*gate[i]
    return out


@numba.njit(cache=True, fastmath=False)
def accumulate_complex_gated(out, env, oscillator, gate):
    """out += gate*real(env*osc)."""
    for i in range(len(out)):
        e, o = env[i], oscillator[i]
        out[i] += gate[i]*(e.real*o.real-e.imag*o.imag)


@numba.njit(cache=True, fastmath=False)
def fit_groups(groups, centers, valid, training, validation, levels, residual,
               amplitude_floor, original_level, percentile, threshold, improve_ratio):
    """Local phasor fits for residual_refinement, one 8-second group at a time."""
    local_coeffs = np.zeros(len(groups), dtype=np.complex128)
    measured_zero = np.zeros(len(groups), dtype=np.bool_)
    for index in range(len(groups)):
        center = groups[index]
        local = valid & (np.abs(centers-center) <= 4)
        fit = local & training
        check = local & validation
        if min(fit.sum(), check.sum()) < 8:
            continue
        fit &= levels <= np.percentile(levels[fit], percentile)
        check &= levels <= np.percentile(levels[check], percentile)
        rf = residual[fit]
        rc = residual[check]
        amount = complex(np.median(rf.real), np.median(rf.imag))
        floor_level = np.median(amplitude_floor[fit])
        if np.median(np.abs(rc)) <= 1.2*np.median(amplitude_floor[check]):
            measured_zero[index] = True
            continue
        scatter = np.median(np.abs(rf-amount))
        if abs(amount) <= max(threshold*floor_level, 1.25*scatter):
            continue
        cap = .8*np.median(original_level[fit])
        amount *= min(1., cap/(abs(amount)+1e-30))
        old = np.median(np.abs(rc)**2)
        new = np.median(np.abs(rc-amount)**2)
        if new < improve_ratio*old:
            local_coeffs[index] = amount
    return local_coeffs, measured_zero


@numba.njit(cache=True, fastmath=False)
def harmonic_observations(samples, phase, first, last, starts, window):
    """observations(samples, exp(1j*j*phase)) for j in [first, last), shape (last-first, frames).

    Harmonic oscillators are running products of exp(1j*phase) inside one
    window-sized buffer, so no full-length oscillator is ever materialized.
    """
    n = len(window)
    count = len(starts)
    result = np.empty((last-first, count), dtype=np.complex128)
    base = np.empty(n, dtype=np.complex128)
    power = np.empty(n, dtype=np.complex128)
    sw = np.empty(n)
    for index in range(count):
        start = starts[index]
        for offset in range(n):
            s_, c_ = _sincos(phase[start+offset])
            base[offset] = complex(c_, s_)
            sw[offset] = 2.*samples[start+offset]*window[offset]
        # power = base**(first-1)
        for offset in range(n):
            power[offset] = 1.+0j
        for _ in range(first-1):
            for offset in range(n):
                power[offset] = power[offset]*base[offset]
        for j in range(first, last):
            real, imag = 0., 0.
            for offset in range(n):
                p = power[offset]*base[offset]
                power[offset] = p
                real += sw[offset]*p.real
                imag -= sw[offset]*p.imag
            result[j-first, index] = complex(real, imag)
    return result


@numba.njit(cache=True, fastmath=False)
def accumulate_harmonics(model, phase, gate, harmonics, coefs, centers, presences, sr):
    """model += gate * sum_j interp(t, centers, presences[j]) * real(coefs[j]*exp(1j*harmonics[j]*phase)).

    Blocks are aligned to the uniform frame-center grid, so each block lies
    inside a single interpolation interval of every presence curve.
    """
    n = len(model)
    hop = int(round((centers[1]-centers[0])*sr))
    first_knot = int(round(centers[0]*sr))
    m = len(centers)
    count = len(harmonics)
    top = 0
    for a in range(count):
        top = max(top, harmonics[a])
    base = np.empty(hop, dtype=np.complex128)
    power = np.empty(hop, dtype=np.complex128)
    contribution = np.empty(hop)
    block_start = 0
    while block_start < n:
        # Block [block_start, block_end) within one knot interval.
        rel = block_start-first_knot
        if rel < 0:
            block_end = min(n, first_knot)
            seg = -1
        else:
            seg = rel//hop
            block_end = min(n, first_knot+(seg+1)*hop)
        length = block_end-block_start
        for i in range(length):
            s_, c_ = _sincos(phase[block_start+i])
            base[i] = complex(c_, s_)
            power[i] = 1.+0j
            contribution[i] = 0.
        j = 0
        for a in range(count):
            target = harmonics[a]
            while j < target:
                for i in range(length):
                    power[i] = power[i]*base[i]
                j += 1
            coef = coefs[a]
            # Linear presence inside this block.
            if seg < 0:
                p0, slope = presences[a, 0], 0.
                x0 = 0.
            elif seg >= m-1:
                p0, slope = presences[a, m-1], 0.
                x0 = 0.
            else:
                x0 = centers[seg]
                p0 = presences[a, seg]
                slope = (presences[a, seg+1]-p0)/(centers[seg+1]-x0)
            for i in range(length):
                cg = p0+slope*((block_start+i)/sr-x0)
                p = power[i]
                contribution[i] += cg*(coef.real*p.real-coef.imag*p.imag)
        for i in range(length):
            model[block_start+i] += gate[block_start+i]*contribution[i]
        block_start = block_end


@numba.njit(cache=True, fastmath=False, inline='always')
def _rising_cosine(fraction):
    if fraction <= 0.:
        return 0.
    if fraction >= 1.:
        return 1.
    return .5-.5*np.cos(np.pi*fraction)


@numba.njit(cache=True, fastmath=False)
def compact_weight_and_oscillator(first_frame, n, sr, center, step, taper, hz_a, hz_b, mode,
                                  gain_a, gain_b, absences):
    """Blended gain*gate weight and exp(2j*pi*phase) for one compact-bank harmonic.

    mode: 0 = left bank only, 1 = right bank only, 2 = both banks.
    absences: rows of (context_first_frame, onset_seconds, end_seconds).
    """
    weight = np.empty(n)
    oscillator = np.empty(n, dtype=np.complex128)
    for i in range(n):
        frame = first_frame+i
        position = (frame-center)/step
        if mode == 2:
            cp = _rising_cosine(position)
            gain = (1-cp)*gain_a+cp*gain_b
            gate = 1.
            u = min(max(position, 0.), 1.)
            integral = .5*(u-np.sin(np.pi*u)/np.pi)+max(0., position-1)
            phase = hz_a*(frame-center)/sr+(hz_b-hz_a)*step/sr*integral
        elif mode == 0:
            gain = gain_a
            gate = _rising_cosine((center+step-frame)/taper)
            phase = hz_a*(frame-center)/sr
        else:
            gain = gain_b
            gate = _rising_cosine((frame-center)/taper)
            phase = hz_b*(frame-center)/sr
        for r in range(absences.shape[0]):
            first = absences[r, 0]
            after_start = _rising_cosine((frame-(first+(absences[r, 1]-1)*sr))/sr)
            before_end = _rising_cosine(((first+(absences[r, 2]+1)*sr)-frame)/sr)
            gate *= 1-after_start*before_end
        weight[i] = gain*gate
        s_, c_ = _sincos(TWO_PI*phase)
        oscillator[i] = complex(c_, s_)
    return weight, oscillator
