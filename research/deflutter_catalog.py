"""Research-only tone catalog. No target-event or correction evaluator imports.

FFT masks describe lost discovery resolution, not authenticated tone origin.
Lineage is source/time/frequency based; scores never determine identity.
"""
from dataclasses import asdict, dataclass
import hashlib
import json

import numpy as np
from scipy import ndimage, signal

VERSION = 'tone-catalog-research-v2'


@dataclass(frozen=True)
class CatalogConfig:
    resolutions: tuple = (.5, 2., 8.)
    excess_db: float = 12.
    minimum_seconds: float = 10.
    minimum_center_span_seconds: float = 10.
    maximum_gap_seconds: float = 1.
    response_band: tuple = (2150., 2850.)
    low_hz: float = 45.
    edge_hz: float = 100.
    mains_hz: float = 60.
    mains_floor_halfwidth_hz: float = .5
    background_max_samples: int = 81


def identity(source, start, end, resolution, hz):
    lineage = [source, round(start, 6), round(end, 6), resolution, round(hz, 6)]
    return hashlib.sha256(json.dumps(lineage).encode()).hexdigest()[:24]


def contamination_mask(frequencies, resolution, config):
    # Hann's first null is two FFT bins from its center. Protect at least the
    # existing 0.5 Hz uncertain mains center, never an arbitrary +/-5 Hz rule.
    width = max(config.mains_floor_halfwidth_hz, 2 / resolution)
    distance = abs(frequencies - np.round(frequencies/config.mains_hz)*config.mains_hz)
    return distance <= width, width


def discover_ridges(x, sr, config=CatalogConfig()):
    """Return per-resolution ridges plus complete peak disposition accounting.

    Independent original/cleaned calls are merged by the campaign adapter.
    No circuit name or expected useful carrier is an input. Short ridges remain
    accounted for but are not promoted to ten-second carrier hypotheses.
    """
    if x.ndim != 1 or not np.isfinite(x).all():
        raise ValueError('Expected finite mono samples')
    rows, accounting = [], []
    for resolution in config.resolutions:
        n = round(sr*resolution)
        if len(x) < n:
            accounting.append(dict(resolution_seconds=resolution, status='insufficient_context'))
            continue
        hop = max(1, n//4)
        f, times, p = signal.spectrogram(x, sr, window='hann', nperseg=n,
                                        noverlap=n-hop, detrend=False)
        df = sr/n
        # Background measured before suppressing contamination. A robust median
        # prevents an isolated loud line from raising every neighboring floor.
        width = max(7, round(80/df) | 1)
        # Evaluate the 80-Hz robust background on a coarser frequency grid.
        # The fine ridge/peak spectrum is unchanged. Record this approximation
        # as a new configuration, and compare on the declared synthetic draws.
        step = max(1, int(np.ceil(width/config.background_max_samples)))
        coarse = ndimage.median_filter(p[::step], size=(max(7, (width//step)|1), 1), mode='nearest')
        background = np.column_stack([np.interp(f, f[::step], coarse[:, col]) for col in range(p.shape[1])])
        excess = 10*np.log10((p+1e-30)/(background+1e-30))
        mask, mask_width = contamination_mask(f, resolution, config)
        allowed = (f > config.low_hz) & (f < sr/2-config.edge_hz)
        response = (f >= config.response_band[0]) & (f <= config.response_band[1])
        tracks, active = [], []
        counts = dict(raw_peaks=0, contaminated=0, response_band_withheld=0,
                      edge_excluded=0, eligible_peak_frames=0)
        for col, tm in enumerate(times):
            peaks, _ = signal.find_peaks(excess[:, col], height=config.excess_db)
            counts['raw_peaks'] += len(peaks)
            counts['edge_excluded'] += int(np.sum(~allowed[peaks]))
            counts['response_band_withheld'] += int(np.sum(allowed[peaks] & response[peaks]))
            counts['contaminated'] += int(np.sum(allowed[peaks] & ~response[peaks] & mask[peaks]))
            peaks = peaks[allowed[peaks] & ~response[peaks] & ~mask[peaks]]
            counts['eligible_peak_frames'] += len(peaks)
            # Greedy nearest-frequency assignment, one observation per ridge.
            # Tolerance follows FFT resolution plus a declared .4% local drift.
            # Exact same admissible links as the all-pairs implementation.
            # Search only the sorted frequency neighborhood; the slightly
            # larger bound follows d<=.004*f_peak algebraically, then the
            # original predicate is checked without approximation.
            links=[];peak_frequencies=f[peaks]
            for ri,r in enumerate(active):
                if tm-r['times'][-1]>config.maximum_gap_seconds+hop/sr:continue
                last=r['hz'][-1];bound=max(1.5*df,.004*last/(1-.004))
                left=np.searchsorted(peak_frequencies,last-bound,side='left')
                right=np.searchsorted(peak_frequencies,last+bound,side='right')
                for k in peaks[left:right]:
                    distance=abs(f[k]-last)
                    if distance<=max(1.5*df,.004*f[k]):links.append((distance,ri,int(k)))
            links.sort()
            used_r, used_k = set(), set()
            for _, ri, k in links:
                if ri in used_r or k in used_k:
                    continue
                r = active[ri]
                r['times'].append(float(tm)); r['hz'].append(float(f[k]))
                r['excess'].append(float(excess[k, col]))
                used_r.add(ri); used_k.add(k)
            expired = [r for r in active if tm-r['times'][-1] > config.maximum_gap_seconds+hop/sr]
            tracks.extend(expired)
            active = [r for r in active if tm-r['times'][-1] <= config.maximum_gap_seconds+hop/sr]
            active.extend(dict(times=[float(tm)], hz=[float(f[k])], excess=[float(excess[k, col])])
                          for k in peaks if int(k) not in used_k)
        tracks.extend(active)
        counts.update(ridges=len(tracks), short_ridges=0, retained_ridges=0)
        for r in tracks:
            # Support is a union of observed FFT-window footprints; intermittent
            # gaps larger than max-gap split ridges rather than bridge evidence.
            lo = max(0., r['times'][0]-resolution/2)
            hi = min(len(x)/sr, r['times'][-1]+resolution/2)
            support = min(hi-lo, len(r['times'])*hop/sr+resolution-hop/sr)
            # Overlapping FFT footprints are not independent persistence. Two
            # eight-second noise peaks must not become a ten-second carrier.
            if support < config.minimum_seconds or r['times'][-1]-r['times'][0] < config.minimum_center_span_seconds:
                counts['short_ridges'] += 1
                continue
            counts['retained_ridges'] += 1
            rows.append(dict(carrier_hz=float(np.median(r['hz'])), start_seconds=lo,
                end_seconds=hi, observed_support_seconds=support,
                occupancy=support/max(hi-lo, 1e-12), resolution_seconds=resolution,
                bin_width_hz=df, frequency_range_hz=[min(r['hz']), max(r['hz'])],
                spectral_score_db=float(np.median(r['excess'])),
                peak_frames=len(r['times']), status='spectral_hypothesis',
                mains_mask_halfwidth_hz=mask_width))
        accounting.append(dict(resolution_seconds=resolution, status='analyzed',
            frequency_bin_hz=df, background_grid_hz=step*df,
            mains_mask_halfwidth_hz=mask_width, **counts))
    return rows, accounting


def allocation_only(x, sr, legacy_discover, limit=8):
    """I001 isolates allocation: no new peaks, spacing, windows or tracking."""
    all_rows = legacy_discover(x, sr, max(1, int(sr/2)))
    usable = [r for r in all_rows if abs(r['carrier_hz']-round(r['carrier_hz']/60)*60) > .5]
    return usable[:limit], dict(spectral_hypotheses=len(all_rows),
        contaminated=len(all_rows)-len(usable), budget_deferred=max(0, len(usable)-limit))


def matching_truth(rows, truth, tolerance_hz=2.):
    """One truth interval may appear at multiple resolutions; count it once.

    Unmatched catalog rows are reported separately as hypotheses, not invented
    negative physical carriers. Synthetic precision merges near-identical rows.
    """
    hits = []
    for t in truth:
        hits.append(any(abs(r['carrier_hz']-t['hz']) <= max(tolerance_hz, r.get('bin_width_hz', .5))
                        and min(r.get('end_seconds', t['end']), t['end'])-
                        max(r.get('start_seconds', t['start']), t['start']) >= .8*(t['end']-t['start'])
                        for r in rows))
    groups = []
    for row in sorted(rows, key=lambda r: r.get('resolution_seconds', 2.), reverse=True):
        if not any(abs(row['carrier_hz']-g['carrier_hz']) <= max(tolerance_hz, row.get('bin_width_hz', .5), g.get('bin_width_hz', .5))
                   and min(row.get('end_seconds', 32), g.get('end_seconds', 32)) >
                   max(row.get('start_seconds', 0), g.get('start_seconds', 0)) for g in groups):
            groups.append(row)
    matched_groups = sum(any(abs(r['carrier_hz']-t['hz']) <= max(tolerance_hz, r.get('bin_width_hz', .5))
                             for t in truth) for r in groups)
    return dict(truth_count=len(truth), found=sum(hits), recall=sum(hits)/max(1, len(hits)),
                catalog_groups=len(groups), unmatched_groups=len(groups)-matched_groups,
                precision=matched_groups/max(1, len(groups)), hits=hits)
