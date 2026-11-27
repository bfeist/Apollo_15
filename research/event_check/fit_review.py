"""Step 5 of the docs/23 archive run: review a 07a joint fit and say whether to continue. Read-only.

  uv run python research/event_check/fit_review.py              # newest complete fit edition
  uv run python research/event_check/fit_review.py <sg_fit edition folder>

Three checks, each printed with a verdict and the action to take:

1. NARA voice-row switches. Residuals (GET - curve) of NARA events, pooled in 30-minute GET bins.
   A bin whose median is beyond 0.25 s (a voice-row step is 0.683 s) means a NARA tape changes
   head row there: add an interval to catalog/nara_voice/nara_voice_offsets.json, rerun from step 2.
2. NARA tapes of unknown voice row (655-AAA). Their free offset should land on 0 (voice on the
   time-code row) or on the row gap 0.683 s (voice on the other row). Between the two means the tape
   switches row inside the fitted span: find the switch with check 1.
3. IA segment offsets. Expected near the prior mean -0.42 s (measured -0.28..-0.52 s). A
   well-constrained segment more than 0.35 s from it is suspect (an IA drift-map error or a
   row step). The offsets are a proposal only (ia_segment_revision.json, never applied).

Exit code 0: continue to step 6. Exit code 1: something needs a decision (listed at the end).
"""
import csv
import json
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / 'scripts/30-track/07_retime'), str(ROOT / 'scripts/00_common')]
from sg_inputs import IA_OFFSET_PRIOR_MEAN_S, NARA_VOICE_OFFSETS, RETIMING_ROOT, read_json  # noqa: E402

BIN_S = 1800
MIN_BIN_EVENTS = 30
BIN_LIMIT_S = 0.25
VOICE_TOL_S = 0.15     # a free NARA offset within this of 0 or the row gap is settled
MIN_EVENTS = 30        # fewer events: the prior dominates, nothing to decide
IA_LIMIT_S = 0.35


def hms(get_s):
    h, r = divmod(int(round(get_s)), 3600)
    return f'{h:03d}:{r // 60:02d}'


def find_fit(arg):
    if arg:
        fit = Path(arg)
        if '<' in arg or not fit.is_dir():
            sys.exit(f'Not a fit edition folder: {arg}')
        return fit
    import sg_stage
    fits = []
    for f in (RETIMING_ROOT / 'sg_fit').glob('fit-*'):
        m = f / 'manifest.json'
        if m.is_file():
            man = read_json(m)
            if man.get('status') == 'complete' and man.get('version') == sg_stage.VERSION:
                fits.append(f)
    if not fits:
        sys.exit(f'No complete {sg_stage.VERSION} fit edition under {RETIMING_ROOT / "sg_fit"}: '
                 'step 4 (07a run) has not finished.')
    return max(fits, key=lambda f: (f / 'manifest.json').stat().st_mtime_ns)


def check_bins(fit, issues):
    print('\n1. NARA voice-row switches (median residual per 30-min GET bin)')
    plans = sorted((fit / 'tapes').glob('*/curve.json'))
    if not plans:
        sys.exit(f'{fit} has no tapes/*/curve.json: not a complete fit edition.')
    bins = defaultdict(list)
    tapes_in = defaultdict(set)
    for plan in plans:
        for a in read_json(plan)['anchors']:
            if a['ref_kind'] == 'nara' and a['kind'] != 'irig':
                k = int(a['get_s'] // BIN_S)
                bins[k].append(a['residual_s'])
                tapes_in[k].add(plan.parent.name)
    used = {k: np.asarray(v) for k, v in bins.items() if len(v) >= MIN_BIN_EVENTS}
    if not used:
        issues.append('No NARA bin has enough events to test: the fit has no usable NARA evidence.')
        print('   FAIL  no bin with enough NARA events')
        return
    from sg_inputs import nara_sources
    spans = {s['name']: [(g['get_start_s'], g['get_end_s'], g['voice_late_s']) for g in s['segments']] for s in nara_sources()}
    flagged = []
    for k in sorted(used):
        med = float(np.median(used[k]))
        if abs(med) > BIN_LIMIT_S:
            flagged.append(k)
            lo, hi = k * BIN_S, (k + 1) * BIN_S
            print(f'   REVIEW GET {hms(lo)}-{hms(hi)}  n={len(used[k]):5d}  median {med:+.3f} s  '
                  f'(30-track {",".join(sorted(tapes_in[k]))})')
            for name, segs in spans.items():
                lates = {late for a, b, late in segs if a < hi and b > lo}
                if lates:
                    # residual = mix-derived GET - curve; positive: voice placed too late, i.e. lag under-corrected
                    new = [round(max(0.0, min(row_gap(), late + med)) / row_gap()) * row_gap() for late in lates]
                    print(f'      NARA {name} (voice_late_s now {",".join(f"{x:.3f}" for x in sorted(lates))}); candidate: '
                          f'"intervals": [{{"get_from_s": {lo:.1f}, "get_to_s": {hi:.1f}, "voice_late_s": {new[0]:.3f}}}]')
    print(f'   {len(used)} bins tested ({sum(len(v) for v in used.values())} NARA events), {len(flagged)} off by a row step')
    if flagged:
        issues.append(f'{len(flagged)} NARA bin(s) off by a voice-row step: add an interval '
                      f'(get_from_s/get_to_s, voice_late_s 0 or {row_gap():.3f}) for the NARA tape covering each flagged '
                      f'GET range to {NARA_VOICE_OFFSETS.relative_to(ROOT)}, then rerun from step 2.')
    else:
        print('   OK')


def row_gap():
    return float(json.loads(NARA_VOICE_OFFSETS.read_text(encoding='utf-8'))['row_gap_s'])


def read_offsets(fit):
    path = fit / 'ia_offsets.tsv'
    if not path.is_file():
        sys.exit(f'{path} is missing: not a complete fit edition.')
    with path.open(encoding='utf-8', newline='') as f:
        rows = list(csv.DictReader(f, delimiter='\t'))
    num = lambda v: float(v) if v not in ('', None) else None  # noqa: E731
    for r in rows:
        r['offset_s'], r['se_s'], r['events'] = num(r['offset_s']), num(r['se_s']), int(float(r['events'] or 0))
    return rows


def check_free_nara(rows, issues):
    print('\n2. NARA tapes of unknown voice row (free offsets)')
    voice = json.loads(NARA_VOICE_OFFSETS.read_text(encoding='utf-8'))
    gap = float(voice['row_gap_s'])
    unknown = [t for t, v in voice['tapes'].items() if v.get('voice_late_s', 0.0) is None]
    if not unknown:
        print('   none in the voice table: OK')
        return
    for tape in unknown:
        mine = [r for r in rows if r['key'].startswith(f'nara:{tape}:')]
        if not mine:
            print(f'   {tape}: no events in this fit; its offset constrains nothing. OK')
            continue
        for r in mine:
            b, se, n = r['offset_s'], r['se_s'], r['events']
            se_txt = f' +/- {se:.3f}' if se is not None else ''
            head = f'   {r["key"]}: offset {b:+.3f}{se_txt} s from {n} events ({r["tapes"]})'
            if n < MIN_EVENTS:
                print(f'{head}\n      OK   too few events to decide; it stays free and barely moves the fit')
                continue
            near = min((0.0, gap), key=lambda c: abs(b - c))
            if abs(b - near) <= VOICE_TOL_S:
                row = 'the time-code row' if near == 0.0 else 'the other row'
                print(f'{head}\n      OK   voice is on {row}. Optional: set "voice_late_s": {near:.3f} for {tape} in '
                      f'{NARA_VOICE_OFFSETS.relative_to(ROOT)} to pin it (then rerun from step 2).')
            else:
                print(f'{head}\n      REVIEW  between 0 and {gap:.3f} s')
                issues.append(f'{tape} free offset {b:+.3f} s is neither 0 nor {gap:.3f} s: the tape probably switches '
                              f'row inside the fit. Use the flagged bins of check 1 around its GET range to place an '
                              f'interval, set its voice_late_s, and rerun from step 2.')


def check_ia(rows, issues):
    print(f'\n3. IA segment offsets (prior mean {IA_OFFSET_PRIOR_MEAN_S:+.2f} s)')
    ia = [r for r in rows if r['key'].startswith('ia:')]
    if not ia:
        print('   no IA segments in this fit')
        return
    strong = [r for r in ia if r['events'] >= MIN_EVENTS]
    bad = [r for r in strong if abs(r['offset_s'] - IA_OFFSET_PRIOR_MEAN_S) > IA_LIMIT_S]
    if strong:
        v = np.array([r['offset_s'] for r in strong])
        print(f'   {len(strong)} of {len(ia)} segments with >= {MIN_EVENTS} events: median {np.median(v):+.3f} s, '
              f'range {v.min():+.3f}..{v.max():+.3f} s')
    for r in bad:
        se = f' +/- {r["se_s"]:.3f}' if r['se_s'] is not None else ''
        print(f'   REVIEW {r["key"]}: {r["offset_s"]:+.3f}{se} s from {r["events"]} events ({r["tapes"]})')
    if bad:
        issues.append(f'{len(bad)} IA segment(s) more than {IA_LIMIT_S} s from {IA_OFFSET_PRIOR_MEAN_S:+.2f} s. '
                      'Check those reels in catalog/sg_drift; they matter most for tapes with little NARA evidence.')
    else:
        print('   OK')


def main():
    fit = find_fit(sys.argv[1] if len(sys.argv) > 1 else None)
    print(f'Fit edition: {fit}')
    issues = []
    check_bins(fit, issues)
    rows = read_offsets(fit)
    check_free_nara(rows, issues)
    check_ia(rows, issues)
    if issues:
        print('\nNEEDS A DECISION before step 6:')
        for i, s in enumerate(issues, 1):
            print(f'  {i}. {s}')
        sys.exit(1)
    print('\nAll checks pass: continue to step 6.')


if __name__ == '__main__':
    main()
