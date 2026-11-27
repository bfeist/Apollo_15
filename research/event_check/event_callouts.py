"""Which head-stack group is on real time? Documented events vs transcript callouts.

Read-only. For a time-coded tape, every channel's words are placed on GMT through the IRIG-B
frames decoded from CH30 (local robust linear fit around the event). Words near a documented
event are printed per channel, with the channel's stagger group where an anchors edition exists.

  uv run python research/event_check/event_callouts.py --tape A15_T510078 --get 28:40:22.00 --window 90
"""
import argparse
import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / 'scripts/30-track/07_retime'), str(ROOT / 'scripts/00_common'),
                str(ROOT / 'scripts/30-track/06_transcribe'), str(ROOT / 'scripts/sg-audio/02_sg-transcript')]
from sg_inputs import load_words, tape_channels, read_tsv  # noqa: E402

EVIDENCE = Path('D:/Apollo_15_30-Track/stage_evidence/retiming')
RANGE_ZERO = 206 * 86400 + 13 * 3600 + 34 * 60  # 1971-07-26 13:34:00 GMT, s since 1971-01-01


def parse_get(text):
    h, m, s = text.split(':')
    return int(h) * 3600 + int(m) * 60 + float(s)


def gmt_text(clock):
    day = int(clock // 86400) + 1
    r = clock - (day - 1) * 86400
    return f'{day:03d} {int(r // 3600):02d}:{int(r % 3600 // 60):02d}:{r % 60:06.3f}'


def clock_fit(tape, t_center, half=300.0):
    d = json.loads((EVIDENCE / 'irig' / tape / 'decode' / 'CH30.json').read_text())
    f = np.asarray(d['frames'], float)
    m = np.abs(f[:, 0] - t_center) < half
    t, c = f[m, 0], f[m, 1]
    if len(t) < 20:
        raise SystemExit(f'{tape}: only {len(t)} frames within {half} s of tape {t_center:.0f}')
    keep = np.ones(len(t), bool)
    for _ in range(5):
        a, b = np.polyfit(t[keep] - t_center, c[keep], 1)
        r = c - (a * (t - t_center) + b)
        keep = np.abs(r) < max(5 * np.median(np.abs(r[keep])), 0.005)
    return a, b, len(t), int(keep.sum()), float(np.sqrt(np.mean(r[keep] ** 2)))


def groups(tape):
    eds = sorted((EVIDENCE / tape / 'sg_anchors').glob('anchors-*/stagger.tsv'))
    if not eds:
        return {}
    out = {}
    for row in read_tsv(eds[-1]):
        if row.get('group') not in ('', None) and row.get('measured') in ('True', True):
            out.setdefault(int(row['channel']), []).append(int(row['group']))
    return {c: max(set(g), key=g.count) for c, g in out.items()}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--tape', required=True)
    ap.add_argument('--get', required=True, help='documented event GET hhh:mm:ss.ss')
    ap.add_argument('--window', type=float, default=60.0)
    ap.add_argument('--words', default='', help='comma-separated tokens to flag')
    args = ap.parse_args()
    event_clock = RANGE_ZERO + parse_get(args.get)
    # first approximate tape time from the whole decode
    d = json.loads((EVIDENCE / 'irig' / args.tape / 'decode' / 'CH30.json').read_text())
    f = np.asarray(d['frames'], float)
    med = np.median(f[:, 1] - f[:, 0])
    near = f[np.abs((f[:, 1] - f[:, 0]) - med) < 3600]
    t_guess = float(np.interp(event_clock, near[:, 1], near[:, 0]))
    a, b, n, k, rms = clock_fit(args.tape, t_guess)
    t_event = t_guess + (event_clock - (a * 0 + b)) / a
    a, b, n, k, rms = clock_fit(args.tape, t_event)
    print(f'{args.tape}: event GET {args.get} = GMT {gmt_text(event_clock)}; time-code tape '
          f'{t_event:.3f} s; {k}/{n} frames, rms {rms * 1000:.1f} ms, rate {a:.6f}')
    grp = groups(args.tape)
    flags = {w.strip().lower() for w in args.words.split(',') if w.strip()}
    rows = []
    for ch in tape_channels(args.tape):
        if ch['problem'] or not ch['identity_dir']:
            continue
        w = load_words(Path(ch['identity_dir']), EVIDENCE / '_cache' / 'words')
        t = ch['origin_s'] + w.s
        m = np.abs(t - t_event) < args.window
        for tok, tt, p, al in zip(w.tok[m], t[m], w.p[m], w.aligned[m]):
            gmt = a * (tt - t_event) + b
            rows.append((ch['channel'], grp.get(ch['channel'], '-'), gmt - event_clock, tok, p, al))
    rows.sort(key=lambda r: (r[0], r[2]))
    for ch in sorted({r[0] for r in rows}):
        sel = [r for r in rows if r[0] == ch]
        text = ' '.join((f'[{r[3]}@{r[2]:+.2f}]' if r[3] in flags else r[3]) for r in sel)
        print(f'CH{ch:02d} g{sel[0][1]}: {text}')


if __name__ == '__main__':
    main()
