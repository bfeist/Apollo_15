"""Independent low-frequency confirmation for inconclusive broadband capture-offset measurements."""
import hashlib
import json
from pathlib import Path

import numpy as np
from scipy import fft, ndimage, signal
import soundfile as sf

import flutter_alignment as audit
import flutter_census as census
from flutter_plan_store import planning_lock


def evidence_hash():
    h = hashlib.sha256(Path(__file__).read_bytes())
    h.update(Path(census.__file__).read_bytes())
    return h.hexdigest()


def destination(root, inv):
    return Path(root).resolve()/evidence_hash()[:16]/inv['tape']/(audit.source_key(inv)+'.json')


def carriers(row, start, seconds):
    sr = row['sample_rate']
    x = sf.read(row['path'], start=round((start-row['start_seconds'])*sr),
                frames=seconds*sr, dtype='float32')[0]
    settings = census.SETTINGS
    dec = settings['decimation']
    low = sr/dec
    xd = signal.resample_poly(x,1,dec).astype('float64')
    lines = census.exact_mains(xd,low,settings)
    clean = census.remove_stationary(xd,lines,low,settings['mains']['block_seconds'])
    spectrum = census.BlockSpectrum(clean,low)
    return [census.track(spectrum,hz,settings['tracking'])['e'] for hz in (59,118)]


def white(v, size):
    spectrum = fft.rfft(v*signal.windows.tukey(len(v),.15),size)
    hz = fft.rfftfreq(size,1/census.RATE)
    envelope = ndimage.maximum_filter1d(abs(spectrum),201)
    return spectrum*((hz>2)&(hz<23))/np.maximum(envelope,1e-7)


def lag_measurements(curves, reference):
    size = fft.next_fast_len(2*len(reference))
    ref = white(reference,size)
    limit = audit.SEARCH*census.RATE
    result = []
    for curve in curves:
        c = fft.irfft(white(curve,size)*ref.conj(),size)
        c = np.r_[c[-limit:],c[:limit+1]]
        k = int(c.argmax())
        h = c[k]
        score = h/max(np.sqrt(np.mean(c*c)),1e-12)
        c[max(0,k-4):k+5] = 0
        ratio = h/max(c.max(),1e-12)
        if score >= 5 and ratio >= 1.25 and 0 < k < 2*limit:
            result.append(dict(lag=(k-limit)/census.RATE,score=round(float(score),2),
                               peak_ratio=round(float(ratio),2)))
    return result


def resolve(report, epochs, inv):
    rows = {r['channel']:r for r in inv['channels']}
    decisions = {k:dict(v) for k,v in report['channels'].items()}
    for ch in report['unresolved']:
        r = decisions[str(ch)]
        end = r['end_delta_seconds']
        # Blind FM peaks must agree across independent passages in the first and
        # last thirds. Require a second, broadband clock measurement for a shift.
        proposed = [0.]
        if abs(end) > .01:
            proposed.append(end)
        accepted = []
        for offset in proposed:
            matches = [any(abs(p['lag']-offset)<=.006 for p in w['matches'][str(ch)]) for w in epochs]
            if not (sum(matches)>=3 and any(matches[:2]) and any(matches[-2:])):
                continue
            votes = [v for v in r['epoch_lags_seconds'] if v is not None]
            if offset and (len(votes)<3 or max(abs(v-offset) for v in votes)>.009):
                continue
            # Strong contrary *clock* observations cannot be resolved by a majority.
            if any(abs(p['lag']-offset)>.012 and p['score']>=8 and p['peak_ratio']>=1.7
                   for w in epochs for p in w['matches'][str(ch)]):
                continue
            accepted.append(offset)
        if len(accepted)==1:
            delay = round(accepted[0]*rows[ch]['sample_rate'])
            r.update(state='offset' if delay else 'aligned', delayed_frames=delay,
                     confirmation='independent held-out carrier motion')
    # Relative end times can differ even among synchronized reference tracks.
    # Test every actual boundary difference using independent broadband and
    # carrier observations. Never infer the start from the end time alone.
    refs = [k for k in report['reference_channels'] if decisions[str(k)]['state']=='aligned']
    for ch in report['unresolved']:
        r = decisions[str(ch)]
        if r['state']!='unresolved':
            continue
        votes = [v for v in r['epoch_lags_seconds'] if v is not None]
        boundary = [rows[ch]['end_seconds']-rows[k]['end_seconds'] for k in refs]
        if len(votes)>=3 and boundary and abs(np.median(votes))>.02:
            proposed = min(boundary,key=lambda v:abs(v-np.median(votes)))
            matched = [any(abs(p['lag']-proposed)<=.006 for p in e['matches'][str(ch)]) for e in epochs]
            if (abs(proposed-np.median(votes))<=.003 and max(abs(v-proposed) for v in votes)<=.009
                    and sum(matched)>=3 and any(matched[:3]) and any(matched[3:])):
                r.update(state='offset',delayed_frames=round(proposed*rows[ch]['sample_rate']),
                         confirmation='measured broadband and held-out motion agree on a source boundary')
    # A channel can verify its registration against sources already independently
    # registered. Each supporting match is a distinctive blind full-range peak.
    anchors = {int(k):v['delayed_frames']/rows[int(k)]['sample_rate'] for k,v in decisions.items()
               if v['state']!='unresolved'}
    for ch in report['unresolved']:
        r = decisions[str(ch)]
        if r['state']!='unresolved':
            continue
        candidates = {0.}
        # A nonzero bank start requires both a previously verified member at that
        # delay and matching file boundaries. Test its audio independently below.
        candidates.update(d for k,d in anchors.items() if d and abs(rows[k]['end_seconds']-rows[ch]['end_seconds'])<.003)
        supported = []
        for d in candidates:
            voters = set()
            good = []
            for win in report['windows']:
                hits = set()
                opposing = set()
                for a,b,lag,_ in win['edges']:
                    if ch not in (a,b):
                        continue
                    k,offset = (a,lag) if b==ch else (b,-lag)
                    if k not in anchors:
                        continue
                    observed = anchors[k]+offset
                    if abs(observed-d)<=audit.TOL:
                        hits.add(k)
                    elif abs(observed-d)>.012:
                        opposing.add(k)
                # Speech routing can produce an extra match about 1.3 s away.
                good.append(bool(hits) and len(hits)>len(opposing))
                if good[-1]:
                    voters.update(hits)
            broadband = [v for v in r['epoch_lags_seconds'] if v is not None]
            conflicts = any(abs(v-d)>.012 for v in broadband)
            if (len(voters)>=2 and all(any(good[i:i+2]) for i in (0,2,4)) and not conflicts and
                    not any(abs(p['lag']-d)>.012 and p['score']>=8 and p['peak_ratio']>=1.7
                            for e in epochs for p in e['matches'][str(ch)])):
                supported.append(d)
        if len(supported)==1:
            delay = round(supported[0]*rows[ch]['sample_rate'])
            r.update(state='offset' if delay else 'aligned',delayed_frames=delay,
                     confirmation='repeatable full-range matches to independently registered channels')
    held = [int(k) for k,v in decisions.items() if v['state']=='unresolved']
    return dict(channels=decisions, unresolved=held,
                action='review' if held else 'retrace' if any(r['delayed_frames'] for r in decisions.values()) else 'reuse')


def refine(root, inv, primary, report):
    path = destination(root,inv)
    with planning_lock(path):
        if path.exists():
            return path,json.loads(path.read_bytes())
        rows = {r['channel']:r for r in inv['channels']}
        anchors = [ch for ch in report['reference_channels'] if report['channels'][str(ch)]['state']=='aligned'
                   and rows[ch]['role'] != 'timing_only']
        anchors = [anchors[i] for i in np.unique(np.linspace(0,len(anchors)-1,min(7,len(anchors))).astype(int))]
        if len(anchors)<3:
            return primary,report
        first = max(r['start_seconds'] for r in rows.values())
        last = min(r['end_seconds'] for r in rows.values())-64
        epochs = []
        for fraction in (.08,.26,.46,.66,.84,.93):
            start = round(first+(last-first)*fraction)
            reference = np.median([carriers(rows[ch],start,64) for ch in anchors],axis=(0,1))
            hits = {str(ch):lag_measurements(carriers(rows[ch],start,64),reference) for ch in report['unresolved']}
            epochs.append(dict(start_seconds=start,matches=hits))
        enriched = dict(schema=1,tape=inv['tape'],source_key=audit.source_key(inv),
                        method_sha256=evidence_hash(),primary=dict(path=str(primary.resolve()),
                        sha256=hashlib.sha256(primary.read_bytes()).hexdigest()),anchors=anchors,epochs=epochs,
                        **resolve(report,epochs,inv))
        if audit.source_key(audit.inventory(inv['input'])) != enriched['source_key']:
            raise ValueError('Originals changed during carrier confirmation')
        path.parent.mkdir(parents=True,exist_ok=True)
        tmp = path.with_suffix('.partial')
        tmp.write_text(json.dumps(enriched,indent=1,allow_nan=False),encoding='utf-8')
        tmp.replace(path)
        return path,enriched


def check(folder, root=audit.ROOT):
    path, report = audit.audit(folder,root)
    return refine(root,audit.inventory(folder),path,report) if report['action']=='review' else (path,report)
