"""Search the no-speech delivered channels for IRIG-B time code (stage 10 check).

Stage 10 finds delivered channel numbers running opposite to the track sheet (numbering
only; the audio plays forward), with true CH01 (IRIG-B) not delivered.
The ``09_irig`` search only looked at delivered CH01-CH04. This runs the same, unchanged
``irig_search.search_tape`` on the delivered channels the stage-10 maps call "no track"
or where GLM found no own speech, so a time-code track at the other end, or anywhere,
would be found.

``irig_search`` selects channels through its module-level ``CHANNELS``; this sets it per
tape and restores it afterwards. The frozen source is not modified. Screening finds
carriers; decoding (needs ``--legacy-repo``) establishes IRIG-B identity.

    uv run python scripts/30-track/10_channelid/channelid_irig.py --list
    uv run python scripts/30-track/10_channelid/channelid_irig.py --screen-only
    uv run python scripts/30-track/10_channelid/channelid_irig.py --legacy-repo F:/_repos/Apollo_Audio_Processing
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(SCRIPTS))
import _bootstrap  # noqa: E402,F401  stage import paths
import irig_search  # noqa: E402

DATA = Path('D:/Apollo_15_30-Track')
DEFAULT_ROOT = DATA / 'channel_id'
ORIGINALS = DATA / 'orig_concatenated'


def candidates(root: Path, maps_dir: str, ends: str):
    """{tape: [delivered channels]} with no track or no own speech."""
    out = {}
    for p in sorted((root / maps_dir / 'maps').glob('A15_T*.json')):
        m = json.loads(p.read_text(encoding='utf-8'))
        d30 = next((int(d) for d, c in m['channels'].items() if c['true_channel'] == 30), 15)
        chans = []
        for d, c in sorted(m['channels'].items(), key=lambda kv: int(kv[0])):
            d = int(d)
            silent = c['true_channel'] is None or c['glm_top'] == 0
            side = 'low' if d < d30 else 'high'
            if silent and ends in ('all', side):
                chans.append(d)
        if chans:
            out[m['tape']] = chans
    return out


def main(argv=None):
    sys.stdout.reconfigure(encoding='utf-8')
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--root', type=Path, default=DEFAULT_ROOT)
    p.add_argument('--maps', default='shared', help="map set under --root: 'shared' (default) or '.' (per tape)")
    p.add_argument('--ends', choices=('all', 'low', 'high'), default='all',
                   help='channels before (low) or after (high) the delivered channel that reads true CH30')
    p.add_argument('--tape', action='append', default=[])
    p.add_argument('--legacy-repo', type=Path)
    p.add_argument('--screen-only', action='store_true')
    p.add_argument('--max-windows', type=int)
    p.add_argument('--list', action='store_true', help='only list the channels that would be searched')
    args = p.parse_args(argv)
    if not args.list and not args.screen_only and args.legacy_repo is None:
        p.error('--legacy-repo is required unless --screen-only or --list')
    maps_dir = '' if args.maps == '.' else args.maps
    todo = candidates(args.root, maps_dir, args.ends)
    if args.tape:
        todo = {t: c for t, c in todo.items() if t in args.tape}
    total = sum(len(c) for c in todo.values())
    print(f'{len(todo)} tapes, {total} channels: ' + '; '.join(
        f'{t[-6:]} ' + ','.join(f'{c:02d}' for c in cs) for t, cs in todo.items()), flush=True)
    if args.list:
        return
    out_root = args.root / 'irig_check'
    out_root.mkdir(parents=True, exist_ok=True)
    manifest_path = out_root / 'manifest.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8')) if manifest_path.is_file() else {}
    original = irig_search.CHANNELS
    t0 = time.monotonic()
    try:
        for i, (tape, chans) in enumerate(todo.items(), 1):
            irig_search.CHANNELS = tuple(chans)
            print(f'[{i}/{len(todo)} {time.monotonic() - t0:7.0f}s] {tape}: CH ' + ','.join(f'{c:02d}' for c in chans),
                  flush=True)
            report = irig_search.search_tape(ORIGINALS / tape, out_root / tape, args.legacy_repo,
                                             max_windows=args.max_windows, screen_only=args.screen_only)
            manifest[tape] = dict(channels=chans, status=report['status'], timecode_detected=report['timecode_detected'],
                                  snapshots=len(report.get('snapshots') or []),
                                  search=str(out_root / tape / 'search.json'),
                                  note='channels chosen by stage 10 (no track / no own speech); '
                                       'the screening index scope text in irig_search says CH01-CH04 and is stale')
            manifest_path.write_text(json.dumps(manifest, indent=1), encoding='utf-8')
            print(f'    -> {report["status"]}, timecode detected: {report["timecode_detected"]}, '
                  f'snapshots {len(report.get("snapshots") or [])}', flush=True)
    finally:
        irig_search.CHANNELS = original
    found = {t: v for t, v in manifest.items() if v.get('timecode_detected')}
    print(f'Done. Time code detected on {len(found)} tapes: {sorted(found)}')


if __name__ == '__main__':
    main()
