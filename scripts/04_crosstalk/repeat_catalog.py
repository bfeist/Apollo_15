"""Freeze one tape/channel direction and compile only bounded, donor-supported repeat intervals."""
from bisect import bisect_left
from collections import defaultdict
import json
from pathlib import Path
import shutil
import sys

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _bootstrap import REPO
from denoise_archive import atomic_json, sha256
from shared_tape import bound, verify_bindings
from crosstalk_stage import DEFAULT_EVIDENCE_ROOT, tape_key


def direction_for(events, saved):
    if saved:
        return saved
    blocks = defaultdict(list)
    for e in events:
        blocks[int(e['early']//60)].append(e['later_db'])
    levels = [(minute, float(np.median(db))) for minute, db in sorted(blocks.items())]
    votes = [(minute, db) for minute, db in levels if abs(db) >= 5]
    if len(votes) < 3:
        return None
    fraction = np.mean([db > 0 for _, db in votes])
    if .15 < fraction < .85:
        return None
    return dict(remove='earlier' if fraction >= .85 else 'later', minute_votes=votes,
                evidence='Median matched levels within target, one vote per minute with broadband donor support')


def intervals(events, direction, reserved):
    weak, strong = ('early', 'later') if direction == 'earlier' else ('later', 'early')
    # Protect every independently identified main, including those associated
    # with an ambiguous donor choice or a rejected subtraction.
    protected = sorted(round(e[strong]*8000) for e in events)
    protected_ends = [a+2000 for a in protected]
    groups = []
    for e in sorted(events, key=lambda x: x[weak]):
        a = round(e[weak]*8000)
        b = a+2000
        i = bisect_left(protected, b)
        if (i and protected_ends[i-1] > a) or any(a < hi and b > lo for lo, hi in reserved):
            continue
        previous = groups[-1] if groups else None
        if (previous and a <= previous['b'] and e['donor'] == previous['events'][-1]['donor']
                and b-previous['a'] <= 6000 and
                max(abs(previous['events'][-1][key]-e[key]) for key in ('early_path', 'late_path')) < .003):
            previous['b'] = max(previous['b'], b)
            previous['events'].append(e)
        elif not previous or a >= previous['b']:
            groups.append(dict(a=a, b=b, events=[e]))
    return groups


def compile_channel(report, ch, output):
    manifest = report/'sources.json'
    spec = json.loads(manifest.read_bytes())
    entries = {r['row']['channel']: r for r in spec['channels']}
    key = f'{tape_key(spec["tape"])}/CH{ch:02d}'
    stem = f'CH{ch:02d}'
    registry = DEFAULT_EVIDENCE_ROOT/'directions.json'
    prior = json.loads(registry.read_bytes()).get(key)
    results = report/'validate'/(stem+'.json')
    scan = report/'scan'/(stem+'.npy')
    scansig = report/'scan'/(stem+'.receipt.json')
    valsig = report/'validate'/(stem+'.receipt.json')
    scan_record, val_record = [json.loads(p.read_bytes()) for p in (scansig, valsig)]
    if (scan_record['sha256'] != sha256(scan) or val_record['identity']['scan'] != scan_record['sha256']
            or val_record['sha256'] != sha256(results)
            or any(x['identity']['sources'] != sha256(manifest) for x in (scan_record, val_record))):
        raise ValueError(f'Invalid worker receipts {results}')
    data = json.loads(results.read_bytes())
    if (data['tape'], data['channel']) != (spec['tape'], ch):
        raise ValueError(f'Wrong validation result {results}')
    events = data['events']
    chosen = direction_for(events, prior)
    bindings = [bound(p) for p in (manifest, scan, scansig, results, valsig, registry,
                                    DEFAULT_EVIDENCE_ROOT/'listening_feedback.json', Path(__file__))]
    if 'alignment' in spec:
        verify_bindings([spec['alignment']])
        bindings.append(spec['alignment'])
    if 'prior_catalog' in spec:
        old_catalog = Path(spec['prior_catalog'])/stem/'directions.json'
        previous = json.loads(old_catalog.read_bytes()).get(key)
        if previous:
            if prior and previous['remove'] != prior['remove']:
                raise ValueError(f'Conflicting fixed directions for {key}')
            chosen = previous
        bindings.append(bound(old_catalog))
    reserved, historical = [], []
    for review in DEFAULT_EVIDENCE_ROOT.rglob('review.json'):
        saved = json.loads(review.read_bytes())
        if (tape_key(saved['tape']), saved['target']) != (tape_key(spec['tape']), ch):
            continue
        historical.append(review)
        bindings.append(bound(review))
        a, b = saved['application_interval']
        reserved.append((round(a*8000), round(b*8000)))
        # Preserve the other, stronger occurrence of the historical phrase as well.
        gap = np.median(saved['main_tracking']['donor_minus_target_seconds'])-np.median(
            saved['echo_tracking']['donor_minus_target_seconds'])
        reserved.append((round((a-gap-.04)*8000), round((b-gap+.04)*8000)))
    reviews = []
    if chosen:
        direction = chosen['remove']
        weak, strong = ('early', 'late') if direction == 'earlier' else ('late', 'early')
        for group in intervals(events, direction, reserved):
            a, b = group['a']/8000, group['b']/8000
            windows = group['events']
            donor = windows[0]['donor']
            start, length = a-.125, b-a
            # Both source contexts must exist in canonical time.
            shift = windows[0].get('donor_center_shift_frames',0)
            if any(w.get('donor_center_shift_frames',0) != shift for w in windows):
                raise ValueError('Inconsistent donor clock within a repeat interval')
            if any(start+offset-2 < entries[c]['row']['start_seconds'] or
                   b+offset+.125+2 > entries[c]['row']['end_seconds']
                   for c,offset in ((ch,0.),(donor,shift/8000))):
                continue
            review = dict(tape=spec['tape'], target=ch, donor=donor, direction=direction,
                          direction_evidence=windows, start=start, duration=length+.25,
                          application_interval=[a, b], calibration_interval=[a+.3*length, b-.3*length],
                          sources={str(c): dict(signature=entries[c]['signature']) for c in (ch, donor)},
                          denoised_hashes={str(c): entries[c]['audio']['sha256'] for c in (ch, donor)})
            for label, field in [('main_tracking', strong), ('echo_tracking', weak)]:
                review[label] = dict(donor_minus_target_seconds=[e[field+'_path'] for e in windows],
                                     correlation=[e[field+'_corr'] for e in windows],
                                     time=[e['early' if weak == 'early' else 'later']+.125-start for e in windows])
            if 'alignment' in spec:
                review['donor_center_shift_frames'] = shift
            reviews.append(review)
    payload = dict(tape=spec['tape'], channel=ch, bindings=bindings, counts=data['counts'], reviews=reviews)
    directions = {key:chosen} if chosen else {}
    if output.exists():
        if (json.loads((output/'reviews.json').read_bytes()) != payload
                or json.loads((output/'directions.json').read_bytes()) != directions):
            raise ValueError(f'Existing catalog differs {output}')
        verify_bindings(bindings)
        return
    tmp = output.with_name(output.name+'.partial')
    tmp.mkdir(parents=True, exist_ok=True)
    for i, path in enumerate(historical):
        dest = tmp/f'historical_{i}'/'review.json'
        dest.parent.mkdir(exist_ok=True)
        shutil.copyfile(path, dest)
    atomic_json(tmp/'directions.json', directions)
    atomic_json(tmp/'reviews.json', payload)
    tmp.rename(output)
