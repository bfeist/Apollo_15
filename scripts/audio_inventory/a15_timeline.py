"""Apollo 15 audio by GET on a fixed hourly grid: JSC reels, NARA tapes and what we hold.

The JSC SG reels, the NARA flight tapes and the GOSS reels all start and end at
different times, so the rows are a fixed GET grid (one hour by default). Each
row lists every tape that overlaps that hour.

*SG audio %* is the share of the hour that our SG reel audio actually covers.
The reels are edited commentary, so this comes from the coarse reel-to-GET
offsets (``.local/mission_data/sg/sg_offsets.tsv``, 60-s grid). It is not
taken from the reels' nominal spans. A reel with no measured offsets falls back
to its nominal span and is marked ``~``. *Held %* also counts the downloaded
NARA flight tapes, which run continuously. A gap is a run of hours whose held
share falls below the threshold (25 %). The offsets exist only where words
matched, so SG audio % undercounts; partly held hours above the threshold are
not listed as gaps.

The span of each JSC reel comes from the PAO edition's segment headers, using
the same rule as ``md_sg.build_reels``. This also places reels 584–601-AAA,
which are not online, from their JSC index rows.
"""
from __future__ import annotations

import math
import re

from a15_nara import LOCAL, SPLASHDOWN, overlap, read_tsv, union
from md_common import format_get

FIELDS = ('get_start', 'get_end', 'jsc_reels', 'nara_flight_tape', 'flight_tape_status', 'nara_goss',
          'sg_audio_pct', 'held_pct', 'gap')


def jsc_spans(jsc_rows: list[dict], headers: dict[int, int], have: set[str]) -> list[dict]:
    """Apollo 15 Mission Commentary reels with their GET spans from PAO segment headers."""
    segs = sorted(headers)
    out = []
    for r in jsc_rows:
        m = re.search(r'Commentary (\d+)-(\d+|EOM)', r['jsc_description'])
        if r['mission'] != 'Apollo 15' or not m:
            continue
        first = int(m.group(1))
        last = int(m.group(2)) if m.group(2).isdigit() else max(segs)
        starts = [s for s in segs if first <= s <= last]
        after = [s for s in segs if s > last]
        if starts:
            out.append({'reel': r['key'], 'start': headers[starts[0]],
                        'end': headers[after[0]] if after else SPLASHDOWN, 'online': r['key'] in have})
    return out


def sg_audio_by_reel() -> dict[str, list[tuple[int, int]]]:
    spans: dict[str, list] = {}
    for r in read_tsv(LOCAL / 'sg' / 'sg_offsets.tsv'):
        if r.get('rank') == '1':
            g = round(float(r['source_s']) + float(r['offset_s']))
            spans.setdefault(r['reel'], []).append((g, g + 60))
    return {k: union(v) for k, v in spans.items()}


def _compress(numbers) -> str:
    """[3004, 3005, 3006, 3252] -> '03004–03006, 03252'."""
    runs = []
    for n in sorted(set(numbers)):
        if runs and n == runs[-1][-1] + 1:
            runs[-1].append(n)
        else:
            runs.append([n])
    return ', '.join(f'{r[0]:05d}' if len(r) == 1 else f'{r[0]:05d}–{r[-1]:05d}' for r in runs)


def build(jsc: list[dict], nara: list[dict], step: int = 3600, threshold: float = 25) -> list[dict]:
    tapes = [r for r in nara if r['kind'] == 'flight_tape' and r['get_start_s'] is not None]
    goss = [r for r in nara if r['kind'] == 'goss' and r['get_start_s'] is not None]
    measured = sg_audio_by_reel()
    estimated = {j['reel'] for j in jsc if j['online'] and j['reel'] not in measured}
    sg = union([s for j in jsc if j['online'] for s in measured.get(j['reel'], [(j['start'], j['end'])])])
    nara_held = union([(t['get_start_s'], t['get_end_s']) for t in tapes if t['local']])
    held = union(sg + nara_held)
    lo = math.floor(min([j['start'] for j in jsc] + [t['get_start_s'] for t in tapes]) / step) * step
    hi = math.ceil(SPLASHDOWN / step) * step
    rows = []
    for a in range(lo, hi, step):
        b = a + step
        reels = [j for j in jsc if j['start'] < b and j['end'] > a]
        tape = [t for t in tapes if t['get_start_s'] < b and t['get_end_s'] > a]
        gs = [int(g['nara_local_id'].rsplit('-', 1)[-1]) for g in goss if g['get_start_s'] < b and g['get_end_s'] > a]
        sg_pct = round(100 * overlap(a, b, sg) / step)
        held_pct = round(100 * overlap(a, b, held) / step)
        label = lambda j: j['reel'] + ('' if j['online'] else ' (not online)') + ('~' if j['reel'] in estimated else '')
        status = lambda t: 'downloaded' if t['local'] else 'digitized' if int(t['nara_objects'] or 0) \
            else 'not digitized'
        rows.append({'start_s': a, 'end_s': b, 'get_start': format_get(a), 'get_end': format_get(b),
                     'jsc_reels': ', '.join(map(label, reels)),
                     'nara_flight_tape': ', '.join(t['nara_local_id'][9:12] for t in tape),
                     'flight_tape_status': ', '.join(map(status, tape)), 'nara_goss': _compress(gs), 'goss_numbers': gs,
                     'sg_audio_pct': sg_pct, 'held_pct': held_pct, 'gap': 'GAP' if held_pct < threshold else ''})
    return rows


def markdown(rows: list[dict], threshold: float = 25) -> str:
    step = rows[0]['end_s'] - rows[0]['start_s']
    gaps = []
    for r in rows:
        if r['gap']:
            if gaps and gaps[-1][-1]['end_s'] == r['start_s']:
                gaps[-1].append(r)
            else:
                gaps.append([r])
    held = sum(r['held_pct'] for r in rows) * step / 100 / 3600
    lines = ['# Apollo 15 audio by GET', '',
             'Generated by `uv run python scripts/run.py audio_inventory.py timeline`. Each row is one hour of GET '
             'and lists every tape that overlaps it. **JSC reels** are the PAO/SG mission-commentary reels, placed '
             'on GET from the PAO edition\'s segment headers. **NARA flight tapes** are '
             '`255-PAOa-6NN-AAA`: 6-hour blocks with four tracks, one of them timecode. **GOSS** reels are '
             '`255-AVTa-15-0NNNN`, the air-to-ground net in reels of about 95 minutes. **SG audio %** is the '
             'share of the hour covered by our edited SG reels, from the measured reel offsets (`~` = reel '
             'without measured offsets, nominal span used). **Held %** adds the downloaded flight tapes.', '',
             f'Held: **{held:.1f} h** of {len(rows) * step / 3600:.0f} h. A gap is a run of hours with less '
             f'than {threshold:.0f}% held.', '',
             '## Gaps', '', '| GET start | GET end | Hours | Held % | JSC reels | NARA flight tapes | NARA GOSS |',
             '|---|---|---|---|---|---|---|']
    for g in gaps:
        reels = ', '.join(dict.fromkeys(x for r in g for x in r['jsc_reels'].split(', ') if x))
        tapes = ', '.join(dict.fromkeys(f'{t} ({s})' for r in g for t, s in
                                        zip(r['nara_flight_tape'].split(', '), r['flight_tape_status'].split(', '))
                                        if t))
        goss = _compress(n for r in g for n in r['goss_numbers'])
        pct = round(sum(r['held_pct'] for r in g) / len(g))
        lines.append(f'| {g[0]["get_start"]} | {g[-1]["get_end"]} | {len(g) * step / 3600:.0f} | {pct} | '
                     f'{reels} | {tapes} | {goss} |')
    lines += ['', '## By hour', '',
              '| GET | JSC reels | NARA flight tape | Flight tape | NARA GOSS 15-… | SG audio % | Held % | |',
              '|---|---|---|---|---|---|---|---|']
    for r in rows:
        lines.append(f'| {r["get_start"][:-3]}–{r["get_end"][:-3]} | {r["jsc_reels"]} | {r["nara_flight_tape"]} | '
                     f'{r["flight_tape_status"]} | {r["nara_goss"]} | {r["sg_audio_pct"]} | {r["held_pct"]} | '
                     f'{"**GAP**" if r["gap"] else ""} |')
    return '\n'.join(lines) + '\n'
