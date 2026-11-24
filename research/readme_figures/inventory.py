"""Recorder inventory and mission coverage for the README executive summary.

Reads filename time ranges and WAV headers from orig_concatenated, plus the
stage-05 analysis windows for the on-tape hum frequency. Writes inventory.json
beside this script. Times use the 1971 day-of-year clock in the filenames.
"""
import json
import re
from collections import defaultdict
from pathlib import Path

import sys

import numpy as np
import soundfile as sf

sys.path.insert(0, str(Path(__file__).resolve().parents[2]/'scripts'))
from _bootstrap import install  # noqa: E402
install()

ROOT = Path('D:/Apollo_15_30-Track')
OUT = Path(__file__).with_name('inventory.json')
STATS = json.loads(Path(__file__).with_name('stats.json').read_text(encoding='utf-8'))
LAUNCH = 207*86400 + 13*3600 + 34*60 + 0.6         # 1971-07-26 13:34:00.6 UTC
SPLASHDOWN = 219*86400 + 20*3600 + 45*60 + 53      # 1971-08-07 20:45:53 UTC
# Reel photographed, never delivered as audio (box note: July 26 1971, 0130-1805).
UNDELIVERED = {'A15_T510074': ('HR2U', 207*86400 + 1*3600 + 30*60, 207*86400 + 18*3600 + 5*60)}
NO_SIGNAL = {'A15_T510096'}     # 30 byte-identical tracks without program audio
UNPLACED = {'A15_T510087', 'A15_T510111'}  # box dates July 7 / May 7 1971 contradict the recording
# Provisional day of year from the HR1 handover sequence: start times fall ~30 min before
# the preceding HR1U reel ends (T510085 at 211-15:22, T510109 at 219-06:59).
INFERRED_DOY = {'A15_T510087': 211, 'A15_T510111': 219}


def doy(text):
    d, h, m, s = (int(x) for x in text.split('-'))
    return d*86400 + h*3600 + m*60 + s


def union(intervals):
    out = []
    for a, b in sorted(intervals):
        if out and a <= out[-1][1]:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return out


def clip_len(intervals, lo, hi):
    return sum(max(0, min(b, hi) - max(a, lo)) for a, b in intervals)


def hum_fundamental(wavs, positions=(.15, .35, .55, .75, .9), channels=('_CH5_', '_CH14_', '_CH20_', '_CH25_')):
    """Median on-tape hum fundamental: PSD peaks of 60·ρ·n (n = 1..3) after exact-line removal."""
    import flutter_census as census
    from scipy import signal
    s, values = census.SETTINGS, []
    low = 8000/s['decimation']
    for p in wavs:
        if not any(c in p.name for c in channels):
            continue
        info = sf.info(p)
        for q in positions:
            x, _ = sf.read(p, start=int(q*(info.frames-64*8000)), frames=64*8000, dtype='float64')
            xd = signal.resample_poly(x, 1, s['decimation'])
            clean = census.remove_stationary(xd, census.exact_mains(xd, low, s, fmax=400.), low)
            f, pw = signal.welch(clean, low, nperseg=int(16*low), noverlap=int(8*low))
            for n in (1, 2, 3):
                # Stay clear of the exact 60·n Hz residual; refine the peak parabolically.
                idx = np.flatnonzero((f > 58.3*n) & (f < 59.8*n))
                k = idx[np.argmax(pw[idx])]
                if pw[k] > 30*np.median(pw[idx]) and idx[0] < k < idx[-1]:
                    y0, y1, y2 = np.log(pw[k-1:k+2])
                    delta = .5*(y0-y2)/(y0-2*y1+y2) if y0-2*y1+y2 < 0 else 0.
                    values.append((f[k]+delta*(f[1]-f[0]))/n)
    return (float(np.median(values)), len(values)) if values else (None, 0)


def main():
    tapes = []
    for folder in sorted((ROOT/'orig_concatenated').glob('A15_T*')):
        wavs = [p for p in folder.glob('*.wav') if re.search(r'_CH\d+_', p.name)]
        m = re.search(r'_(HR\d[UL])_CH\d+_.*?_(\d+-\d+-\d+-\d+)_(\d+-\d+-\d+-\d+)\.wav$', wavs[0].name)
        head, a, b = m.group(1), doy(m.group(2)), doy(m.group(3))
        seconds = [sf.info(p).duration for p in wavs]
        hum, n = (None, 0) if folder.name in NO_SIGNAL else hum_fundamental(wavs)
        row = dict(tape=folder.name, head=head, recorder=head[:3], start=a, end=b, tracks=len(wavs),
                   hours=float(np.median(seconds))/3600, track_hours=sum(seconds)/3600,
                   status=('no_signal' if folder.name in NO_SIGNAL else
                           'unplaced' if folder.name in UNPLACED else 'delivered'),
                   hum_hz=hum, hum_measurements=n)
        if folder.name in INFERRED_DOY:
            shift = (INFERRED_DOY[folder.name] - a//86400)*86400
            row.update(inferred_start=a+shift, inferred_end=b+shift)
        tapes.append(row)
        print(folder.name, head, row['status'], hum, n, flush=True)
    for tape, (head, a, b) in UNDELIVERED.items():
        tapes.append(dict(tape=tape, head=head, recorder=head[:3], start=a, end=b, tracks=30,
                          hours=(b-a)/3600, track_hours=30*(b-a)/3600, status='undelivered', hum_hz=None))
    mission = SPLASHDOWN - LAUNCH

    def span(t):
        return (t.get('inferred_start', t['start']), t.get('inferred_end', t['end']))

    usable = ('delivered', 'unplaced')
    summary = {}
    for rec in ('HR1', 'HR2'):
        rows = [t for t in tapes if t['recorder'] == rec]
        good = union([span(t) for t in rows if t['status'] in usable])
        everything = union([span(t) for t in rows])
        covered = clip_len(good, LAUNCH, SPLASHDOWN)
        summary[rec] = dict(
            reels=len(rows), delivered=sum(t['status'] != 'undelivered' for t in rows),
            usable_track_hours=sum(t['track_hours'] for t in rows if t['status'] in usable),
            lost_track_hours=sum(t['track_hours'] for t in rows if t['status'] in ('undelivered', 'no_signal')),
            mission_covered_fraction=covered/mission,
            mission_covered_as_labelled_fraction=clip_len(union(
                [(t['start'], t['end']) for t in rows if t['status'] == 'delivered']), LAUNCH, SPLASHDOWN)/mission,
            mission_recorded_fraction=clip_len(everything, LAUNCH, SPLASHDOWN)/mission,
            lost_mission_hours=(clip_len(everything, LAUNCH, SPLASHDOWN)-covered)/3600,
            uncovered_mission_hours=(mission-covered)/3600)
    both = []
    for rec in ('HR1', 'HR2'):
        both.append(union([span(t) for t in tapes if t['recorder'] == rec and t['status'] in usable]))
    # Time when both recorders deliver usable audio (60 simultaneous tracks).
    overlap = 0.
    for a, b in both[0]:
        for c, d in both[1]:
            overlap += max(0, min(b, d, SPLASHDOWN) - max(a, c, LAUNCH))
    hums = [t['hum_hz'] for t in tapes if t['hum_hz']]
    for head in ('HR1U', 'HR1L', 'HR2U', 'HR2L'):
        v = [t['hum_hz'] for t in tapes if t['hum_hz'] and t['head'] == head]
        print(head, 'median hum', np.median(v), 'slow %', 100*(1-np.median(v)/60), 'n', len(v))
    result = dict(tapes=tapes, summary=summary, launch=LAUNCH, splashdown=SPLASHDOWN,
                  mission_hours=(SPLASHDOWN-LAUNCH)/3600, both_recorders_mission_fraction=overlap/(SPLASHDOWN-LAUNCH),
                  hum_hz=dict(median=float(np.median(hums)), min=float(min(hums)), max=float(max(hums)), n=len(hums)),
                  delivered_track_hours=sum(t['track_hours'] for t in tapes if t['status'] != 'undelivered'),
                  usable_track_hours=sum(t['track_hours'] for t in tapes if t['status'] in ('delivered', 'unplaced')))
    OUT.write_text(json.dumps(result, indent=1), encoding='utf-8')
    print(json.dumps({k: v for k, v in result.items() if k != 'tapes'}, indent=1))



if __name__ == '__main__':
    main()
