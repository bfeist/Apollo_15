"""Are the delivered-to-true channel maps consistent across tapes of one kind? (stage 10)

Read-only: uses the saved GLM answers (``glm/<tape>/CHnn.json``) and evidence, never calls
the model, and writes only to ``<root>/consistency/``.

Model. For the reversed order, delivered channel ``d`` of tape ``k`` reads true track
``t = b_k + s(d) - d``. ``b_k`` is the tape's base offset (how that reel sat on the
playhead) and ``s(d)`` a shape: 0 at CH01 and dropping by one at each step position, at
most ``MAX_STEPS`` steps. A shape describes the playback head geometry (if it is the
same for every reel) or the recording head geometry (if it differs by recorder or by
head stack). For each grouping of tapes (all tapes, recorder HR1/HR2, head stack
HR1U/HR1L/HR2U/HR2L) one shape is shared within a group while each tape keeps its own
base. Scores are the solver's tempered log-likelihoods (``channelid_solve.emissions``);
each step costs ``log(STEP_P)`` as in the solver.

Reported per tape: its best own shape and the loss when forced to the group shape.
Reported per grouping: total loss and a BIC-style comparison, so "one shape for all"
can be weighed against "one per recorder" or "one per head stack".
"""
from __future__ import annotations

import argparse
import itertools
import json
import math
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import channelid_evidence as ce  # noqa: E402
import channelid_llm as cl  # noqa: E402
import channelid_sheets as cs  # noqa: E402
import channelid_solve as cv  # noqa: E402

DEFAULT_ROOT = Path('D:/Apollo_15_30-Track/channel_id')
MAX_STEPS = 3
OFFSETS = list(range(26, 41))
STEP_COST = -math.log(cv.STEP_P)
GROUPINGS = {'all tapes': lambda h: 'all', 'recorder': lambda h: h[:3], 'head stack': lambda h: h}


def load_tape(root: Path, tape: str):
    ev_path = root / 'evidence' / f'{tape}.json.gz'
    if not ev_path.is_file():
        return None
    ev = ce.read(ev_path)
    answers, stale = {}, 0
    sheet = cs.sheet(ev['recorder'])
    for d, c in ev['channels'].items():
        p = root / 'glm' / tape / f'CH{int(d):02d}.json'
        if not p.is_file():
            return None                     # tape not fully assessed yet
        rec = json.loads(p.read_text(encoding='utf-8'))
        answers[int(d)] = rec['parsed']
        stale += rec.get('key') != cl.prompt_key(cl.build_prompt(ev['recorder'], sheet, c, c['excerpt']), rec['model'])
    lp, _ = cv.emissions(answers, ev)
    # L[d-1, j] = log-lik of delivered d reading true(OFFSETS[j] - d); missing channels contribute 0
    L = np.zeros((30, len(OFFSETS)))
    for d, row in lp.items():
        for j, o in enumerate(OFFSETS):
            L[d - 1, j] = row[cv.true_of(-1, o, d)]
    prefix = np.vstack([np.zeros(len(OFFSETS)), np.cumsum(L, axis=0)])   # prefix[i] = sum of rows < i
    return dict(tape=tape, head=ev['head'], prefix=prefix, channels=len(lp), stale=stale)


def shapes():
    """Step positions p (a step after delivered CHp, 1..29), up to MAX_STEPS steps."""
    out = [()]
    for k in range(1, MAX_STEPS + 1):
        out += list(itertools.combinations(range(1, 30), k))
    return out


def tape_scores(t, shape_list):
    """score[shape index] = best base for this tape under each shape (no step cost); also argmax base."""
    pre = t['prefix']
    best = np.full(len(shape_list), -np.inf)
    base = np.zeros(len(shape_list), dtype=int)
    for i, steps in enumerate(shape_list):
        bounds = [0, *steps, 30]
        total = np.full(len(OFFSETS), 0.0)
        valid = np.ones(len(OFFSETS), dtype=bool)
        for k in range(len(bounds) - 1):
            lo, hi = bounds[k], bounds[k + 1]
            # base index j, segment uses offset index j - k
            seg = pre[hi] - pre[lo]
            shifted = np.full(len(OFFSETS), -np.inf)
            if k:
                shifted[k:] = seg[:-k]
            else:
                shifted = seg
            total = total + shifted
        j = int(np.argmax(total))
        best[i] = total[j]
        base[i] = OFFSETS[j]
    return best, base


def profile(steps, base):
    """Offset per delivered channel 1..30."""
    return [base - sum(d > p for p in steps) for d in range(1, 31)]


def describe(steps, base):
    segs, o, start = [], base, 1
    for p in [*steps, 30]:
        segs.append(f'{o}-d (CH{start:02d}-{p:02d})')
        o, start = o - 1, p + 1
    return '; '.join(segs)


def analyse(root: Path):
    tapes = sorted(p.name for p in (root / 'glm').glob('A15_T*') if p.is_dir())
    data = [t for t in (load_tape(root, x) for x in tapes) if t]
    shape_list = shapes()
    cost = np.array([STEP_COST * len(s) for s in shape_list])
    for t in data:
        t['scores'], t['bases'] = tape_scores(t, shape_list)
        own = int(np.argmax(t['scores'] - cost))
        t['own'] = own
        t['own_score'] = float(t['scores'][own] - cost[own])

    report = dict(tapes=len(data), max_steps=MAX_STEPS, groupings={}, per_tape={})
    for gname, key in GROUPINGS.items():
        groups = {}
        for t in data:
            groups.setdefault(key(t['head']), []).append(t)
        gout, total_loss, total_score = {}, 0.0, 0.0
        for g, members in sorted(groups.items()):
            summed = sum(m['scores'] for m in members) - cost
            si = int(np.argmax(summed))
            losses = {m['tape']: round(m['own_score'] - float(m['scores'][si] - cost[si]), 2) for m in members}
            total_loss += sum(losses.values())
            total_score += float(summed[si])
            # runner-up shape: best shape whose step positions differ
            order = np.argsort(-summed)
            runner = next(int(i) for i in order if shape_list[int(i)] != shape_list[si])
            gout[g] = dict(tapes=[m['tape'] for m in members], steps=list(shape_list[si]),
                           margin_over_next_shape=round(float(summed[si] - summed[runner]), 2),
                           next_shape=list(shape_list[runner]),
                           bases={m['tape']: int(m['bases'][si]) for m in members},
                           maps={m['tape']: describe(shape_list[si], int(m['bases'][si])) for m in members},
                           loss_per_tape=losses)
        n_params = sum(len(v['steps']) for v in gout.values())
        report['groupings'][gname] = dict(groups=gout, total_score=round(total_score, 2),
                                          total_loss=round(total_loss, 2), shape_steps=n_params)
    for t in data:
        report['per_tape'][t['tape']] = dict(head=t['head'], steps=list(shape_list[t['own']]),
                                             base=int(t['bases'][t['own']]),
                                             map=describe(shape_list[t['own']], int(t['bases'][t['own']])),
                                             profile=profile(shape_list[t['own']], int(t['bases'][t['own']])),
                                             stale_answers=t['stale'], channels=t['channels'])
    return report


def markdown(r):
    lines = ['# Stage 10 consistency of channel maps across tapes', '',
             f'{r["tapes"]} fully assessed tapes. Model: true = base(tape) + shape(d) - d, shape shared within a group, '
             f'up to {r["max_steps"]} one-track steps (each step costs {STEP_COST:.1f}). Scores are tempered '
             'log-likelihoods; "loss" is what a tape gives up when forced from its own best shape to its group\'s.', '',
             '## Groupings', '', '| Grouping | Groups | Total score | Total loss | Shared step parameters |',
             '|---|---:|---:|---:|---:|']
    for g, v in r['groupings'].items():
        lines.append(f'| {g} | {len(v["groups"])} | {v["total_score"]:.1f} | {v["total_loss"]:.1f} | {v["shape_steps"]} |')
    for g, v in r['groupings'].items():
        lines += ['', f'## Shared shape by {g}', '']
        for name, grp in v['groups'].items():
            steps = ', '.join(f'after CH{p:02d}' for p in grp['steps']) or 'none'
            nxt = ', '.join(f'CH{p:02d}' for p in grp['next_shape']) or 'none'
            lines += [f'**{name}** ({len(grp["tapes"])} tapes): steps {steps}; margin {grp["margin_over_next_shape"]:.1f} '
                      f'over next shape (steps after {nxt})', '',
                      '| Tape | Base | Group map | Loss vs own best |', '|---|---:|---|---:|']
            for t in grp['tapes']:
                lines.append(f'| {t} | {grp["bases"][t]} | {grp["maps"][t]} | {grp["loss_per_tape"][t]:.1f} |')
            lines.append('')
    lines += ['## Each tape\'s own best shape', '', '| Tape | Head | Own map | Stale GLM answers |', '|---|---|---|---:|']
    for t, v in sorted(r['per_tape'].items()):
        lines.append(f'| {t} | {v["head"]} | {v["map"]} | {v["stale_answers"]} |')
    return '\n'.join(lines) + '\n'


def main(argv=None):
    sys.stdout.reconfigure(encoding='utf-8')
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--root', default=str(DEFAULT_ROOT))
    args = p.parse_args(argv)
    root = Path(args.root)
    r = analyse(root)
    out = root / 'consistency'
    out.mkdir(parents=True, exist_ok=True)
    (out / 'consistency.json').write_text(json.dumps(r, indent=1), encoding='utf-8')
    (out / 'CONSISTENCY.md').write_text(markdown(r), encoding='utf-8')

    from rich.console import Console
    from rich.table import Table
    con = Console()
    tab = Table(title=f'Shape sharing across {r["tapes"]} tapes')
    for c in ('grouping', 'groups', 'score', 'loss', 'step params'):
        tab.add_column(c, justify='right' if c != 'grouping' else 'left')
    for g, v in r['groupings'].items():
        tab.add_row(g, str(len(v['groups'])), f'{v["total_score"]:.1f}', f'{v["total_loss"]:.1f}', str(v['shape_steps']))
    con.print(tab)
    for g in ('recorder', 'head stack'):
        t2 = Table(title=f'Shared shape per {g}')
        for c in ('group', 'tapes', 'steps after', 'margin', 'bases', 'worst losses'):
            t2.add_column(c)
        for name, grp in r['groupings'][g]['groups'].items():
            worst = sorted(grp['loss_per_tape'].items(), key=lambda kv: -kv[1])[:3]
            bases = {}
            for b in grp['bases'].values():
                bases[b] = bases.get(b, 0) + 1
            t2.add_row(name, str(len(grp['tapes'])), ', '.join(map(str, grp['steps'])) or '-',
                       f'{grp["margin_over_next_shape"]:.1f}',
                       ' '.join(f'{b}x{n}' for b, n in sorted(bases.items())),
                       ', '.join(f'{t[-3:]} {l:.0f}' for t, l in worst))
        con.print(t2)
    colours = {35: 'magenta', 34: 'red', 33: 'green', 32: 'cyan', 31: 'blue', 30: 'white'}
    con.print('[bold]Own best offset a (true = a - delivered), one digit per delivered channel '
              '(last digit of a: 4=34 3=33 2=32 1=31 0=30)[/]')
    con.print('                    ' + ''.join(str(d // 10) for d in range(1, 31)))
    con.print('                    ' + ''.join(str(d % 10) for d in range(1, 31)))
    last = None
    for t, v in sorted(r['per_tape'].items(), key=lambda kv: (kv[1]['head'], kv[0])):
        if last and last != v['head']:
            con.print('')
        last = v['head']
        cells = ''.join(f'[{colours.get(o, "yellow")}]{o % 10}[/]' for o in v['profile'])
        stale = f'  ({v["stale_answers"]} stale)' if v['stale_answers'] else ''
        con.print(f'{t:<12} {v["head"]:<6} {cells}{stale}')
    print(f'Wrote {out / "CONSISTENCY.md"}')


if __name__ == '__main__':
    main()
