"""Coordinate transforms for stage 05. Original samples and stage-04 identities stay intact."""
import hashlib
import json
from pathlib import Path

import flutter_alignment as audit


def report_path(root, inv):
    method = hashlib.sha256(Path(audit.__file__).read_bytes()).hexdigest()
    return Path(root).resolve()/method[:16]/inv['tape']/(audit.source_key(inv)+'.json')


def aligned_inventory(inv, root):
    path = report_path(root, inv)
    if not path.is_file():
        raise ValueError(f'Run flutter_alignment.py first; missing {path}')
    raw = path.read_bytes()
    report = json.loads(raw)
    if (report['source_key'] != audit.source_key(inv) or report['tape'] != inv['tape']
            or report['method_sha256'] != hashlib.sha256(Path(audit.__file__).read_bytes()).hexdigest()
            ):
        raise ValueError(f'Alignment requires review: {path}')
    decisions = audit.decide(inv, report['windows'])
    if any(report.get(k) != value for k,value in decisions.items()):
        raise ValueError(f'Alignment decisions disagree with measurements: {path}')
    if report['action']=='review':
        import alignment_confirmation as confirmation
        second = confirmation.destination(root,inv)
        if second.exists():
            data = second.read_bytes()
            supplement = json.loads(data)
            resolved = confirmation.resolve(report,supplement['epochs'],inv)
            if (supplement['primary'] != dict(path=str(path),sha256=hashlib.sha256(raw).hexdigest())
                    or supplement['method_sha256'] != confirmation.evidence_hash()
                    or supplement['source_key'] != audit.source_key(inv)
                    or any(supplement.get(k) != v for k,v in resolved.items())):
                raise ValueError('Independent timing evidence changed')
            path, raw, report = second, data, supplement
    if report['action']=='review':
        import capture_highband
        last = capture_highband.destination(root,inv)
        if last.exists():
            data = last.read_bytes()
            supplement = json.loads(data)
            resolved = capture_highband.resolve(inv,report,supplement['epochs'])
            if (supplement['parent_sha256'] != hashlib.sha256(raw).hexdigest()
                    or supplement['source_key'] != audit.source_key(inv)
                    or any(supplement.get(k)!=v for k,v in resolved.items())):
                raise ValueError('High-band registration evidence changed')
            path,raw,report=last,data,supplement
    if report['action'] not in ('reuse','retrace'):
        raise ValueError(f"Unresolved capture timing on {inv['tape']}: {report['unresolved']}")
    rows = []
    for row in inv['channels']:
        delay = report['channels'][str(row['channel'])]['delayed_frames']
        start = row['start_seconds'] - delay/row['sample_rate']
        rows.append(dict(row, acquisition_start_seconds=row['start_seconds'], start_seconds=start))
    origin = min(r['start_seconds'] for r in rows)
    for row in rows:
        row['start_seconds'] -= origin
        row['end_seconds'] = row['start_seconds'] + row['duration_seconds']
    return dict(inv, channels=rows, duration_seconds=max(r['end_seconds'] for r in rows),
                time_basis='Shared physical tape seconds from earliest measured WAV start',
                alignment=dict(path=str(path), sha256=hashlib.sha256(raw).hexdigest(),
                               action=report['action'], origin_shift_seconds=origin))


def acquisition_row(row):
    if 'acquisition_start_seconds' not in row:
        return row
    original = row.copy()
    original['start_seconds'] = original.pop('acquisition_start_seconds')
    original['end_seconds'] = original['start_seconds'] + original['duration_seconds']
    return original


def verify_coordinates(inv, root):
    original = dict(inv, channels=[acquisition_row(r) for r in inv['channels']])
    expected = aligned_inventory(original, root)
    if inv.get('alignment'):
        if expected['alignment'] != inv['alignment'] or expected['channels'] != inv['channels']:
            raise ValueError('Plan uses different measured tape alignment')
    elif expected['alignment']['action'] != 'reuse':
        raise ValueError('This tape needs retracing and a new shared plan on its measured alignment')
