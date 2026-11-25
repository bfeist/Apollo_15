"""Check-tone-free blind tape-motion detection: the numerical census core.

Implements stages A-D of docs/09_2026-11-15_deflutter_detection_guide.md. This module reads
and writes nothing; callers supply canonical-original audio for one block of
all channels. Check beeps (2150-2850 Hz) never enter the estimate and are used
only as an audit. The output speed error uses the pipeline convention
``(f_inst - f_centre)/f_centre`` (as ``flutter_tracking.trajectory``) on the
shared 200 Hz grid, with one zero-lag curve for every channel of a tape.
"""
import copy
import hashlib
import json
import math

import numpy as np
from scipy import fft, ndimage, signal

from flutter import positions
from flutter_tracking import bound, evaluate_events, spans

RATE = 200
METHODOLOGY = 'blind-tape-census-v1'

SETTINGS = dict(
    methodology=METHODOLOGY, rate=RATE, sample_rate=8000,
    block_seconds=600., halo_seconds=8.,
    decimation=3, fast_max_hz=1150., full_rate_max_hz=3900.,
    mains=dict(welch_seconds=16., max_harmonic=64, peak_hz=.25, side_hz=[2., 6.],
               threshold_db=10., block_seconds=16.),
    candidates=dict(welch_seconds=2., min_hz=50., log_step=.0025, smoothing=3, background=161,
                    height_db=3., prominence_db=3., distance=8, per_channel=10,
                    reserved_hz=[2150., 2850.], mains_exclusion_hz=.5,
                    mains_exclusion_below_hz=1000., hum_bands=[[58.5, 59.95], [116., 119.9]],
                    hum_welch_seconds=8., hum_snr_db=4., duplicate_fraction=.02),
    tracking=dict(cap_slope=.06, cap_offset=20., cap_min=8., cap_max=120., band_hz=[2., 30.],
                  amplitude_floor=.3, capture_fraction=.9, erosion=10, min_support=.5),
    window=dict(seconds=64., step_seconds=42., edge_seconds=4., min_window_support=.5,
                r_edge=.6, family_separation=.025, max_clusters=4, min_cluster=3,
                gain_floor=.3, r_cap=.95, top_members=5, loo_r=.7, loo_gain=[.6, 1.6],
                comparable_fm_fraction=.25),
    gates=dict(min_agreeing=3, min_families=2, hum_min_agreeing=5, cluster_min_fm_percent=.8,
               consensus_fm_percent=[.5, 4.], duplicate_r=.95, speed_bound=.049,
               max_clipped_fraction=.01),
    fingerprint=dict(excerpt_seconds=20., excerpts=[.1, .5, .9], highpass_hz=300.),
    audit=dict(random_fraction=.1, beep_margin_seconds=5., beep_snippet_seconds=1.,
               shift_seconds=1.5, concentration_min_hz=200., concentration_lines=2,
               controls=dict(curve_ratio_below=.8, wrong_sign_above=1., shift_ratio_at_least=.9)),
    join=dict(taper_seconds=7.),
)


def normalized(value):
    """The JSON form of settings; tuples and lists compare equal after saving."""
    return json.loads(json.dumps(value, sort_keys=True, allow_nan=False))


def make_settings(**overrides):
    """Default settings with nested overrides (used for bounded tests only)."""
    result = copy.deepcopy(SETTINGS)
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key].update(value)
        else:
            result[key] = value
    return normalized(result)


def settings_digest(settings):
    return hashlib.sha256(json.dumps(normalized(settings), sort_keys=True).encode()).hexdigest()


# ---------------------------------------------------------------- stage A

def _welch(x, sr, nper):
    """Mean one-sided Welch density with bounded batches of Hann-windowed frames."""
    x = np.asarray(x)
    if not x.size:
        return signal.welch(x, sr, nperseg=nper)
    hop = nper-nper//2
    frames = np.lib.stride_tricks.sliding_window_view(x, nper)[::hop]
    window = signal.get_window('hann', nper)
    window /= np.sqrt(np.sum(window**2)*sr)
    power = np.zeros(nper//2+1)
    # Fit roughly 2 MiB of real samples per batch, even on the 16 s mains FFT.
    batch = max(1, (1 << 18)//nper)
    for start in range(0, len(frames), batch):
        part = frames[start:start+batch]
        centered = part-part.mean(axis=1, keepdims=True)
        spectrum = fft.rfft(centered*window, workers=1)
        power += np.sum(spectrum.real**2+spectrum.imag**2, axis=0)
    power[1:-1 if nper % 2 == 0 else None] *= 2
    power /= len(frames)
    return fft.rfftfreq(nper, 1/sr), power.astype(np.result_type(x.dtype, np.float32), copy=False)


def exact_mains(x, sr, settings, fmin=0., fmax=None):
    """Strong narrow lines at exact 60 Hz multiples: post-tape digitization hum."""
    m = settings['mains']
    nper = min(len(x), int(m['welch_seconds']*sr))
    if nper < sr:
        return []
    f, p = _welch(x, sr, nper)
    lp = 10*np.log10(p+1e-30)
    top = sr/2-m['side_hz'][1]-1 if fmax is None else min(fmax, sr/2-m['side_hz'][1]-1)
    out = []
    for n in range(1, int(m['max_harmonic'])+1):
        hz = 60.*n
        if hz <= fmin or hz >= top:
            continue
        peak = abs(f-hz) < m['peak_hz']
        side = (abs(f-hz) > m['side_hz'][0]) & (abs(f-hz) < m['side_hz'][1])
        if peak.any() and side.any() and lp[peak].max()-np.median(lp[side]) > m['threshold_db']:
            i = int(np.flatnonzero(peak)[np.argmax(lp[peak])])
            # Parabolic refinement: a residual frequency error leaves the
            # stationary line partly unsubtracted beside the weak on-tape hum.
            y0, y1, y2 = lp[i-1], lp[i], lp[i+1]
            delta = .5*(y0-y2)/(y0-2*y1+y2) if y0-2*y1+y2 < 0 else 0.
            out.append(float(f[i]+np.clip(delta, -.5, .5)*(f[1]-f[0])))
    return out


def remove_stationary(x, hz_list, sr, block_seconds=16.):
    """Per-harmonic least squares over fixed sub-blocks, batched with one basis.

    A fluttered on-tape line near an exact harmonic is FM-spread and is left
    largely intact; the stationary digitization line is removed. Identical to
    an independent 2x2 cos/sin fit per harmonic per sub-block.
    """
    y = np.array(x, dtype=np.float64)
    if not len(hz_list):
        return y
    hz = np.asarray(hz_list, float)
    n = int(block_seconds*sr)
    # Retain the original grouping: later harmonic fits see earlier residuals.
    per = max(1, (1 << 21)//max(n, 1))
    for first in range(0, len(hz), per):
        group, cache = hz[first:first+per], {}
        tile = min(256, n)
        # exp(i*w*(k*tile+j)) factors into one phase per tile and this
        # cache-sized cos/sin matrix. Both real BLAS products reuse it.
        basis = np.exp(2j*np.pi*np.outer(np.arange(tile)/sr, group))
        cos_sin = basis.view(np.float64)
        square = basis**2
        for a in range(0, len(y), n):
            m = min(n, len(y)-a)
            if m < sr:
                continue
            if m not in cache:
                phase = np.exp(2j*np.pi*np.outer(np.arange(0, m, tile)/sr, group))
                whole, tail = divmod(m, tile)
                twice = (phase[:whole]**2).sum(0)*square.sum(0)
                if tail:
                    twice += phase[-1]**2*square[:tail].sum(0)
                cc, ss, cs = (m+twice.real)/2, (m-twice.real)/2, twice.imag/2
                cache[m] = phase, cc, ss, cs, cc*ss-cs*cs
            phase, cc, ss, cs, det = cache[m]
            part = y[a:a+m]
            padding = (-m) % tile
            tiles = (np.pad(part, (0, padding)) if padding else part).reshape(-1, tile)
            projection = ((tiles @ cos_sin).view(np.complex128)*phase).sum(0)
            u = (ss*projection.real-cs*projection.imag)/det
            v = (cc*projection.imag-cs*projection.real)/det
            weights = phase.conj()*(u+1j*v)
            part -= (weights.view(np.float64) @ cos_sin.T).ravel()[:m]
    return y


# ---------------------------------------------------------------- stage B

def welch_db(x, sr, seconds):
    nper = min(len(x), int(seconds*sr))
    f, p = _welch(x, sr, nper)
    return f, 10*np.log10(p+1e-30)


def candidates(f, level_db, low, high, c):
    """Broad on-tape humps on a log-frequency axis, not the sharpest lines."""
    if high <= low*1.05:
        return []
    axis = np.exp(np.arange(np.log(low), np.log(high), c['log_step']))
    smooth = ndimage.gaussian_filter1d(np.interp(axis, f, level_db), c['smoothing'])
    excess = smooth-ndimage.median_filter(smooth, size=c['background'], mode='nearest')
    peaks, props = signal.find_peaks(excess, height=c['height_db'], prominence=c['prominence_db'],
                                     distance=c['distance'])
    rows = []
    for i in peaks[np.argsort(-props['peak_heights'])][:2*c['per_channel']]:
        hz = float(axis[i])
        if c['reserved_hz'][0] < hz < c['reserved_hz'][1]:
            continue  # Check beeps stay held out as an independent audit.
        if hz < c['mains_exclusion_below_hz'] and abs(hz-60*round(hz/60)) < c['mains_exclusion_hz']:
            continue  # Exact digitization comb: little FM leverage.
        rows.append(dict(hz=hz, prominence_db=float(excess[i])))
    return rows


def hum_peaks(x, sr, c):
    """Strongest peak in each historical on-tape mains band (60·rho·n Hz)."""
    f, level = welch_db(x, sr, c['hum_welch_seconds'])
    out = []
    for lo, hi in c['hum_bands']:
        band = (f >= lo) & (f <= hi)
        if band.sum() < 3:
            continue
        i = int(np.argmax(level[band]))
        if level[band][i]-np.median(level[band]) > c['hum_snr_db']:
            out.append(float(f[band][i]))
    return out


class BlockSpectrum:
    """One rFFT per cleaned channel block, reused by every carrier."""
    def __init__(self, x, sr):
        self.sr, self.n = float(sr), len(x)
        self.df = self.sr/self.n
        self.X = fft.rfft(np.asarray(x, np.float64), workers=1)


def baseband(spec, hz, capture, rate, order=6, taper=None):
    """FFT band isolation around ``hz``; returns complex baseband and its rate.

    ``taper`` widens the crop to ``taper*capture`` with a raised-cosine skirt,
    avoiding brick-wall pre-ringing around abrupt bursts.
    """
    d = max(1, int(spec.sr/rate))
    length, r = spec.n//d, spec.sr/d
    half = capture*(taper or 1.)
    i0 = max(1, int(round((hz-half)/spec.df)))
    i1 = min(len(spec.X)-1, int(round((hz+half)/spec.df)))
    if i1-i0 < 8 or i1-i0+1 >= length:  # A complex baseband holds bandwidth up to its rate.
        return None, r
    band = spec.X[i0:i1+1]
    if taper:
        offset = abs(np.arange(i0, i1+1)*spec.df-hz)
        band = band*(.5+.5*np.cos(np.pi*np.clip((offset-capture)/(half-capture), 0, 1)))
    folded = np.zeros(length, complex)
    # The crop is shorter than one alias period, so each bin is written once.
    first = i0 % length
    count = min(len(band), length-first)
    folded[first:first+count] = band[:count]
    folded[:len(band)-count] = band[count:]
    z = fft.ifft(folded, overwrite_x=True, workers=1)
    centre = (hz/r) % 1.
    centre = (centre-1. if centre > .5 else centre)*r
    # Factor the oscillator into short tiles and one phase per tile.
    tile = 256
    step = -2j*np.pi*centre/r
    phase = d*np.exp(step*np.arange(tile))
    whole = length//tile*tile
    offsets = np.exp(step*np.arange(0, length, tile))
    tiles = z[:whole].reshape(-1, tile)
    tiles *= phase
    tiles *= offsets[:len(tiles), None]
    if whole < length:
        z[whole:] *= offsets[-1]*phase[:length-whole]
    return signal.sosfiltfilt(signal.butter(order, capture, fs=r, output='sos'), z), r


def fft_demod(spec, hz, capture):
    """Instantaneous amplitude and frequency of one carrier at 200 Hz."""
    z, r = baseband(spec, hz, capture, min(1000., max(300., 6*capture, .25*hz)))
    if z is None:
        return None
    t = np.arange(len(z))/r
    fr = hz+np.gradient(np.unwrap(np.angle(z)))*r/(2*np.pi)
    grid = np.arange(int(round(spec.n/spec.sr*RATE)))/RATE
    return np.interp(grid, t, abs(z)), np.interp(grid, t, fr)


def frac_dev(fr, amp, capture, t):
    """Band-limited fractional deviation and its support mask."""
    centre = float(np.median(fr))
    e = (fr-centre)/centre
    ok = (amp > t['amplitude_floor']*np.median(amp)) & (abs(fr-centre) < t['capture_fraction']*capture)
    ok = ndimage.binary_erosion(ok, iterations=t['erosion'])
    e = np.clip(np.where(ok, e, 0.), -.05, .05)
    sos = signal.butter(3, t['band_hz'], fs=RATE, btype='bandpass', output='sos')
    return signal.sosfiltfilt(sos, e), ok, centre


def capture_hz(hz, t):
    return float(np.clip(t['cap_slope']*hz+t['cap_offset'], t['cap_min'], min(t['cap_max'], hz-20.)))


def track(spec, hz, t):
    cap = capture_hz(hz, t)
    demodulated = fft_demod(spec, hz, cap)
    if demodulated is None:
        return None
    amp, fr = demodulated
    e, ok, centre = frac_dev(fr, amp, cap, t)
    return dict(hz=float(hz), centre=centre, cap=cap, support=float(ok.mean()),
                rms_percent=100*float(e.std()), e=e.astype(np.float32), ok=ok)


def detect_beeps(spec, sr):
    """``flutter_tracking.beeps`` on an FFT-isolated 2.5 kHz baseband (audit only)."""
    z, r = baseband(spec, 2500., 280., 1000., order=5, taper=1.7)
    if z is None or abs(r-1000.) > 1e-9:
        return []
    amp = ndimage.gaussian_filter1d(abs(z), 2)
    freq = ndimage.gaussian_filter1d(2500.+np.gradient(np.unwrap(np.angle(z)))*r/(2*np.pi), 3)
    floor = max(float(np.percentile(amp, 25)), 1e-15)
    mask = ndimage.binary_closing(amp > max(4*floor, 2*np.median(amp)), iterations=3)
    out = []
    for lo, hi in spans(mask):
        if not .18 < (hi-lo)/1000 < .36 or lo < 300 or hi > len(amp)-300:
            continue
        core = slice(lo+20, hi-20)
        ratio = float(np.percentile(amp[core], 90)/max(np.percentile(amp[core], 10), 1e-15))
        rough = float(np.std(freq[core]-ndimage.gaussian_filter1d(freq[core], 8)))
        if ratio <= 2.3 and rough <= 18 and 2360 < np.median(freq[core]) < 2600:
            out.append(dict(start=lo/1000, end=hi/1000))
    return out


def channel_tracks(x, sr, settings):
    """Stage A+B for one channel block: clean, discover and track carriers."""
    c, t = settings['candidates'], settings['tracking']
    dec = int(settings['decimation'])
    low_rate = sr/dec
    x = np.asarray(x, np.float32)
    xd = np.asarray(signal.resample_poly(x, 1, dec), np.float64)
    fast = float(settings['fast_max_hz'])
    lines = exact_mains(xd, low_rate, settings, fmax=fast+100.)
    clean = remove_stationary(xd, lines, low_rate, settings['mains']['block_seconds'])
    rows = [dict(r, path='fast') for r in candidates(*welch_db(clean, low_rate, c['welch_seconds']),
                                                    c['min_hz'], fast, c)]
    paths = dict(fast=BlockSpectrum(clean, low_rate))
    meta = dict(mains_fast=len(lines))
    full = settings.get('full_rate_max_hz')
    if full:
        high_lines = exact_mains(x, sr, settings, fmin=fast-t['cap_max']-20., fmax=full)
        high = remove_stationary(x, high_lines, sr, settings['mains']['block_seconds'])
        rows += [dict(r, path='full') for r in candidates(*welch_db(high, sr, c['welch_seconds']),
                                                         fast, min(full, sr/2-100.), c)]
        paths['full'] = BlockSpectrum(high, sr)
        meta['mains_full'] = len(high_lines)
        del high
    rows = sorted(rows, key=lambda r: -r['prominence_db'])[:c['per_channel']]
    for hz in hum_peaks(clean, low_rate, c):
        if not any(abs(hz-r['hz']) < c['duplicate_fraction']*max(hz, r['hz']) for r in rows):
            rows.append(dict(hz=hz, prominence_db=None, path='fast', forced='hum_band'))
    tracks = []
    for row in rows:
        result = track(paths[row['path']], row['hz'], t)
        row['support'] = None if result is None else round(result['support'], 4)
        if result is not None and result['support'] >= t['min_support']:
            result.update(path=row['path'], prominence_db=row['prominence_db'])
            tracks.append(result)
    meta['candidates'] = [{k: (round(v, 4) if isinstance(v, float) else v) for k, v in r.items()}
                          for r in rows]
    beeps = detect_beeps(paths['full'] if full else BlockSpectrum(x, sr), sr)
    return tracks, meta, clean, beeps


def fingerprint(clean, low_rate, settings):
    """Speech-band excerpts of cleaned audio for the duplicate-feed guard."""
    f = settings['fingerprint']
    sos = signal.butter(4, f['highpass_hz'], fs=low_rate, btype='highpass', output='sos')
    n = int(f['excerpt_seconds']*low_rate)
    parts = []
    for q in f['excerpts']:
        a = int(max(0, min(len(clean)-n, q*len(clean)-n/2)))
        parts.append(signal.sosfiltfilt(sos, clean[a:a+n]))
    return np.concatenate(parts).astype(np.float32)


def duplicate_groups(prints, threshold, known=()):
    """Map each channel to its group representative; copies count once."""
    channels = sorted(prints)
    parent = {c: c for c in channels}

    def root(c):
        while parent[c] != c:
            parent[c] = parent[parent[c]]
            c = parent[c]
        return c

    def join(a, b):
        a, b = root(a), root(b)
        if a != b:
            parent[max(a, b)] = min(a, b)

    for group in known:
        present = [c for c in group if c in parent]
        for c in present[1:]:
            join(present[0], c)
    if len(channels) > 1:
        length = min(len(prints[c]) for c in channels)
        matrix = np.array([prints[c][:length] for c in channels], np.float64)
        matrix -= matrix.mean(1, keepdims=True)
        norms = np.sqrt((matrix*matrix).sum(1))
        valid = norms > 0
        r = (matrix @ matrix.T)/np.maximum(np.outer(norms, norms), 1e-30)
        for i, a in enumerate(channels):
            for j in range(i+1, len(channels)):
                if valid[i] and valid[j] and r[i, j] > threshold:
                    join(a, channels[j])
    return {c: root(c) for c in channels}


# ---------------------------------------------------------------- stage C

def same_source(a, b, tol=.3):
    """Same frequency, or low-order harmonics of one fundamental >= 80 Hz."""
    if abs(a-b) < 2.5:
        return True
    lo, hi = min(a, b), max(a, b)
    for k in range(1, 5):
        f0 = lo/k
        if f0 < 80:
            break
        n = round(hi/f0)
        if n > k and abs(hi-n*f0) < tol*(n/k):
            return True
    return False


def is_hum(hz):
    n = round(hz/59.25)
    return 1 <= n <= 11 and 58.5 <= hz/n <= 59.95


def correlation(a, b):
    a, b = a-a.mean(), b-b.mean()
    d = math.sqrt(float((a*a).sum()*(b*b).sum()))
    return float((a*b).sum()/d) if d > 0 else 0.


def screen_window(members, groups, settings):
    """Agreement graph, motion cluster, consensus and leave-one-channel-out.

    ``members`` holds (track, e_window, ok_window). Returns the record and the
    consensus over the window's valid (edge-trimmed) region, or None.
    """
    w, g = settings['window'], settings['gates']
    edge = int(w['edge_seconds']*RATE)
    pool = [(m, e[edge:len(e)-edge]) for m, e, ok in members
            if ok.mean() >= w['min_window_support']]
    record = dict(candidates=len(pool), eligible=False)
    if len(pool) < 2:
        return dict(record, reasons=['fewer than two supported candidates']), None
    M = np.array([e for _, e in pool], dtype=np.float64)
    std = M.std(1)
    rms = 100*std
    Mn = M-M.mean(1, keepdims=True)
    Mn /= std[:, None]+1e-12
    R = Mn @ Mn.T/M.shape[1]
    np.fill_diagonal(R, 0)
    group = np.array([groups[m['channel']] for m, _ in pool])
    hz = np.array([m['centre'] for m, _ in pool])
    graph = ((R > w['r_edge']) & (group[:, None] != group[None, :])
             & (abs(hz[:, None]-hz[None, :]) > w['family_separation']*np.maximum(hz[:, None], hz[None, :])))
    alive = np.ones(len(pool), bool)
    clusters = []
    for _ in range(int(w['max_clusters'])):
        degree = (graph & alive[None, :] & alive[:, None]).sum(1)
        if degree.max() < 2:
            break
        seed = int(np.argmax(degree))
        chosen = [seed]+[j for j in range(len(pool)) if alive[j] and j != seed and R[seed, j] > w['r_edge']]
        clusters.append((seed, chosen, float(np.median(rms[chosen]))))
        alive[chosen] = False
    record['clusters'] = [dict(seed_channel=int(pool[s][0]['channel']), seed_hz=round(hz[s], 2),
                               size=len(c), median_fm_percent=round(f, 3)) for s, c, f in clusters]
    usable = [c for c in clusters if len(c[1]) >= w['min_cluster']]
    if not usable:
        return dict(record, reasons=['no agreeing cluster']), None
    # Post-tape common combs agree perfectly but carry ~0.15 % FM; tape ~2 %.
    # Clusters of comparable FM can be mirror products with inverted motion
    # (anti-correlated seeds). FM cannot separate those; the cluster seen on
    # the most independent channel groups wins, and the conflict is recorded.
    top = max(c[2] for c in usable)
    comparable = [c for c in usable if c[2] >= (1-w['comparable_fm_fraction'])*top]
    seed, chosen, cluster_fm = max(comparable, key=lambda c: (len({int(group[j]) for j in c[1]}), c[2]))
    record['sign_conflict'] = any(R[seed, c[0]] < -w['r_edge'] for c in comparable if c[0] != seed)
    reference = M[seed]
    normalized_members = {}
    for j in chosen:
        gain = 1. if j == seed else float(M[j] @ reference/max(reference @ reference, 1e-30))
        r = w['r_cap'] if j == seed else min(R[seed, j], w['r_cap'])
        if gain > w['gain_floor']:
            normalized_members[j] = (M[j]/gain, r*r/(1-r*r))
    loo = {}
    for c in sorted({int(group[j]) for j in normalized_members}):
        own = [j for j in normalized_members if group[j] == c]
        others = [j for j in normalized_members if group[j] != c]
        if not others:
            continue
        weight = sum(normalized_members[j][1] for j in others)
        prediction = sum(normalized_members[j][1]*normalized_members[j][0] for j in others)/weight
        best = None
        for j in own:
            r = correlation(M[j], prediction)
            gain = float(M[j] @ prediction/max(prediction @ prediction, 1e-30))
            if best is None or r > best[0]:
                best = (r, gain, j)
        r, gain, j = best
        loo[c] = dict(channels=sorted(int(k) for k, v in groups.items() if v == c),
                      r=round(r, 4), gain=round(gain, 4),
                      tls_gain=round(math.copysign(float(M[j].std()/max(prediction.std(), 1e-30)), r), 4),
                      hz=round(hz[j], 2), fm_percent=round(rms[j], 4))
    agree = [c for c, v in loo.items() if v['r'] >= w['loo_r'] and w['loo_gain'][0] <= v['gain'] <= w['loo_gain'][1]]
    families = []
    for c in agree:
        f = loo[c]['hz']
        if not any(same_source(f, k) or abs(f-k) < w['family_separation']*max(f, k) for k in families):
            families.append(f)
    total = sum(v for _, v in normalized_members.values())
    consensus = sum(v*e for e, v in normalized_members.values())/total
    if agree:
        best = sorted(agree, key=lambda c: -loo[c]['r'])[:int(w['top_members'])]
        target = float(np.median([loo[c]['fm_percent'] for c in best]))/100
        consensus = consensus*target/max(float(consensus.std()), 1e-12)
    fm = 100*float(consensus.std())
    # Real ~2 % RMS flutter peaks briefly above the 4.9 % correction bound;
    # clip like flutter_tracking.bound, but reject a broadly saturated estimate.
    peak = float(np.max(abs(consensus)))
    clipped = float(np.mean(abs(consensus) > g['speed_bound']))
    consensus = np.clip(consensus, -g['speed_bound'], g['speed_bound'])
    hum = bool(agree) and all(is_hum(loo[c]['hz']) for c in agree)
    gates = dict(
        agreement=len(agree) >= g['min_agreeing'],
        diversity=len(families) >= g['min_families'] or (hum and len(agree) >= g['hum_min_agreeing']),
        magnitude=(cluster_fm >= g['cluster_min_fm_percent']
                   and g['consensus_fm_percent'][0] <= fm <= g['consensus_fm_percent'][1]),
        speed_bound=clipped <= g['max_clipped_fraction'])
    record.update(seed_channel=int(pool[seed][0]['channel']), seed_hz=round(hz[seed], 2),
                  cluster_size=len(chosen), cluster_fm_percent=round(cluster_fm, 4),
                  leave_one_out={str(k): v for k, v in loo.items()}, agreeing_groups=agree,
                  agreeing_channels=sorted(ch for c in agree for ch in loo[c]['channels']),
                  families=families, hum_cluster=hum, consensus_fm_percent=round(fm, 4),
                  peak_speed_percent=round(100*peak, 4), clipped_fraction=round(clipped, 6),
                  gates=gates, eligible=all(gates.values()),
                  reasons=[name for name, passed in gates.items() if not passed])
    return record, consensus


def window_starts(read_start, read_end, settings):
    """Overlapping windows over one block's read interval, aligned to 1/RATE."""
    w = settings['window']
    length, step = w['seconds'], w['step_seconds']
    if read_end-read_start < length:
        return []
    starts = list(read_start+np.arange(0, read_end-read_start-length+1e-9, step))
    last = math.floor((read_end-length)*RATE+1e-9)/RATE
    if last-starts[-1] > 1e-6:
        starts.append(last)
    return [round(float(s), 6) for s in starts]


def audit_selected(tape, start, fraction):
    digest = hashlib.sha256(f'{tape}:{start:.3f}'.encode()).hexdigest()
    return int(digest[:8], 16)/2**32 < fraction


def shifted(curve, seconds):
    """Wrong-time control without wrapping samples around the window."""
    t = np.arange(len(curve))/RATE
    return np.interp(t-seconds, t, curve, left=0., right=0.)


def dewarp(x, e, sr, up=4):
    """Resample so a tone with fractional deviation e(t) becomes steady."""
    n = len(x)
    t = np.arange(n)/sr
    ee = np.interp(t, np.arange(len(e))/RATE, e)
    tau = np.concatenate([[0.], np.cumsum((1+ee[:-1])/sr)])
    uniform = np.arange(n)/sr*(tau[-1]/t[-1])
    times = np.interp(uniform, tau, t)
    xu = signal.resample_poly(x, up, 1)
    return np.interp(times*sr*up, np.arange(len(xu)), xu)


def concentration(x, hz, sr):
    nper = min(len(x), int(8*sr))
    f, p = signal.welch(x, sr, nperseg=nper, noverlap=nper//2)
    core = abs(f-hz) <= .25
    wide = abs(f-hz) <= max(4., .04*hz)
    return float(p[core].sum()/max(p[wide].sum(), 1e-30))


def beep_audit(snippets, window_start, curve_full, gate, sr, settings):
    """Held-out check-beep residuals under +curve, -curve and a time shift."""
    a = settings['audit']
    n = int(round(settings['window']['seconds']*sr))
    variants = dict(identity=np.zeros_like(curve_full), curve=bound(curve_full, gate),
                    wrong_sign=bound(-curve_full, gate),
                    shift=bound(shifted(curve_full, a['shift_seconds']), gate))
    rows = []
    maps = {}
    for channel, items in sorted(snippets.items()):
        events = []
        for event, first, audio in items:
            start = event['start']-(window_start-first['block_start'])
            end = event['end']-(window_start-first['block_start'])
            if not a['beep_margin_seconds'] < start < settings['window']['seconds']-a['beep_margin_seconds']:
                continue
            lo = int(round((first['snippet_start']-window_start)*sr))
            hi = lo+len(audio)
            if not events:
                x = np.zeros(n)
            x[max(0, lo):min(n, hi)] = audio[max(0, -lo):len(audio)-max(0, hi-n)]
            events.append(dict(start=start, end=end))
        if not events:
            continue
        if not maps:
            maps = {k: positions(v, sr, n) for k, v in variants.items()}
        results = {k: evaluate_events(x, sr, v, events, sample_positions=maps[k])
                   for k, v in variants.items()}
        for i, event in enumerate(events):
            margins = {k: results[k][i]['margins'] for k in results}
            if not all(margins.values()):
                continue
            rows.append(dict(channel=channel, start=round(window_start+event['start'], 3),
                             **{k: round(float(np.median([m['after_rms_hz'] for m in v])), 3)
                                for k, v in margins.items()}))
    if not rows:
        return dict(beeps=0)
    table = np.array([[r['identity'], r['curve'], r['wrong_sign'], r['shift']] for r in rows])
    base = np.maximum(table[:, 0], 1e-9)
    medians = np.median(table, 0)
    c = a['controls']
    summary = dict(beeps=len(rows), median_hz=dict(zip(['identity', 'curve', 'wrong_sign', 'shift'],
                                                       np.round(medians, 3).tolist())),
                   curve_ratio=round(float(np.median(table[:, 1]/base)), 4),
                   wrong_sign_ratio=round(float(np.median(table[:, 2]/base)), 4),
                   shift_ratio=round(float(np.median(table[:, 3]/base)), 4),
                   worse=int((table[:, 1] > table[:, 0]).sum()), events=rows)
    summary['controls_pass'] = bool(summary['curve_ratio'] < c['curve_ratio_below']
                                    and summary['wrong_sign_ratio'] > c['wrong_sign_above']
                                    and summary['shift_ratio'] >= c['shift_ratio_at_least'])
    return summary


def concentration_audit(record, excerpts, curve_full, low_rate, settings):
    """Dewarp witness for lines above ~200 Hz; never used for the hum."""
    a = settings['audit']
    lines = sorted((v for v in record.get('leave_one_out', {}).values()
                    if int(v['channels'][0]) in excerpts and v['hz'] >= a['concentration_min_hz']
                    and v['hz'] < settings['fast_max_hz']), key=lambda v: -v['r'])
    out = []
    for line in lines[:int(a['concentration_lines'])]:
        x = excerpts[int(line['channels'][0])]
        values = {k: concentration(dewarp(x, c, low_rate) if c is not None else x, line['hz'], low_rate)
                  for k, c in [('none', None), ('curve', curve_full), ('wrong_sign', -curve_full),
                               ('shift', shifted(curve_full, a['shift_seconds']))]}
        out.append(dict(channel=line['channels'][0], hz=line['hz'],
                        **{k: round(v, 4) for k, v in values.items()},
                        concentrates=values['curve'] > max(values['none'], values['wrong_sign'], values['shift'])))
    return out


def analyse_block(read, channels, tape, read_start, read_end, settings, known_duplicates=(), progress=None):
    """Detect, screen and audit every window of one block of all channels.

    ``read(channel)`` returns float32 canonical-original samples for
    [read_start, read_end) and the fraction of that interval the channel covers.
    Returns the JSON-ready block record and float32 consensus curves.
    """
    sr = int(settings['sample_rate'])
    w, a = settings['window'], settings['audit']
    low_rate = sr/int(settings['decimation'])
    starts = window_starts(read_start, read_end, settings)
    audits = {s for s in starts if audit_selected(tape, s, a['random_fraction'])}
    tracks, meta, prints, snippets, excerpts = {}, {}, {}, {}, {}
    for number, channel in enumerate(channels):
        x, coverage = read(channel)
        if coverage < .5 or not np.any(x):
            meta[str(channel)] = dict(skipped='silent or under half covered', coverage=round(coverage, 4))
            continue
        found, info, clean, beeps = channel_tracks(x, sr, settings)
        for m in found:
            m['channel'] = channel
        tracks[channel] = found
        meta[str(channel)] = dict(info, coverage=round(coverage, 4), tracks=len(found), beeps=len(beeps))
        prints[channel] = fingerprint(clean, low_rate, settings)
        half = a['beep_snippet_seconds']
        snippets[channel] = []
        for event in beeps:
            lo = max(0, int((event['start']-half)*sr))
            hi = min(len(x), int((event['end']+half)*sr))
            snippets[channel].append((event, dict(block_start=read_start, snippet_start=read_start+lo/sr),
                                      np.array(x[lo:hi], np.float64)))
        for s in audits:
            i0 = int(round((s-read_start)*low_rate))
            excerpts.setdefault(s, {})[channel] = clean[i0:i0+int(w['seconds']*low_rate)].copy()
        del x, clean
        if progress:
            progress(number+1, len(channels))
    groups = duplicate_groups(prints, settings['gates']['duplicate_r'], known_duplicates)
    length = int(round(w['seconds']*RATE))
    edge = int(w['edge_seconds']*RATE)
    windows, curves = [], []
    for start in starts:
        i0 = int(round((start-read_start)*RATE))
        members = [(m, m['e'][i0:i0+length], m['ok'][i0:i0+length]) for ch in tracks for m in tracks[ch]
                   if len(m['e']) >= i0+length]
        record, consensus = screen_window(members, groups, settings)
        record.update(start_seconds=start, end_seconds=round(start+w['seconds'], 6),
                      valid_start_seconds=round(start+w['edge_seconds'], 6),
                      valid_end_seconds=round(start+w['seconds']-w['edge_seconds'], 6),
                      random_audit=start in audits, curve_index=None)
        if consensus is not None:
            record['curve_index'] = len(curves)
            curves.append(consensus.astype(np.float32))
            if record['eligible'] or start in audits:
                full, gate = np.zeros(length), np.zeros(length)
                full[edge:length-edge], gate[edge:length-edge] = consensus, 1.
                record['beep_audit'] = beep_audit(snippets, start, full, gate, sr, settings)
                if start in audits:
                    record['concentration_audit'] = concentration_audit(
                        record, excerpts.get(start, {}), full, low_rate, settings)
        windows.append(record)
    shape = (len(curves), length-2*edge)
    array = np.array(curves, np.float32).reshape(shape)
    result = dict(channels=meta, duplicate_groups=sorted(
        [sorted(c for c in groups if groups[c] == root) for root in set(groups.values())
         if sum(v == root for v in groups.values()) > 1]),
        windows=windows, windows_eligible=sum(r['eligible'] for r in windows))
    return result, array


# ---------------------------------------------------------------- stage D

def taper(n, ramp):
    k = np.arange(n)
    x = np.minimum(1., np.minimum((k+.5)/ramp, (n-k-.5)/ramp))
    return .5-.5*np.cos(np.pi*x)


def join_windows(windows, duration, settings):
    """Overlap-add passing window consensuses; never extrapolate into gaps.

    ``windows`` holds (valid_start_seconds, curve). Returns speed and support
    on the canonical 200 Hz grid starting at tape second 0.
    """
    frames = int(math.floor(duration*RATE+1e-9))+1
    numerator, weight = np.zeros(frames), np.zeros(frames)
    ramp = settings['join']['taper_seconds']*RATE
    for start, curve in windows:
        i0 = int(round(start*RATE))
        if i0 < 0 or i0+len(curve) > frames:
            raise ValueError('Consensus window lies outside the tape')
        w = taper(len(curve), ramp)
        numerator[i0:i0+len(curve)] += w*curve
        weight[i0:i0+len(curve)] += w
    covered = weight > 0
    speed = np.zeros(frames)
    speed[covered] = numerator[covered]/weight[covered]
    support = covered.astype(float)
    if np.max(abs(speed)) > settings['gates']['speed_bound']:
        raise ValueError('Joined census speed exceeds the correction bound')
    return speed, support
