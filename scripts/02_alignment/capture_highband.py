"""Native-rate high-band registration for channels with ambiguous low-frequency evidence."""
import hashlib
import json
from pathlib import Path

import numpy as np
from scipy import fft, ndimage, signal
import soundfile as sf

import alignment_confirmation as confirmation
import flutter_alignment as audit
from flutter_plan_store import planning_lock


def destination(root, inv):
    key = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:16]
    return Path(root).resolve()/key/inv['tape']/(audit.source_key(inv)+'.json')


def measure(inv, report, fraction):
    rows = inv['channels']
    rate = rows[0]['sample_rate']
    if any(r['sample_rate']!=rate for r in rows):
        raise ValueError('High-band alignment requires the same digitization rate')
    first = max(r['start_seconds'] for r in rows)
    last = min(r['end_seconds'] for r in rows)-32
    start = round(first+(last-first)*fraction)
    size = fft.next_fast_len(64*rate)
    hz = fft.rfftfreq(size,1/rate)
    mask = (hz>1350)&(hz<3400)&(abs(hz-60*np.round(hz/60))>4)
    spectra, seen = {}, set()
    for r in rows:
        x = sf.read(r['path'],start=round((start-r['start_seconds'])*rate),frames=32*rate,dtype='float32')[0]
        signature=hashlib.sha256(x.tobytes()).digest()
        if signature in seen and r['channel'] not in report['unresolved']:
            continue
        seen.add(signature)
        v=fft.rfft(x*signal.windows.tukey(len(x),.15).astype('float32'),size)
        spectra[r['channel']] = v*(mask/np.maximum(np.maximum(abs(v),ndimage.uniform_filter1d(abs(v),511)),1e-7))
    hits = {}
    for ch in report['unresolved']:
        matches = []
        for donor, v in spectra.items():
            if donor in report['unresolved']:
                continue
            c = fft.irfft(spectra[ch]*v.conj(),size)
            limit = audit.SEARCH*rate
            peaks = abs(np.r_[c[-limit:],c[:limit+1]])
            k = int(peaks.argmax())
            h = peaks[k]
            strength = h/max(np.sqrt(np.mean(peaks*peaks)),1e-12)
            peaks[max(0,k-40):k+41] = 0
            if strength>13 and h>1.4*peaks.max() and 0<k<2*limit:
                matches.append([donor,(k-limit)/rate,round(float(strength),1)])
        hits[str(ch)] = matches
    return dict(start_seconds=start,hits=hits)


def resolve(inv, report, epochs):
    rows = {r['channel']:r for r in inv['channels']}
    decisions = {k:dict(v) for k,v in report['channels'].items()}
    for ch in report['unresolved']:
        row = rows[ch]
        key = str(ch)
        boundaries, votes = [], []
        for w in epochs:
            offsets = []
            for donor,lag,_ in w['hits'][key]:
                anchor = report['channels'][str(donor)]['delayed_frames']/rows[donor]['sample_rate']
                offsets.append(lag+anchor)
                boundaries.append(row['end_seconds']-rows[donor]['end_seconds']+anchor)
            votes.append(audit.mode(offsets))
        valid = [v for v in votes if v is not None]
        if len(valid)<4 or votes[0] is None or votes[-1] is None or np.ptp(valid)>.004:
            continue
        lag = float(np.median(valid))
        delay = 0 if abs(lag)<=audit.ZERO else min(boundaries,key=lambda b:abs(b-lag))
        if abs(delay-lag)>.003:
            continue
        prior = [v for v in decisions[key]['epoch_lags_seconds'] if v is not None]
        if abs(delay)>audit.ZERO and (len(prior)<3 or max(abs(v-delay) for v in prior)>.012):
            continue
        frames = round(delay*row['sample_rate'])
        decisions[key].update(state='offset' if frames else 'aligned',delayed_frames=frames,
                              confirmation='native-rate high-band matches at independent epochs')
    held = [int(k) for k,v in decisions.items() if v['state']=='unresolved']
    return dict(channels=decisions, unresolved=held,
                action='review' if held else 'retrace' if any(v['delayed_frames'] for v in decisions.values()) else 'reuse')


def check(folder,root=audit.ROOT):
    parent, report = confirmation.check(folder,root)
    if report['action']!='review':
        return parent,report
    inv=audit.inventory(folder)
    path=destination(root,inv)
    with planning_lock(path):
        if not path.exists():
            epochs = [measure(inv,report,f) for f in (.12,.32,.52,.72,.91)]
            if audit.source_key(audit.inventory(folder)) != audit.source_key(inv):
                raise ValueError('Originals changed during high-band alignment')
            saved=dict(tape=inv['tape'],source_key=audit.source_key(inv),epochs=epochs,
                       parent_sha256=hashlib.sha256(parent.read_bytes()).hexdigest(),parent=str(parent),
                       **resolve(inv,report,epochs))
            path.parent.mkdir(parents=True,exist_ok=True)
            tmp=path.with_suffix('.partial')
            tmp.write_text(json.dumps(saved,indent=1,allow_nan=False),encoding='utf-8')
            tmp.replace(path)
        return path,json.loads(path.read_bytes())
