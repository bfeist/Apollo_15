"""Measured head row (07a stagger) of each delivered channel beside its stage-10 true track. Read-only.

  uv run python research/event_check/rows_vs_map.py A15_T510075 [...]
"""
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from overlap_073_075 import EV  # noqa: E402
from sg_inputs import read_tsv  # noqa: E402

MAPS = json.loads((Path(__file__).parents[1] / 'readme_figures/channel_maps.json').read_text())['tapes']


def main():
    for tape in sys.argv[1:]:
        ed = sorted((EV / tape / 'sg_anchors').glob('anchors-*'))[-1]
        per = defaultdict(list)
        for r in read_tsv(ed / 'stagger.tsv'):
            if r['measured'] in ('True', True) and r['group'] not in ('', None):
                per[int(r['channel'])].append((int(r['interval']), float(r['stagger_s'])))
        late = max(float(r['tape_level_s']) for r in read_tsv(ed / 'stagger.tsv') if r['tape_level_s'] not in ('', None))
        m = MAPS[tape]
        print(f'{tape} {m["head"]} missing {m["missing_true"]} irig CH{m.get("irig_channel")}')
        for d in range(1, 31):
            c = m['channels'].get(str(d), {})
            v = per.get(d, [])
            rows = Counter('L' if abs(s - late) < 0.3 else ('E' if abs(s - late + 1.38) < 0.3 else '?') for _, s in v)
            seq = ''.join('L' if abs(s - late) < 0.3 else ('E' if abs(s - late + 1.38) < 0.3 else '?')
                          for _, s in sorted(v))
            true = c.get('true')
            print(f'  CH{d:02d} true {true if true else "-":>3} ({"odd " if true and true % 2 else "even" if true else "    "}) '
                  f'rows {dict(rows)} {seq[:60]}')


if __name__ == '__main__':
    main()
