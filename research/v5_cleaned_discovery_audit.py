"""Diagnostic short-context search on already-denoised speech channels.

Consumes the fixed contexts from v5_discovery_audit.py. No correction fitting or
reserved-event evaluation. Spectral hypotheses cannot establish phone-system
origin, tape motion, or useful correction. Excludes +/-5 Hz of every 60 Hz
harmonic conservatively; this is an audit guard, not a qualified physical mask.
"""
import os
for name in ('OPENBLAS_NUM_THREADS', 'OMP_NUM_THREADS', 'MKL_NUM_THREADS'):
    os.environ.setdefault(name, '1')
import hashlib
import json
import sys
from pathlib import Path

import numpy as np
import soundfile as sf

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(REPO/'scripts'))
from _bootstrap import install as _install
_install()
from flutter_tracking import discover, trajectory
from carrier_capture import capture_candidates
from continuous_planning import competing_carriers
from denoise_archive import CANONICAL, historical_path

BASE = REPO/'reports/deflutter_discovery_audit_20261030'


def outside_mains(hz):
    return abs(hz/60-round(hz/60))*60 > 5


def main():
    prior = json.loads((BASE/'evidence.json').read_text())
    rows = []
    cleanup = []
    for tape in prior['tapes']:
        inv = json.loads((Path('D:/Apollo_15_30-Track/deflutter_planning_v5_allchannel')/
                         tape['tape']/'plan/plan.json').read_text())['inventory']
        sources = {r['channel']:r for r in inv['channels']}
        for ch in (3,14,22):
            contexts = [e for e in tape['excerpts'] if e['version']=='denoised' and e['channel']==ch]
            folder = historical_path(CANONICAL/tape['tape']/f'CH{ch:02d}')
            log_path = folder/'cleanup.jsonl'
            log_hash = hashlib.sha256(log_path.read_bytes()).hexdigest()
            metadata = json.loads((folder/'denoised.json').read_text())
            if log_hash != metadata['cleanup_log_sha256']:
                raise ValueError(f'Cleanup log hash mismatch {folder}')
            removed = []
            sr = sources[ch]['sample_rate']
            for line in log_path.open():
                entry = json.loads(line)
                overlaps = any(entry['end_frame']/sr > e['start'] and entry['start_frame']/sr < e['end'] for e in contexts)
                if not overlaps:
                    continue
                for pass_ in entry['tones']['passes']:
                    for tone in pass_['lines']:
                        if outside_mains(tone['hz']) and not 2150 < tone['hz'] < 2850:
                            removed.append(dict(start_seconds=entry['start_frame']/sr,
                                frequency_hz=tone['hz'],width_hz=tone['width_hz'],iteration=pass_['iteration']))
            cleanup.append(dict(tape=tape['tape'],channel=ch,log_sha256=log_hash,
                nonmains_notch_applications_in_overlapping_blocks=len(removed),examples=removed[:20]))
            for context in contexts:
                a,b = context['start'],context['end']
                with sf.SoundFile(folder/'denoised.wav') as stream:
                    stream.seek(round((a-sources[ch]['start_seconds'])*sr))
                    x = stream.read(round((b-a)*sr),dtype='float64')
                if hashlib.sha256(x.tobytes()).hexdigest()!=context['decoded_sha256']:
                    raise ValueError('Cleaned excerpt hash mismatch')
                full = [hz for hz in context['capped_frequencies'] if outside_mains(hz)]
                clusters = []
                starts = sorted(set([float(v) for v in np.arange(0,len(x)/sr-10+.001,5)] + [len(x)/sr-10]))
                for offset in starts:
                    lo = round(offset*sr)
                    for candidate in discover(x[lo:lo+10*sr],sr,4096):
                        hz = candidate['carrier_hz']
                        if not outside_mains(hz) or any(abs(hz-f)<15 for f in full):
                            continue
                        cluster = next((c for c in clusters if abs(c['first_frequency_hz']-hz)<8),None)
                        detection = dict(start_seconds=a+offset,end_seconds=a+offset+10,**candidate)
                        if cluster is None:
                            clusters.append(dict(first_frequency_hz=hz,detections=[detection]))
                        else:
                            cluster['detections'].append(detection)
                # Two candidates per context in detector traversal order. This
                # checks tracker support only, never predictive improvement.
                for cluster in clusters[:2]:
                    d = cluster['detections'][0]
                    lo = round((d['start_seconds']-a)*sr)
                    clip = x[lo:lo+10*sr]
                    measurements = []
                    for capture in capture_candidates(d['carrier_hz'],sr,(2.,30.)):
                        try:
                            trace = trajectory(clip,sr,d['carrier_hz'],capture)
                            measurements.append(dict(capture_hz=capture,support_fraction=float(np.mean(trace[5]>=.95)),
                                competing_peaks_hz=competing_carriers(clip,sr,d['carrier_hz'],capture)))
                        except ValueError as exc:
                            measurements.append(dict(capture_hz=capture,error=str(exc)))
                    cluster['cleaned_tracker_diagnostics']=measurements
                rows.append(dict(tape=tape['tape'],channel=ch,start=a,end=b,
                    full_context_nonmains_candidates=full,short_context_additional_clusters=clusters))
    measured = [c for r in rows for c in r['short_context_additional_clusters'] if 'cleaned_tracker_diagnostics' in c]
    result = dict(mains_guard_hz=5,short_context_seconds=10,stride_seconds=5,
        caveat='Additional spectral hypotheses may be speech harmonics or artifacts. Phone-line provenance and predictive correction have not been established. Tracker support is not validation.',
        summary=dict(contexts=len(rows),contexts_with_new_hypotheses=sum(bool(r['short_context_additional_clusters']) for r in rows),
            new_hypothesis_clusters=sum(len(r['short_context_additional_clusters']) for r in rows),
            clusters_given_tracker_diagnostics=len(measured),
            clusters_with_half_support_and_no_competitor=sum(any(m.get('support_fraction',0)>=.5 and not m.get('competing_peaks_hz') for m in c['cleaned_tracker_diagnostics']) for c in measured),
            nonmains_notch_applications=sum(r['nonmains_notch_applications_in_overlapping_blocks'] for r in cleanup)),
        cleanup_logs=cleanup,contexts=rows)
    (BASE/'cleaned_evidence.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(result['summary'],indent=2))


if __name__=='__main__':
    main()
