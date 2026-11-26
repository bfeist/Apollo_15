"""Full-length IRIG-B decode of delivered channels with the NARA decoder (stage 10 check).

Uses ``scripts/sg-audio/01_sg-nara/nara_irig.decode_stream`` (vectorized reimplementation of
the legacy decoder; a frame is strict when its SBS matches its BCD time and every marker is in
place). About 750x real time on the 8 kHz originals, so a 17-hour channel takes ~80 s.

Per channel it stores strict frames per hour, first/last decoded clock, and the playback
rate (original-file seconds per IRIG second) under ``<root>/irig_nara/<tape>/CHnn.json``.
Results are reused when the source file's size and mtime are unchanged.

    uv run python scripts/30-track/10_channelid/channelid_irig_nara.py --channels 1,2,29,30
    uv run python scripts/30-track/10_channelid/channelid_irig_nara.py --tape A15_T510075 --channels all
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import re
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

HERE = Path(__file__).resolve().parent
NARA = HERE.parents[1] / 'sg-audio' / '01_sg-nara'
DATA = Path('D:/Apollo_15_30-Track')
DEFAULT_ROOT = DATA / 'channel_id'
ORIGINALS = DATA / 'orig_concatenated'
VERSION = 'channelid-irig-nara-v1'
MIN_STRICT = 20          # strict frames needed to call a channel time-code bearing


def files(tape: str):
    out = {}
    for f in (ORIGINALS / tape).glob('*.wav'):
        m = re.search(r'_CH(\d+)_', f.name)
        if m:
            out[int(m[1])] = f
    return out


def identity(path: Path):
    st = path.stat()
    return dict(path=str(path), bytes=st.st_size, mtime_ns=st.st_mtime_ns)


def decode(job):
    sys.path.insert(0, str(NARA))
    import numpy as np
    import nara_irig
    tape, ch, path, out = job
    t0 = time.monotonic()
    frames, total = nara_irig.decode_stream(path, None, 8000)
    strict = [f for f in frames if f.strict]
    per_hour = collections.Counter(int(f.t // 3600) for f in strict)
    rate = None
    if len(strict) >= MIN_STRICT:
        t = np.array([f.t for f in strict])
        c = np.array([f.clock for f in strict], dtype=float)
        dc, dt = np.diff(c), np.diff(t)
        good = (dc >= 1) & (dc <= 5) & (np.abs(dt / np.where(dc > 0, dc, 1) - 1) < 0.1)
        if good.sum() > 10:
            rate = float(np.median(dt[good] / dc[good]))   # file seconds per IRIG second
    rec = dict(version=VERSION, tape=tape, channel=ch, source=identity(Path(path)), decoded_s=round(total, 1),
               seconds=round(time.monotonic() - t0, 1), candidates=len(frames), strict=len(strict),
               strict_per_hour={str(k): v for k, v in sorted(per_hour.items())},
               first=strict[0].row() if strict else None, last=strict[-1].row() if strict else None,
               file_s_per_irig_s=rate,
               frames=[[round(f.t, 4), f.clock, round(f.quality, 3)] for f in strict])
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix('.partial')
    tmp.write_text(json.dumps(rec, separators=(',', ':')), encoding='utf-8')
    tmp.replace(out)
    return rec


def cached(out: Path, path: Path):
    if not out.is_file():
        return None
    rec = json.loads(out.read_text(encoding='utf-8'))
    return rec if rec.get('version') == VERSION and rec.get('source') == identity(path) else None


def tape_irig(root: Path, tape: str):
    """{'searched': [d], 'channels': {d: {strict, seconds_share, first, last, rate}}}, or None if not searched."""
    folder = Path(root) / 'irig_nara' / tape
    if not folder.is_dir():
        return None
    chans = {}
    for f in sorted(folder.glob('CH[0-9][0-9].json')):
        rec = json.loads(f.read_text(encoding='utf-8'))
        chans[str(rec['channel'])] = dict(strict=rec['strict'],
                                          seconds_share=round(rec['strict'] / max(1.0, rec['decoded_s']), 4),
                                          first=clock(rec['first']), last=clock(rec['last']),
                                          rate=rec['file_s_per_irig_s'])
    return dict(searched=sorted(int(c) for c in chans), channels=chans) if chans else None


def clock(row):
    return f'{row["day"]} {row["hours"]:02d}:{row["minutes"]:02d}:{row["seconds"]:02d}' if row else '-'


def main(argv=None):
    sys.stdout.reconfigure(encoding='utf-8')
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--root', type=Path, default=DEFAULT_ROOT)
    p.add_argument('--tape', action='append', default=[])
    p.add_argument('--channels', default='1,2,29,30', help="comma list or 'all'")
    p.add_argument('--workers', type=int, default=10)
    args = p.parse_args(argv)
    tapes = args.tape or sorted(d.name for d in ORIGINALS.glob('A15_T*') if d.is_dir())
    want = None if args.channels == 'all' else {int(c) for c in args.channels.split(',')}
    out_root = args.root / 'irig_nara'
    jobs, results = [], {}
    for tape in tapes:
        for ch, path in sorted(files(tape).items()):
            if want is not None and ch not in want:
                continue
            out = out_root / tape / f'CH{ch:02d}.json'
            rec = cached(out, path)
            if rec:
                results[(tape, ch)] = rec
            else:
                jobs.append((tape, ch, str(path), out))
    print(f'{len(results)} cached, {len(jobs)} to decode with {args.workers} workers', flush=True)
    t0 = time.monotonic()
    with ProcessPoolExecutor(args.workers) as ex:
        futs = {ex.submit(decode, j): j for j in jobs}
        for i, f in enumerate(as_completed(futs), 1):
            tape, ch = futs[f][:2]
            try:
                rec = f.result()
            except Exception as exc:  # noqa: BLE001
                print(f'[{i}/{len(jobs)}] {tape} CH{ch:02d} FAILED {exc}', flush=True)
                continue
            results[(tape, ch)] = rec
            el = time.monotonic() - t0
            print(f'[{i}/{len(jobs)} {el:6.0f}s ETA {el / i * (len(jobs) - i):5.0f}s] {tape} CH{ch:02d}: '
                  f'{rec["strict"]} strict frames, {clock(rec["first"])} .. {clock(rec["last"])}', flush=True)
    summary = {}
    for (tape, ch), rec in sorted(results.items()):
        summary.setdefault(tape, {})[str(ch)] = dict(
            strict=rec['strict'], hours_with_code=sum(1 for v in rec['strict_per_hour'].values() if v >= 60),
            first=clock(rec['first']), last=clock(rec['last']), file_s_per_irig_s=rec['file_s_per_irig_s'])
    out_root.mkdir(parents=True, exist_ok=True)
    (out_root / 'summary.json').write_text(json.dumps(summary, indent=1), encoding='utf-8')
    print('\nTime code (>= %d strict frames):' % MIN_STRICT)
    for tape, chans in summary.items():
        hits = {c: v for c, v in chans.items() if v['strict'] >= MIN_STRICT}
        line = '; '.join(f'CH{int(c):02d} {v["strict"]} frames, {v["hours_with_code"]} h, {v["first"]} .. {v["last"]}, '
                         f'rate {v["file_s_per_irig_s"]:.4f}' if v['file_s_per_irig_s'] else
                         f'CH{int(c):02d} {v["strict"]} frames' for c, v in hits.items())
        print(f'  {tape}: {line or "none"}')
    print(f'Wrote {out_root / "summary.json"}')


if __name__ == '__main__':
    main()
