"""Collect archive-wide statistics for the README figures from published records.

Reads only JSON/NPY metadata under D:/Apollo_15_30-Track (no audio) and writes
research/readme_figures/stats.json. Nothing in the archive is modified.
"""
import json
from pathlib import Path

import numpy as np

ROOT = Path('D:/Apollo_15_30-Track')
OUT = Path(__file__).with_name('stats.json')


def load(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def main():
    stats = dict(tapes={}, denoise=[], crosstalk=[], deflutter={})
    for tape_dir in sorted((ROOT/'orig_concatenated').glob('A15_T*')):
        tape = tape_dir.name
        row = dict(channels=len(list(tape_dir.glob('*.wav'))))
        plan_path = ROOT/'deflutter_maps'/tape/'plan.json'
        if plan_path.exists():
            plan = load(plan_path)
            m, c = plan['map'], plan.get('census') or {}
            row.update(duration=plan['inventory']['duration_seconds'], coverage=m['coverage'],
                       supported=m['supported_seconds'], bypass=m['bypass_seconds'],
                       census_selected=m.get('census_selected_seconds'), excluded=m.get('excluded_seconds'),
                       max_speed=m['maximum_speed_correction'], max_disp=m['maximum_displacement_seconds'],
                       dc=m['dc_speed_removed'], census_seconds=c.get('seconds'), partition=c.get('partition'),
                       run_id=c.get('run_id'), analysis_id=c.get('analysis_id'),
                       offsets={str(r['channel']): r['start_seconds'] - r.get('acquisition_start_seconds', r['start_seconds'])
                                for r in plan['inventory']['channels']},
                       starts={str(r['channel']): r['start_seconds'] for r in plan['inventory']['channels']},
                       acq={str(r['channel']): r.get('acquisition_start_seconds') for r in plan['inventory']['channels']},
                       sources=len(plan.get('sources', [])))
            sel = load(ROOT/'deflutter_maps'/tape/'selection.json')
            kinds = dict(census=0., saved=0., excluded=0., unsupported=0.)
            for s in sel:
                dur = s['end_seconds'] - s['start_seconds']
                key = s['selected']
                kind = ('excluded' if s.get('excluded') else 'unsupported') if key is None else (
                    'census' if 'census' in key else 'saved')
                kinds[kind] += dur
            row['selection_seconds'] = kinds
        stats['tapes'][tape] = row
        for ch_dir in sorted((ROOT/'denoised'/tape).glob('CH*')):
            p = ch_dir/'denoised.json'
            if p.exists():
                d = load(p)
                stats['denoise'].append(dict(tape=tape, channel=ch_dir.name, algorithm=d.get('algorithm'),
                                             gain=d.get('gain'), status=d.get('status')))
        for ch_dir in sorted((ROOT/'crosstalk_removed'/tape).glob('CH*')):
            p = ch_dir/'crosstalk.json'
            if p.exists():
                d = load(p)
                ev = d.get('events', [])
                stats['crosstalk'].append(dict(
                    tape=tape, channel=ch_dir.name, status=d.get('status'), direction=d.get('direction'),
                    eligible=d.get('eligible_seconds'), applied=d.get('applied_seconds'),
                    events=len(ev), applied_events=sum(e.get('status') == 'applied' for e in ev),
                    reasons=sorted({e.get('reason') for e in ev if e.get('status') != 'applied' and e.get('reason')}),
                    delays=[e.get('echo_to_main_seconds') for e in ev if e.get('status') == 'applied']))
    stats['deflutter']['published_channels'] = sum(1 for _ in (ROOT/'defluttered_shared').glob('*/CH*/deflutter.wav'))
    OUT.write_text(json.dumps(stats, indent=1), encoding='utf-8')
    t = [v for v in stats['tapes'].values() if 'coverage' in v]
    total = sum(v['duration'] for v in t)
    print('tapes with maps', len(t), 'hours', total/3600,
          'union coverage', sum(v['supported'] for v in t)/total)
    print('denoise channels', len(stats['denoise']), {d['algorithm'] for d in stats['denoise']})
    x = stats['crosstalk']
    print('crosstalk channels', len(x), 'applied events', sum(r['applied_events'] for r in x),
          'applied seconds', sum(r['applied'] or 0 for r in x),
          'channels with any applied', sum(1 for r in x if r['applied_events']))
    print('published 05b', stats['deflutter']['published_channels'])


if __name__ == '__main__':
    main()
