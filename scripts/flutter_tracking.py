"""Original-audio reference discovery, signed-frequency tracking and check tones.

No report-generator imports or filesystem side effects. Frequency trajectories
are measured on original audio; target cleanup belongs to stage 03.
"""
import numpy as np
from scipy import ndimage, signal
from flutter import positions, resample_at

RATE = 200


def discover(x, sr, limit=4):
    """Search the recorded band, reserving a candidate outside the mains comb."""
    if len(x) < 4 * sr or np.max(abs(x)) < 1e-12:
        return []
    f, _, p = signal.spectrogram(x, sr, nperseg=2*sr, noverlap=sr)
    power = ndimage.gaussian_filter1d(np.median(p, axis=1), 4)
    background = ndimage.median_filter(power, size=321)
    score = 10*np.log10((power+1e-30)/(background+1e-30))
    peaks, _ = signal.find_peaks(score, distance=60)
    rows = []
    for i in peaks:
        hz = float(f[i])
        if not 45 < hz < sr/2-100 or 2150 < hz < 2850:
            continue  # Reserve check tones for target validation.
        band = abs(f-hz) < 15
        side = (abs(f-hz) > 45) & (abs(f-hz) < 90)
        contrast = 10*np.log10((p[band].max(axis=0)+1e-30) /
                              (np.median(p[side], axis=0)+1e-30))
        occupancy = float(np.mean(contrast > 10))
        if occupancy < .5 or np.median(contrast) < 8 or score[i] < 6:
            continue
        rows.append(dict(carrier_hz=hz, contrast_db=float(np.median(contrast)),
                         occupancy=occupancy, spectral_score_db=float(score[i]),
                         near_mains_family=abs(hz/60-round(hz/60))*60 < 5))
    rows.sort(key=lambda r: r['spectral_score_db'], reverse=True)
    # Retain more than just the strongest 60 Hz line: the 120 Hz family supplied
    # the useful CH21 reference in the CH22 trial.
    nonmains = [r for r in rows if not r['near_mains_family']][:max(1, limit//2)]
    mains = [r for r in rows if r['near_mains_family']][:limit-len(nonmains)]
    chosen = nonmains+mains
    return chosen+[r for r in rows if r not in chosen][:limit-len(chosen)]


def trajectory(x, sr, hz, capture, support_mask=None):
    """Signed phase tracking and local modulation rate, as used in the trials."""
    if sr % 1000 or len(x) < 10*sr or not np.isfinite(x).all():
        raise ValueError('Tracking needs finite mono audio, >=10 s and a rate divisible by 1000')
    if not 0 < capture < min(450, hz, sr/2-hz):
        raise ValueError('Invalid carrier capture bandwidth')
    t = np.arange(len(x))/sr
    z = signal.resample_poly(x*np.exp(-2j*np.pi*hz*t), 1, sr//1000)
    z = signal.sosfiltfilt(signal.butter(6, capture, fs=1000, output='sos'), z)
    tt = np.arange(round(len(x)/sr*RATE))/RATE
    theta = np.interp(tt, np.arange(len(z))/1000, np.unwrap(np.angle(z)))
    amp = np.interp(tt, np.arange(len(z))/1000, abs(z))
    freq = hz+np.gradient(theta)*RATE/(2*np.pi)
    supported = np.ones(len(tt), dtype=bool) if support_mask is None else np.asarray(support_mask, dtype=bool)
    if supported.shape != tt.shape or supported.sum() < 2*RATE:
        raise ValueError('Reference needs at least two seconds of local support')
    median_amp = float(np.median(amp[400:-400] if support_mask is None else amp[supported]))
    if median_amp < 1e-12:
        raise ValueError('No measurable reference')
    centre = float(np.median(freq[400:-400] if support_mask is None else freq[supported]))
    disp = (theta+2*np.pi*(hz-centre)*tt)/(2*np.pi*centre)
    error = np.clip((freq-centre)/centre, -.10, .10)
    freqs, times, spectrum = signal.stft(error, RATE, nperseg=800, noverlap=700)
    ids = np.flatnonzero((freqs >= 4) & (freqs <= 10))
    peak = ids[np.argmax(abs(spectrum[ids]), axis=0)]
    rates = []
    for col, k in enumerate(peak):
        yy = np.log(abs(spectrum[k-1:k+2, col])+1e-15)
        delta = .5*(yy[0]-yy[2])/(yy[0]-2*yy[1]+yy[2]+1e-15)
        rates.append(float(freqs[k]+np.clip(delta, -.5, .5)*.25))
    rate = np.interp(tt, times, ndimage.median_filter(rates, size=3))
    valid = (amp > (.15 if support_mask is not None else .3)*median_amp) & (abs(freq-centre) < .9*capture) & supported
    # Reject a neighbourhood of dropouts/phase slips; never extrapolate through it.
    support = ndimage.uniform_filter1d(valid.astype(float), size=121, mode='nearest')
    # Match the accepted estimator's support ramp. Brief AM minima are handled
    # by robust regression; requiring 95% raw phase validity rejects the useful
    # low-frequency CH21 carrier despite its recoverable periodic displacement.
    gate = np.clip((support-.35)/.40, 0, 1)
    # A short capture excursion or true signal loss must not disappear inside
    # the occupancy average. Keep a hard reacquisition exclusion around it.
    unsafe = (abs(freq-centre) >= .9*capture) | (amp < .05*median_amp)
    block = sr//RATE
    clipped_samples = (abs(x) >= .999) & np.r_[False, abs(np.diff(x)) < 1e-12]
    clipped = np.add.reduceat(clipped_samples.astype(float), np.arange(0, len(x), block))/block
    unsafe |= np.interp(tt, np.arange(len(clipped))/RATE, clipped) > .01
    gate *= 1-ndimage.maximum_filter1d(unsafe.astype(float), size=101, mode='nearest')
    if support_mask is not None:
        # Legitimate AM low bits retain support; explicit missing carrier does
        # not. Erode the screened edges to remove filter/reacquisition context.
        safe = ndimage.minimum_filter1d(supported.astype(float), size=101, mode='constant', cval=0)
        gate *= safe
    f, p = signal.welch(error[400:-400], RATE, nperseg=min(4000, len(error)-800))
    band = (f >= 2) & (f <= 30)
    fm = float(f[band][np.argmax(p[band])])
    concentration = float(p[abs(f-fm) < .6].sum()/max(p[band].sum(), 1e-30))
    meta = dict(carrier_hz=hz, centre_hz=centre, capture_hz=capture,
                support=float(np.mean(gate[400:-400] > .95)),
                modulation_peak_hz=fm, concentration=concentration,
                out_of_band_motion_rms_percent=float(100*np.sqrt(max(0., np.trapezoid(p, f)-np.trapezoid(p[band], f[band])))),
                relative_motion_band_hz=[2, 30], absolute_speed_estimated=False,
                clipping_fraction=float(np.mean(clipped_samples)),
                capture_loss_or_dropout_fraction=float(np.mean(unsafe)),
                rate_percentiles_hz=np.percentile(rate[400:-400], [5, 50, 95]).tolist())
    return tt, error, disp, amp, rate, gate, meta


def bound(e, gate):
    tt = np.arange(len(e))/RATE
    return np.clip(e, -.049, .049)*gate*np.clip(np.minimum(tt, tt[-1]-tt)/2, 0, 1)


def signed_map(trace, band=(2, 30)):
    return bound(signal.sosfiltfilt(signal.butter(3, band, fs=RATE,
                 btype='bandpass', output='sos'), trace[1]), trace[5])


def burst_track(x, sr, carrier_hz=2500., capture_hz=280.):
    t = np.arange(len(x))/sr
    z = signal.resample_poly(x*np.exp(-2j*np.pi*carrier_hz*t), 1, sr//1000)
    z = signal.sosfiltfilt(signal.butter(5, capture_hz, fs=1000, output='sos'), z)
    amp = ndimage.gaussian_filter1d(abs(z), 2)
    freq = ndimage.gaussian_filter1d(carrier_hz+np.gradient(np.unwrap(np.angle(z)))*1000/(2*np.pi), 3)
    return amp, freq


def spans(mask):
    d = np.diff(np.r_[False, mask, False].astype(int))
    return list(zip(np.flatnonzero(d == 1), np.flatnonzero(d == -1)))


def beeps(x, sr):
    amp, f = burst_track(x, sr)
    floor = max(float(np.percentile(amp, 25)), 1e-15)
    mask = ndimage.binary_closing(amp > max(4*floor, 2*np.median(amp)), iterations=3)
    out = []
    for lo, hi in spans(mask):
        if not .18 < (hi-lo)/1000 < .36 or lo < 300 or hi > len(amp)-300:
            continue
        core = slice(lo+20, hi-20)
        ratio = float(np.percentile(amp[core], 90)/max(np.percentile(amp[core], 10), 1e-15))
        rough = float(np.std(f[core]-ndimage.gaussian_filter1d(f[core], 8)))
        if ratio <= 2.3 and rough <= 18 and 2360 < np.median(f[core]) < 2600:
            out.append(dict(start=lo/1000, end=hi/1000,
                            amplitude_over_floor=float(np.median(amp[core])/floor)))
    return out


def evaluate_events(x, sr, e, events, sample_positions=None):
    """Render each check event through the actual inverse warp; keep all margins."""
    if not events:
        return []
    pos = positions(e, sr, len(x)) if sample_positions is None else np.asarray(sample_positions)
    if pos.shape != (len(x),) or not np.isfinite(pos).all() or np.any(np.diff(pos) <= 0):
        raise ValueError('Event evaluation needs a finite monotonic inverse map')
    out = []
    for b in events:
        lo, hi = max(0, int((b['start']-.15)*sr)), min(len(x), int((b['end']+.15)*sr))
        requested = pos[lo:hi]
        left = max(0, int(requested.min())-sr//5)
        right = min(len(x), int(requested.max())+sr//5)
        y = resample_at(x[left:right], requested-left)
        carrier, capture = b.get('carrier_hz', 2500.), b.get('capture_hz', 280.)
        _, fb = burst_track(x[lo:hi], sr, carrier, capture)
        _, fa = burst_track(y, sr, carrier, capture)
        t = lo/sr+np.arange(len(fb))/1000
        mt = np.interp(t*sr, np.arange(len(pos)), pos)/sr
        margins = []
        for margin in (.025, .04, .055):
            kb = (t > b['start']+margin) & (t < b['end']-margin)
            ka = (mt > b['start']+margin) & (mt < b['end']-margin)
            if min(kb.sum(), ka.sum()) < 40:
                continue
            before, after = float(np.std(fb[kb])), float(np.std(fa[ka]))
            margins.append(dict(margin_seconds=margin, before_rms_hz=before,
                                after_rms_hz=after, residual_ratio=after/max(before, 1e-9),
                                after_rms_cents=float(1200/np.log(2)*after/max(abs(np.median(fa[ka])), 1.)),
                                before_rms_cents=float(1200/np.log(2)*before/max(abs(np.median(fb[kb])), 1.))))
        out.append(dict(b, margins=margins))
    return out
