"""Physical evidence for the channel maps (stage 10; docs/23): head rows and the time-code carrier.

Head rows. The 1971 recorders wrote odd and even true tracks with two staggered head rows, and in
the delivered capture a sound reaches the even (later-row) tracks about 1.38 s after the odd
(earlier-row) ones. The 07a anchors measure that stagger for every channel and 30-minute interval
from SG words heard on several channels. So a measured row fixes the parity of the true track a
delivered channel reads: earlier row -> odd, later row -> even. Where the parity of the GLM map
breaks the alternation, a true track is skipped or present that the map placed otherwise (T510075
reads true CH15 on CH17; T510073 and T510078 read true CH08). A channel whose row changes along the
tape slides between two tracks part-way through the reel; it gets weaker evidence and a note.

Carrier. True CH01 is the IRIG-B track: a 1 kHz carrier is on it wherever it was recorded. A
delivered channel with no 1 kHz line at all cannot be true CH01 (T510073 CH30: -1.4 dB).

``rows(tape)`` and ``carrier(tape, channel)`` read saved artifacts and 05b audio only.
"""
from __future__ import annotations

import json
import math
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np

TAPE_DATA = Path(os.environ.get('A15_TAPE_ROOT', 'D:/Apollo_15_30-Track' if os.name == 'nt' else '/mnt/d/Apollo_15_30-Track'))
RETIMING = TAPE_DATA / 'stage_evidence' / 'retiming'
DEFLUTTERED = TAPE_DATA / 'defluttered_shared'
ROW_TOLERANCE_S = 0.3
MIN_INTERVALS = 2
ROW_NATS = 6.0            # log-penalty for a true track of the wrong parity on a channel measured on one row
CARRIER_MIN_DB = 3.0      # median 1 kHz line over the local floor below this: no time-code carrier
CARRIER_WINDOWS = 5


def _read_tsv(path):
    lines = Path(path).read_text(encoding='utf-8').splitlines()
    head = lines[0].split('\t')
    return [dict(zip(head, ln.split('\t'))) for ln in lines[1:] if ln]


def anchors_edition(tape, root=RETIMING):
    """Newest complete anchors edition with a curve (any sg-retime version), else None."""
    base = Path(root) / tape / 'sg_anchors'
    found = []
    for p in base.iterdir() if base.is_dir() else []:
        m = p / 'manifest.json'
        if p.name.startswith('anchors-') and m.is_file():
            rec = json.loads(m.read_text(encoding='utf-8'))
            if rec.get('status') == 'complete' and rec.get('result') == 'curve':
                found.append((rec.get('version', ''), p.stat().st_mtime, p))
    return max(found)[2] if found else None


def rows(tape, root=RETIMING):
    """{delivered: dict(row 'E'/'L', share, intervals, sequence, changes)} from the 07a stagger."""
    ed = anchors_edition(tape, root)
    if ed is None:
        return {}, None
    manifest = json.loads((ed / 'manifest.json').read_text(encoding='utf-8'))
    groups = sorted(manifest.get('stagger_groups_s') or [])
    if len(groups) < 2:
        return {}, ed.name
    early, late = groups[0], groups[1]
    per = defaultdict(list)
    for r in _read_tsv(ed / 'stagger.tsv'):
        if r['measured'] != 'True' or r['stagger_s'] in ('', None):
            continue
        s = float(r['stagger_s'])
        row = 'E' if abs(s - early) <= ROW_TOLERANCE_S else 'L' if abs(s - late) <= ROW_TOLERANCE_S else None
        if row:
            per[int(r['channel'])].append((int(r['interval']), row))
    out = {}
    for d, v in per.items():
        v.sort()
        c = Counter(x for _, x in v)
        row, n = c.most_common(1)[0]
        out[d] = dict(row=row, share=n / len(v), intervals=len(v), sequence=''.join(x for _, x in v),
                      changes=len(c) > 1 and n / len(v) < 0.85)
    return out, ed.name


def row_logp(info):
    """Log-penalty vector over true 0..30 for one channel's row evidence (0 where consistent)."""
    lp = np.zeros(31)
    if not info or info['intervals'] < MIN_INTERVALS:
        return lp
    strength = min(1.0, info['intervals'] / 4.0) * max(0.0, 2.0 * info['share'] - 1.0)
    if info['changes']:
        strength *= 0.5
    odd = info['row'] == 'E'
    for t in range(1, 31):
        if (t % 2 == 1) != odd:
            lp[t] = -ROW_NATS * strength
    return lp


def carrier(tape, channel, root=DEFLUTTERED, windows=CARRIER_WINDOWS):
    """Median 1 kHz line strength (dB over the 1.1-1.3 kHz floor) of a 05b channel, or None."""
    import soundfile as sf
    from scipy.signal import welch
    path = Path(root) / tape / f'CH{channel:02d}' / 'deflutter.wav'
    if not path.is_file():
        return None
    info = sf.info(path)
    sr, dur = info.samplerate, info.frames / info.samplerate
    out = []
    for start in np.linspace(0.1 * dur, 0.9 * dur, windows):
        x, _ = sf.read(path, start=int(start * sr), frames=int(60 * sr))
        f, p = welch(x, sr, nperseg=8192)
        band, ref = (f > 990) & (f < 1010), (f > 1100) & (f < 1300)
        out.append(10 * math.log10(p[band].max() / max(np.median(p[ref]), 1e-30)))
    return float(np.median(out))


def main(argv=None):
    import argparse
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('tapes', nargs='*')
    args = p.parse_args(argv)
    tapes = args.tapes or sorted(x.name for x in DEFLUTTERED.glob('A15_T*'))
    for tape in tapes:
        r, ed = rows(tape)
        print(f'{tape} ({ed}): ' + ' '.join(f'CH{d:02d}:{v["sequence"]}' for d, v in sorted(r.items())))


if __name__ == '__main__':
    sys.exit(main())
