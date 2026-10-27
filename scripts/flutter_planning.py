"""Canonical tape inventory and reference acquisition helpers for stage 05a."""
import hashlib
import json
from pathlib import Path
import re
import warnings

import numpy as np
import soundfile as sf
from flutter_tracking import RATE, burst_track

STAMP = re.compile(r'_(\d+-\d+-\d+-\d+)_(\d+-\d+-\d+-\d+)\.wav$', re.I)
CHANNEL = re.compile(r'_CH(\d+)_', re.I)


def write_json(path, value):
    temporary = path.with_suffix(path.suffix+'.partial')
    temporary.write_text(json.dumps(value, indent=2, allow_nan=False), encoding='utf-8')
    temporary.replace(path)


def seconds(value):
    day, hour, minute, second = map(int, value.split('-'))
    if day < 0 or not 0 <= hour <= 24 or not 0 <= minute < 60 or not 0 <= second < 60:
        raise ValueError('Invalid filename timestamp')
    return day*86400+hour*3600+minute*60+second


def inventory(folder):
    """One canonical mono WAV per channel; filename clocks preserve offsets."""
    folder = Path(folder).resolve()
    if not folder.is_dir():
        raise ValueError('Tape input must be a canonical tape folder')
    if list(folder.rglob('*.partial')):
        raise ValueError('Tape contains incomplete .partial files')
    rows, seen, excluded = [], set(), []
    files = sorted(p for p in folder.rglob('*') if p.suffix.lower() == '.wav')
    for path in files:
        ch, stamp = CHANNEL.search(path.name), STAMP.search(path.name)
        # Some deliveries include an extra recording with an explicitly empty
        # channel label. Keep it unassigned without blocking numbered channels.
        if not ch and stamp and re.fullmatch(r'A15_T\d+_HR\d+[UL]_', path.name[:stamp.start()], re.I):
            reason = 'Unlabelled recording has no channel number; excluded from channel processing'
            stat = path.stat()
            excluded.append(dict(path=str(path), bytes=stat.st_size, mtime_ns=stat.st_mtime_ns, reason=reason))
            warnings.warn(f'{reason}: {path.name}', stacklevel=2)
            continue
        if not ch or not stamp:
            raise ValueError(f'Expected a timestamped canonical channel WAV: {path.name}')
        channel = int(ch[1])
        if not 1 <= channel <= 30 or channel in seen:
            raise ValueError('Use one concatenated file per channel, numbered 1 through 30')
        seen.add(channel)
        info, stat = sf.info(path), path.stat()
        if info.channels != 1 or not 6000 <= info.samplerate <= 48000 or info.samplerate % 1000:
            raise ValueError(f'Unsupported mono/rate format: {path}')
        start, end = seconds(stamp[1]), seconds(stamp[2])
        if end <= start or abs(info.duration-(end-start)) > 1:
            raise ValueError(f'Filename range disagrees with audio duration: {path}')
        head = re.search(r'_(HR\d+[UL])_', path.name)
        rows.append(dict(channel=channel, path=str(path), head=head[1] if head else None,
                         bytes=stat.st_size, mtime_ns=stat.st_mtime_ns,
                         sample_rate=info.samplerate, frames=info.frames,
                         absolute_start=start, duration_seconds=info.duration,
                         role='timing_only' if 'IRIG' in path.name.upper() else 'reference_and_target'))
    if not rows:
        raise ValueError('No canonical WAV channels found')
    origin = min(r['absolute_start'] for r in rows)
    for r in rows:
        r['start_seconds'] = r['absolute_start']-origin
        r['end_seconds'] = r['start_seconds']+r['duration_seconds']
    result = dict(tape=folder.name, input=str(folder), origin_filename_seconds=origin,
                time_basis='Canonical filename timeline; not authenticated GET or proven head alignment',
                duration_seconds=max(r['end_seconds'] for r in rows),
                missing_channels=sorted(set(range(1, 31))-seen), channels=sorted(rows, key=lambda r: r['channel']))
    if excluded:
        result['excluded_files'] = excluded
    return result


def read_channel(row, start, end):
    path = Path(row['path'])
    def unchanged():
        s = path.stat()
        return s.st_size == row['bytes'] and s.st_mtime_ns == row['mtime_ns']
    if not unchanged():
        raise ValueError(f'Source changed since inventory: {path}')
    sr = row['sample_rate']
    if start < row['start_seconds'] or end > row['end_seconds']+1/sr:
        raise ValueError('Channel does not cover the analysis interval')
    with sf.SoundFile(path) as f:
        f.seek(round((start-row['start_seconds'])*sr))
        x = f.read(round((end-start)*sr), dtype='float64')
    if len(x) != round((end-start)*sr) or not np.isfinite(x).all() or not unchanged():
        raise ValueError(f'Changed, short or non-finite source read: {path}')
    return x, sr, hashlib.sha256(x.tobytes()).hexdigest()


def loss(metrics):
    values = [m['residual_ratio'] for b in metrics for m in b['margins']]
    return float(np.median(values)) if values else None


def fair_shortlist(candidates, screening, limit):
    """Greedy coverage of target winners; a channel number never sets priority."""
    eligible = {c['key']: c for c in candidates if c['eligible']}
    scores = {}
    for s in screening:
        if s['proxy_loss'] is not None:
            scores.setdefault(s['target_channel'], []).append(s)
    winners = {ch: min(rows, key=lambda r: (r['proxy_loss'], r['key']))['key'] for ch, rows in scores.items()}
    chosen = []
    while eligible and len(chosen) < limit:
        best = max(eligible.values(), key=lambda c: (sum(k == c['key'] for k in winners.values()), c['rank'], c['key']))
        chosen.append(best)
        eligible.pop(best['key'])
        winners = {ch: k for ch, k in winners.items() if k != best['key']}
    return chosen, dict(limit=limit, eligible_candidates=len(chosen)+len(eligible),
                        exhausted=bool(eligible), targets_without_winner=sorted(winners),
                        untried_candidates=sorted(eligible),
                        interpretation='Budget exhaustion is not exhausted evidence; increase reference-limit for calibration before freezing holdouts')


def calibration_proxy(x, sr, curve, events):
    """Cheap signed-frequency screen; final selection uses actual rendered audio."""
    values = []
    for b in events:
        lo, hi = int((b['start']-.15)*sr), int((b['end']+.15)*sr)
        _, freq = burst_track(x[lo:hi], sr)
        tt = lo/sr+np.arange(len(freq))/1000
        keep = (tt > b['start']+.04) & (tt < b['end']-.04)
        e = np.interp(tt[keep], np.arange(len(curve))/RATE, curve)
        if keep.sum() >= 40:
            values.append(float(np.std(freq[keep]/(1+e))/max(np.std(freq[keep]), 1e-9)))
    return float(np.median(values)) if values else None
