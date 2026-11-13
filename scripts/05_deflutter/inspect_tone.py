"""Trace an Audition tone sighting through saved discovery and tape-map selection."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _bootstrap import install
install()

import argparse
import json
import math
from shared_tape import DEFAULT_EVIDENCE_ROOT, DEFAULT_PLANNING_ROOT, load_tape_plan


def inspect_tone(bundle, channel, seconds, hz, *, tolerance_hz=5., plan_path=None, local_time=False):
    if (not all(math.isfinite(v) for v in (seconds, hz, tolerance_hz))
            or seconds < 0 or hz <= 0 or tolerance_hz <= 0
            or not 1 <= channel <= 30):
        raise ValueError('Provide a valid channel, nonnegative time, and positive frequency/tolerance')
    bundle = Path(bundle)
    manifest = json.loads((bundle/'survey_manifest.json').read_text(encoding='utf-8'))
    row = next((r for r in manifest['inventory']['channels'] if r['channel'] == channel), None)
    if row is None:
        raise ValueError('Sighting channel is absent from the saved inventory')
    canonical = seconds+row['start_seconds'] if local_time else seconds
    matches = []
    windows = []
    for window in manifest['windows']:
        if not window['start_seconds'] <= canonical < window['end_seconds']:
            continue
        result = window['channels'].get(str(channel), {})
        windows.append(dict(start_seconds=window['start_seconds'], end_seconds=window['end_seconds'],
            status=result.get('status', 'missing'), reason=result.get('reason')))
        for candidate in result.get('candidates', []):
            if abs(candidate['carrier_hz']-hz) <= tolerance_hz:
                matches.append(candidate)
    report = dict(tape=manifest['inventory']['tape'], channel=channel, tape_seconds=canonical,
        local_seconds=canonical-row['start_seconds'], frequency_hz=hz, survey_windows=windows,
        discovery_matches=matches, new_audio_analysis=False,
        discovery_status='found' if matches else 'not_found' if windows else 'outside_saved_survey')
    if plan_path is not None:
        path, plan, data = load_tape_plan(plan_path)
        if plan['inventory']['tape'] != manifest['inventory']['tape']:
            raise ValueError('Sighting survey and tape map belong to different tapes')
        decisions = json.loads((path.parent/plan['selection']['file']).read_text(encoding='utf-8'))
        evidence = json.loads((path.parent/plan['evidence']['file']).read_text(encoding='utf-8'))
        report['tape_selection'] = next((s for s in decisions
            if s['start_seconds'] <= canonical < s['end_seconds']), None)
        if plan['start_seconds'] <= canonical <= plan['end_seconds']:
            import numpy as np
            report['applied_support'] = float(np.interp(canonical, data[:, 0], data[:, 3]))
            report['applied_speed'] = float(np.interp(canonical, data[:, 0], data[:, 2]))
        report['eligible_evidence'] = [c for c in evidence['candidates']
            if c['start_seconds'] <= canonical < c['end_seconds'] and c['reference_channel'] == channel
            and (c.get('carrier_hz') is None or abs(c['carrier_hz']-hz) <= tolerance_hz)]
        report['rejected_local_evidence'] = [c for c in evidence['rejected']
            if c['channel'] == channel and c['start_seconds'] <= canonical < c['end_seconds']]
        report['note'] = 'Selection may use a different channel at this tape time; sightings do not automatically qualify anchors.'
        del data
    return report


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--tape', required=True)
    p.add_argument('--channel', type=int, required=True)
    p.add_argument('--time', type=float, required=True, help='Seconds on the canonical tape clock')
    p.add_argument('--hz', type=float, required=True)
    p.add_argument('--tolerance-hz', type=float, default=5.)
    p.add_argument('--local-time', action='store_true', help='Time is relative to this channel WAV start')
    p.add_argument('--evidence-root', type=Path, default=DEFAULT_EVIDENCE_ROOT)
    p.add_argument('--flutter-plan', type=Path)
    args = p.parse_args(argv)
    try:
        if Path(args.tape).name != args.tape or args.tape in {'.', '..'}:
            raise ValueError('Tape must be a simple folder name')
        path = args.flutter_plan or DEFAULT_PLANNING_ROOT/args.tape/'plan.json'
        result = inspect_tone(args.evidence_root/args.tape, args.channel, args.time, args.hz,
            tolerance_hz=args.tolerance_hz, local_time=args.local_time,
            plan_path=path if path.exists() else None)
        print(json.dumps(result, indent=2))
    except (OSError, ValueError, RuntimeError, KeyError, TypeError) as error:
        p.error(str(error))


if __name__ == '__main__':
    main()
