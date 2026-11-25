"""Measure fixed WAV time offsets from broadband matches distributed over each reel."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy import fft, ndimage, signal

from flutter_planning import inventory
from flutter_plan_store import planning_lock

ORIGINALS = Path('D:/Apollo_15_30-Track/orig_concatenated')
ROOT = Path('F:/Apollo_15_30-Track/deflutter_alignment')
RATE = 2000
SPAN = 48
SEARCH = 16
EPOCHS = (.04, .21, .42, .61, .79, .96)
# A few ms of analogue/communications path delay is common between monitored feeds.
TOL = .003
ZERO = .004


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


def source_key(inv):
    return digest([{k: r[k] for k in ('channel','path','frames','sample_rate','start_seconds','bytes','mtime_ns')}
                   for r in inv['channels']])


def measurements(rows, start):
    """Signed residual after filename alignment; positive means the second WAV event is later."""
    n = SPAN*RATE
    size = fft.next_fast_len(2*n)
    hz = fft.rfftfreq(size, 1/RATE)
    band = (hz >= 250) & (hz <= 920) & (abs(hz-60*np.round(hz/60)) > 2)
    window = signal.windows.tukey(n, .15).astype('f')
    spectra, prints = [], []
    for row in rows:
        sr = row['sample_rate']
        first = round((start-row['start_seconds'])*sr)
        with sf.SoundFile(row['path']) as f:
            f.seek(first)
            x = f.read(SPAN*sr, dtype='float32')
        if len(x) != SPAN*sr or not np.isfinite(x).all():
            raise ValueError('Invalid original audio in alignment window')
        prints.append(hashlib.sha256(x.tobytes()).hexdigest())
        x = signal.resample_poly(x, RATE, sr)
        v = fft.rfft(x*window, size)
        floor = ndimage.uniform_filter1d(abs(v), size=511)
        v *= band/np.maximum(np.maximum(abs(v), floor), 1e-7)
        spectra.append(v)
    spectra = np.array(spectra)
    limit = SEARCH*RATE
    edges = []
    for i in range(len(rows)-1):
        c = fft.irfft(spectra[i+1:]*spectra[i].conj(), size, axis=1)
        peaks = abs(np.concatenate((c[:, -limit:], c[:, :limit+1]), axis=1))
        for j, curve in enumerate(peaks, i+1):
            k = int(curve.argmax())
            strength = float(curve[k]/max(np.sqrt(np.mean(curve**2)), 1e-10))
            if strength < 12 or k in (0, 2*limit):
                continue
            # Reject repeated equal peaks from periodic signals and ambiguous copies.
            height = curve[k]
            curve[max(0,k-8):k+9] = 0
            if height < 1.3*curve.max():
                continue
            edges.append([rows[i]['channel'], rows[j]['channel'], (k-limit)/RATE, round(strength,1)])
    return dict(start_seconds=start, edges=edges, fingerprints=prints)


def mode(values):
    if not values:
        return None
    v = np.sort(values)
    counts = np.searchsorted(v, v+2*TOL, side='right')-np.arange(len(v))
    i = int(counts.argmax())
    n = int(counts[i])
    if n < 3 or n <= len(v)/2:
        return None
    return float(np.median(v[i:i+n]))


def decide(inv, windows):
    rows = inv['channels']
    channels = [r['channel'] for r in rows]
    # Identical tracks contribute just one independent reference.
    signatures = [tuple(w['fingerprints'][i] for w in windows) for i in range(len(rows))]
    independent = [ch for i,ch in enumerate(channels) if signatures.index(signatures[i]) == i]
    zero = Counter()
    by_epoch = []
    for w in windows:
        lags = {}
        for a,b,d,_ in w['edges']:
            lags[a,b], lags[b,a] = d,-d
            if abs(d) <= ZERO:
                zero[a,b] += 1
                zero[b,a] += 1
        by_epoch.append(lags)
    def allies(ch):
        return [j for j in independent if j == ch or zero[ch,j] >= 3]
    base = max(independent,key=lambda ch:len(allies(ch)))
    refs = allies(base)
    ends = {r['channel']:r['end_seconds'] for r in rows}
    ref_end = float(np.median([ends[c] for c in refs]))
    found = {}
    for row in rows:
        ch = row['channel']
        values = [mode([lags[r,ch] for r in refs if (r,ch) in lags]) for lags in by_epoch]
        valid = [x for x in values if x is not None]
        lag = float(np.median(valid)) if valid else None
        state, frames = 'unresolved', None
        if (len(refs) > len(independent)/2 and len(valid) >= 3 and
                all(any(x is not None for x in values[i:i+2]) for i in (0,2,4)) and
                max(abs(x-lag) for x in valid) <= TOL):
            end_delta = ends[ch]-ref_end
            if abs(lag) <= ZERO:
                state, frames = 'aligned', 0
            elif abs(lag-end_delta) <= TOL:
                # A speech delay alone can be a routed feed or an echo. Independent
                # file boundaries must corroborate a nonzero delivery offset.
                state, frames = 'offset', round(end_delta*row['sample_rate'])
        found[str(ch)] = dict(state=state, delayed_frames=frames, epoch_lags_seconds=values,
                              median_lag_seconds=lag, end_delta_seconds=round(ends[ch]-ref_end,7))
    unresolved = [int(ch) for ch,r in found.items() if r['state']=='unresolved']
    state = 'review' if unresolved else 'retrace' if any(r['delayed_frames'] for r in found.values()) else 'reuse'
    if len(independent)==1 and len(rows)>1:
        state='duplicate_feed'
    return dict(action=state, reference_channels=refs, channels=found, unresolved=unresolved)


def audit(folder, root):
    inv = inventory(folder)
    method = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    key = source_key(inv)
    destination = Path(root)/method[:16]/inv['tape']/(key+'.json')
    with planning_lock(destination):
        if destination.is_file():
            report = json.loads(destination.read_bytes())
            if report['method_sha256'] != method or report['source_key'] != key or report['tape'] != inv['tape']:
                raise ValueError(f'Alignment binding changed: {destination}')
        else:
            rows = inv['channels']
            first, last = max(r['start_seconds'] for r in rows), min(r['end_seconds'] for r in rows)-SPAN
            if last-first < SPAN*len(EPOCHS):
                raise ValueError('Insufficient common audio for independent alignment epochs')
            windows = [measurements(rows,round(first+(last-first)*fraction)) for fraction in EPOCHS]
            if source_key(inventory(folder)) != key:
                raise ValueError('Original audio changed during alignment')
            report = dict(schema=1,tape=inv['tape'],method_sha256=method,source_key=key,
                          search_seconds=SEARCH,windows=windows,**decide(inv,windows))
            destination.parent.mkdir(parents=True,exist_ok=True)
            pending = destination.with_suffix('.partial')
            pending.write_text(json.dumps(report,indent=1,allow_nan=False),encoding='utf-8')
            pending.replace(destination)
    return destination, report


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--originals',type=Path,default=ORIGINALS)
    p.add_argument('--root',type=Path,default=ROOT)
    group=p.add_mutually_exclusive_group(required=True)
    group.add_argument('--tape',action='append')
    group.add_argument('--all-tapes',action='store_true')
    args=p.parse_args()
    tapes=sorted(args.originals.glob('A15_T*')) if args.all_tapes else [args.originals/t for t in args.tape]
    for folder in tapes:
        path,r=audit(folder,args.root)
        print(f"{folder.name} {r['action']}  offsets={[(ch,v['delayed_frames']) for ch,v in r['channels'].items() if v['delayed_frames']]}  review={r['unresolved']}  {path}",flush=True)


if __name__=='__main__':
    main()
