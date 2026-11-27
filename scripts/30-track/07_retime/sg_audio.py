"""Audio refinement at text-located windows and Quindar matching to the SG set.

docs/20_2026-11-25_sg_retime_30track.md, method steps 4 and 6.

Audio refinement resamples a 40 s window of a 30-track 05b channel onto the GET axis predicted by
the text curve and that channel's stagger, and correlates it with the SG reference audio (NARA
mix on IRIG time, or the IA reel through its drift map) by GCC-PHAT in 300-1,800 Hz over +/-5 s.
With ``x(G)`` the channel and ``y(G)`` the reference, the lag ``delta`` maximizing
``sum x(G) y(G + delta)`` is the curve's error: ``Phi(t_ref) = predicted GET + delta``.

Quindar bursts are detected with the stage-06 detector (``quindar_speed.detect``) on the 05b
channels and on the SG audio. NARA bursts are on IRIG time and calibrate the tone generator. Each
30-track burst is a rate witness (its pitch over the calibrated or matched NARA pitch); matched
positions are an independent check and never enter the position fit.
"""
from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy import ndimage, signal

from sg_inputs import read_json, source_get, source_seconds, segment_at_get, write_json
from sg_match import fitted_segment

AUDIO = dict(
    rate=8000,
    window_s=40.0,
    max_lag_s=5.0,
    band_hz=(300.0, 1800.0),
    taper=0.1,
    spacing_s=60.0,
    min_events=3,
    per_group=2,
    min_ratio=9.0,
    max_second_ratio=0.7,
    second_exclusion_s=0.2,
    agree_s=0.02,
    sigma_s=0.02,
    workers=8,
)

QUINDAR = dict(
    block_s=60.0,
    context_s=1.0,
    rate=8000,
    match_tolerance_s=0.25,
    duplicate_gap_s=1.6,
    rate_sigma=6e-4,
    min_family=30,
    nominal_hz=(2475.0, 2525.0),
)


# ------------------------------------------------------------------ GCC-PHAT
def gcc_phat(x, y, sr, max_lag_s, band=(300.0, 1800.0)):
    """Lag ``delta`` (s) maximizing sum x[n] y[n + (delta + max_lag) * sr]; ``len(y) = len(x) + 2 max_lag sr``."""
    n = int(2 ** np.ceil(np.log2(len(x) + len(y))))
    X = np.fft.rfft(x, n)
    Y = np.fft.rfft(y, n)
    R = np.conj(X) * Y
    f = np.fft.rfftfreq(n, 1 / sr)
    R = np.where((f >= band[0]) & (f <= band[1]), R / np.maximum(np.abs(R), 1e-20), 0.0)
    cc = np.fft.irfft(R, n)[:int(round(2 * max_lag_s * sr)) + 1]
    a = np.abs(cc)
    k = int(np.argmax(a))
    frac = 0.0
    if 0 < k < len(a) - 1:
        y0, y1, y2 = a[k - 1], a[k], a[k + 1]
        den = y0 - 2 * y1 + y2
        frac = 0.5 * (y0 - y2) / den if den < 0 else 0.0
    lag = (k + frac) / sr - max_lag_s
    median = float(np.median(a)) or 1e-20
    excl = int(round(AUDIO['second_exclusion_s'] * sr))
    rest = np.r_[a[:max(0, k - excl)], a[k + excl + 1:]]
    second = float(rest.max()) if len(rest) else 0.0
    return dict(lag_s=float(lag), ratio=float(a[k] / median), second_ratio=float(second / a[k]) if a[k] else 1.0,
                polarity=int(np.sign(cc[k])))


def bandpass(x, sr, band):
    sos = signal.butter(4, band, btype='bandpass', fs=sr, output='sos')
    return signal.sosfiltfilt(sos, x)


# -------------------------------------------------------------------- readers
def read_resampled(path, sr_file, positions_s, channels_mean=True, lowpass_hz=None):
    """Samples of ``path`` at arbitrary file seconds (cubic), with an optional anti-alias low-pass."""
    pos = np.asarray(positions_s, float) * sr_file
    lo = max(0, int(np.floor(pos.min())) - 256)
    hi = int(np.ceil(pos.max())) + 257
    with sf.SoundFile(path) as f:
        hi = min(hi, len(f))
        if hi - lo < 16:
            raise ValueError('window outside the file')
        f.seek(lo)
        x = f.read(hi - lo, dtype='float32', always_2d=True)
    x = x.mean(axis=1) if channels_mean else x[:, 0]
    x = x.astype(float)
    if lowpass_hz and lowpass_hz < sr_file / 2:
        x = signal.sosfiltfilt(signal.butter(8, lowpass_hz, fs=sr_file, output='sos'), x)
    return ndimage.map_coordinates(x, [pos - lo], order=3, mode='nearest')


class ReferenceAudio:
    """SG audio sampled on a GET grid."""

    def __init__(self, source):
        self.source = source
        info = sf.info(source['audio'])
        self.sr = info.samplerate
        self.path = source['audio']

    def segment(self, g_lo, g_hi):
        return segment_at_get(self.source, g_lo, g_hi)

    def on_get(self, grid, segment):
        s = source_seconds(self.source, segment, grid)
        return read_resampled(self.path, self.sr, s, lowpass_hz=AUDIO['rate'] / 2 * 0.9)


def channel_on_get(channel, curve, stagger, g_grid, g_c, t_ref_c):
    """The 05b channel resampled onto the predicted GET axis around ``t_ref_c``."""
    r = float(curve.rate(np.array([t_ref_c]))[0])
    t = t_ref_c + stagger + (g_grid - g_c) / r
    return read_resampled(channel['wav'], channel['sample_rate'], t - channel['origin_s'])


def refine_window(channel, curve, stagger, t_ref_c, ref: ReferenceAudio, settings=AUDIO, b=None):
    """One correlation. IA audio is placed by its drift map, whose GET is ``Phi + b_k``: the
    prediction is made in that frame, so the event ``(t_ref, predicted + delta)`` keeps ``b_k``."""
    sr = settings['rate']
    half = settings['window_s'] / 2
    phi = float(curve.get(np.array([t_ref_c]))[0])
    lag = settings['max_lag_s']
    seg = ref.segment(phi - half - lag, phi + half + lag)
    if seg is None:
        return None
    bkey = ref.source['segments'][seg]['key'] if fitted_segment(ref.source['segments'][seg], ref.source['kind']) else None
    g_c = phi + (b or {}).get(bkey, 0.0) if bkey else phi
    seg = ref.segment(g_c - half - lag, g_c + half + lag)
    if seg is None:
        return None
    gx = g_c - half + np.arange(int(settings['window_s'] * sr)) / sr
    gy = g_c - half - lag + np.arange(int((settings['window_s'] + 2 * lag) * sr)) / sr
    x = channel_on_get(channel, curve, stagger, gx, g_c, t_ref_c)  # x(G) is the channel at Phi^-1(G - b)
    y = ref.on_get(gy, seg)
    x = bandpass(x, sr, settings['band_hz']) * signal.windows.tukey(len(x), settings['taper'])
    y = bandpass(y, sr, settings['band_hz']) * signal.windows.tukey(len(y), settings['taper'])
    out = gcc_phat(x, y, sr, lag, settings['band_hz'])
    out.update(channel=channel['channel'], t_ref=float(t_ref_c), stagger_s=float(stagger), predicted_get=g_c,
               ref_source=ref.source['name'], ref_kind=ref.source['kind'], ref_segment=int(seg),
               bkey=ref.source['segments'][seg]['key'] if fitted_segment(ref.source['segments'][seg], ref.source['kind']) else None,
               rate=float(curve.rate(np.array([t_ref_c]))[0]))
    out['accepted'] = out['ratio'] >= settings['min_ratio'] and out['second_ratio'] <= settings['max_second_ratio']
    return out


def choose_windows(pairs, keep, stagger, groups, pair_group, settings=AUDIO):
    """Window centres (reference time) every ``spacing_s`` where text events cluster, and per stagger
    group the channels with most kept pairs near each centre."""
    use = keep & np.isfinite(stagger)
    t_ref = pairs['t'] - np.where(np.isfinite(stagger), stagger, 0.0)
    out = []
    if not use.any():
        return out
    spacing = settings['spacing_s']
    bins = np.floor(t_ref[use] / spacing).astype(int)
    idx_use = np.flatnonzero(use)
    for b in np.unique(bins):
        members = idx_use[bins == b]
        if len(members) < settings['min_events']:
            continue
        centre = float(np.median(t_ref[members]))
        near = idx_use[np.abs(t_ref[idx_use] - centre) <= settings['window_s'] / 2]
        for gi in range(len(groups)):
            sel = near[pair_group[near] == gi]
            if not len(sel):
                continue
            counts = {}
            for i in sel:
                counts.setdefault(int(pairs['channel'][i]), []).append(float(stagger[i]))
            ranked = sorted(counts.items(), key=lambda kv: -len(kv[1]))[:settings['per_group']]
            for ch, values in ranked:
                if len(values) >= 2:
                    out.append(dict(t_ref=centre, channel=ch, group=gi, stagger_s=float(np.median(values))))
    return out


def refine(windows, channels, curve, refs, settings=AUDIO, progress=None, b=None):
    """Run every (window, channel, reference) correlation; results in input order."""
    by_channel = {c['channel']: c for c in channels}
    jobs = [(w, r) for w in windows for r in refs]

    def one(job):
        w, ref = job
        try:
            res = refine_window(by_channel[w['channel']], curve, w['stagger_s'], w['t_ref'], ref, settings, b)
        except (ValueError, RuntimeError, OSError) as error:
            return dict(channel=w['channel'], t_ref=w['t_ref'], ref_source=ref.source['name'], error=str(error),
                        accepted=False)
        if res is not None:
            res['group'] = w['group']
        return res

    out = []
    with ThreadPoolExecutor(settings['workers']) as pool:
        for i, res in enumerate(pool.map(one, jobs)):
            if res is not None:
                out.append(res)
            if progress and i % 200 == 0:
                progress(f'  audio windows {i}/{len(jobs)}')
    return out


def audio_events(results, reference_group, settings=AUDIO):
    """Per window and reference: agreeing reference-group lags -> one position anchor; other
    groups' lags against it -> refined stagger."""
    keyed = {}
    for r in results:
        if r.get('accepted'):
            keyed.setdefault((round(r['t_ref'], 3), r['ref_source'], r['ref_segment']), []).append(r)
    events, staggers = [], []
    for (t_ref, src, seg), rows in sorted(keyed.items()):
        ref_rows = [r for r in rows if r['group'] == reference_group]
        delta = None
        if ref_rows:
            lags = np.array([r['lag_s'] for r in ref_rows])
            med = float(np.median(lags))
            agree = np.abs(lags - med) <= settings['agree_s']
            if agree.sum() >= max(1, (len(lags) + 1) // 2):
                delta = float(np.median(lags[agree]))
                r0 = ref_rows[0]
                events.append(dict(t=t_ref, g=r0['predicted_get'] + delta, sigma_s=settings['sigma_s'],
                                   channels=sorted(r['channel'] for r, a in zip(ref_rows, agree) if a),
                                   agreement_s=float(np.ptp(lags[agree])) if agree.sum() > 1 else None,
                                   ref_kind=r0['ref_kind'], bkey=r0['bkey'], source=src, delta_s=delta,
                                   ratio=float(np.median([r['ratio'] for r in ref_rows]))))
        for r in rows:
            if r['group'] != reference_group and delta is not None:
                staggers.append(dict(channel=r['channel'], t_ref=t_ref, text_stagger_s=r['stagger_s'],
                                     audio_stagger_s=r['stagger_s'] + (delta - r['lag_s']) / r['rate'],
                                     source=src))
    return events, staggers


# -------------------------------------------------------------------- Quindar
def scan_quindar(path, settings=QUINDAR):
    """Stage-06 detector over a whole file, resampled to 8 kHz when needed. Source seconds."""
    from quindar_speed import detect
    events = []
    with sf.SoundFile(path) as f:
        sr = f.samplerate
        total = len(f) / sr
        for a in np.arange(0.0, total, settings['block_s']):
            left = max(0.0, a - settings['context_s'])
            right = min(total, a + settings['block_s'] + settings['context_s'])
            f.seek(int(round(left * sr)))
            x = f.read(int(round((right - left) * sr)), dtype='float32', always_2d=True).mean(axis=1).astype(float)
            if sr != settings['rate']:
                g = np.gcd(int(sr), settings['rate'])
                x = signal.resample_poly(x, settings['rate'] // g, int(sr) // g)
            if len(x) < settings['rate']:
                continue
            events.extend(detect(x, settings['rate'], left, a, min(a + settings['block_s'], total)))
    return events


def cached_scan(path, cache_dir, identity, settings=QUINDAR):
    """Scan once per exact file identity; the cache file is never rewritten."""
    key = hashlib.sha256(json.dumps(dict(identity=identity, settings=settings), sort_keys=True).encode()).hexdigest()[:20]
    cache = Path(cache_dir) / f'{key}.json'
    if cache.is_file():
        return read_json(cache)['events']
    events = scan_quindar(path, settings)
    write_json(cache, dict(path=str(path), identity=identity, settings=settings, events=events))
    return events


def families(freq, nominal=QUINDAR['nominal_hz']):
    """0 = unkey (lower), 1 = key (upper), split between the two families by 2-means."""
    freq = np.asarray(freq, float)
    centres = np.array(nominal, float)
    for _ in range(30):
        label = np.argmin(np.abs(freq[:, None] - centres), axis=1)
        new = np.array([np.median(freq[label == k]) if (label == k).any() else centres[k] for k in (0, 1)])
        if np.allclose(new, centres):
            break
        centres = new
    return label, centres


def generator_calibration(nara_events, settings=QUINDAR):
    """Key/unkey frequency per NARA tape on IRIG time and the mission median of populated tapes."""
    from quindar_speed import quality_events
    rows = []
    for tape, events in sorted(nara_events.items()):
        q = quality_events(events)
        if not q:
            continue
        label, _ = families([e['frequency_hz'] for e in q])
        f = np.array([e['frequency_hz'] for e in q])
        row = dict(tape=tape, bursts=len(q))
        for k, name in ((0, 'unkey'), (1, 'key')):
            sel = f[label == k]
            row[f'{name}_count'] = int(len(sel))
            row[f'{name}_hz'] = float(np.median(sel)) if len(sel) else None
            row[f'{name}_mad_hz'] = float(np.median(np.abs(sel - np.median(sel)))) if len(sel) else None
        rows.append(row)
    populated = [r for r in rows if r['key_count'] >= settings['min_family'] and r['unkey_count'] >= settings['min_family']]
    if populated:
        key = [r['key_hz'] for r in populated]
        unkey = [r['unkey_hz'] for r in populated]
        mission = dict(key_hz=float(np.median(key)), unkey_hz=float(np.median(unkey)),
                       key_range_hz=[float(min(key)), float(max(key))],
                       unkey_range_hz=[float(min(unkey)), float(max(unkey))], tapes=[r['tape'] for r in populated],
                       basis='NARA bursts on IRIG time')
    else:
        mission = dict(key_hz=settings['nominal_hz'][1], unkey_hz=settings['nominal_hz'][0], tapes=[],
                       basis='nominal (no populated NARA tape)')
    return dict(tapes=rows, mission=mission)


def sg_bursts(source, events):
    """Quality bursts of one SG source on GET (bursts outside its segments dropped)."""
    from quindar_speed import quality_events
    q = quality_events(events)
    if not q:
        return []
    centre = np.array([(e['start_seconds'] + e['end_seconds']) / 2 for e in q])
    get, seg = source_get(source, centre)
    label, _ = families([e['frequency_hz'] for e in q])
    out = []
    for e, g, k, fam in zip(q, get, seg, label):
        if k >= 0:
            out.append(dict(get=float(g), frequency_hz=e['frequency_hz'], family=int(fam), source=source['name'],
                            kind=source['kind'], segment=int(k)))
    return out


def tape_bursts(channels_events, channels):
    """Quality bursts of every channel on canonical tape seconds."""
    from quindar_speed import quality_events
    by = {c['channel']: c for c in channels}
    out = []
    for ch, events in channels_events.items():
        for e in quality_events(events):
            out.append(dict(channel=ch, t=by[ch]['origin_s'] + (e['start_seconds'] + e['end_seconds']) / 2,
                            frequency_hz=e['frequency_hz'], residual_fm_sd_hz=e['residual_fm_sd_hz']))
    out.sort(key=lambda b: b['t'])
    return out


def match_quindar(bursts, curve, groups, references, calibration, settings=QUINDAR, b=None):
    """Classify, match to SG bursts under each stagger group, and collapse to rate witnesses."""
    b = b or {}
    cal = (calibration['unkey_hz'], calibration['key_hz'])
    ref = sorted(references, key=lambda r: r['get'])
    # SG bursts in the curve's frame: an IA drift-map GET is Phi + b_k.
    ref_get = np.array([r['get'] - (b.get(f"ia:{r['source']}:{r['segment']}", 0.0) if r['kind'] == 'ia' else 0.0)
                        for r in ref])
    order = np.argsort(ref_get, kind='stable')
    ref, ref_get = [ref[i] for i in order], ref_get[order]
    for x in bursts:
        r = float(curve.rate(np.array([x['t']]))[0])
        x['family'] = int(np.argmin([abs(x['frequency_hz'] / r - c) for c in cal]))
        x['curve_rate'] = r
        best = None
        for gi, gd in enumerate(groups):
            g_pred = float(curve.get(np.array([x['t'] - gd]))[0])
            lo, hi = np.searchsorted(ref_get, [g_pred - settings['match_tolerance_s'], g_pred + settings['match_tolerance_s']])
            for j in range(lo, hi):
                if ref[j]['family'] != x['family']:
                    continue
                res = float(ref_get[j] - g_pred)
                if best is None or abs(res) < abs(best[2]):
                    best = (gi, j, res)
        if best:
            gi, j, res = best
            x.update(matched_group=gi, residual_s=res, ref_source=ref[j]['source'], ref_kind=ref[j]['kind'],
                     ref_frequency_hz=ref[j]['frequency_hz'])
    # Rate witnesses: duplicates across channels (same family, close in time) count once.
    witnesses, current = [], []

    def flush():
        if not current:
            return
        matched = [c for c in current if c.get('ref_kind') == 'nara']
        f30 = float(np.median([c['frequency_hz'] for c in current]))
        if matched:
            f_true = float(np.median([c['ref_frequency_hz'] for c in matched]))
            basis = 'matched_nara'
        else:
            f_true = cal[current[0]['family']]
            basis = 'calibrated_generator'
        witnesses.append(dict(t=float(np.median([c['t'] for c in current])), rate=f30 / f_true, family=current[0]['family'],
                              copies=len(current), basis=basis, sigma=settings['rate_sigma']))

    for x in sorted(bursts, key=lambda x: (x['t'])):
        if current and (x['t'] - current[-1]['t'] > settings['duplicate_gap_s'] or x['family'] != current[0]['family']):
            flush()
            current = []
        current.append(x)
    flush()
    return bursts, witnesses


def quindar_check(bursts):
    """Position check per channel and matched group: residual median and MAD (never fitted)."""
    out = []
    keys = sorted({(x['channel'], x.get('matched_group')) for x in bursts if 'residual_s' in x})
    for ch, gi in keys:
        r = np.array([x['residual_s'] for x in bursts if x['channel'] == ch and x.get('matched_group') == gi])
        out.append(dict(channel=ch, group=gi, matches=int(len(r)), median_s=float(np.median(r)),
                        mad_s=float(np.median(np.abs(r - np.median(r)))),
                        within_0_25_s=float(np.mean(np.abs(r) <= 0.25))))
    totals = dict(bursts=len(bursts), matched=sum('residual_s' in x for x in bursts))
    return out, totals
