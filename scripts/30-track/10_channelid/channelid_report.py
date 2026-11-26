"""Stage 10 outputs: per-tape map JSON and label, plus combined JSON, TSV and Markdown."""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

from channelid_sheets import IRIG_CHANNEL, sheet

FILE = re.compile(r'^A15_(T\d+)_(HR[12][UL])_CH(\d+)_(.+?)_(\d{3}-\d{2}-\d{2}-\d{2})_(\d{3}-\d{2}-\d{2}-\d{2})\.wav$')
MAP_VERSION = 'channelid-map-v5'


def parse_filename(name):
    m = FILE.match(name or '')
    if not m:
        return {}
    return dict(tape=m[1], head=m[2], channel=int(m[3]), label=m[4], start=m[5], stop=m[6])


def tape_map(tape, evidence, assessments, solution, model):
    """Machine-readable map for one tape. Keys of ``channels`` are delivered channel numbers."""
    sh = sheet(evidence['recorder'])
    files = {int(d): parse_filename(p['source_filename']) for d, p in evidence['provenance'].items()}
    first = next(iter(files.values()), {})
    channels = {}
    for d in sorted(solution['channels']):
        c, a, ev = solution['channels'][d], assessments[d], evidence['channels'][str(d)]
        t = c['true']
        channels[str(d)] = dict(
            delivered_channel=d, delivered_label=files.get(d, {}).get('label'),
            source_filename=evidence['provenance'][str(d)]['source_filename'],
            true_channel=t, true_label=sh[t]['label'] if t else None, true_sheet=sh[t]['sheet'] if t else None,
            true_position=sh[t]['position'] if t else None,
            confidence=c['confidence'], posterior=c['posterior'], alternatives=c['alternatives'],
            offset=c['offset'], agreement=c['agreement'], note=c['note'],
            glm_p_true=c['p_glm'], glm_top=c['glm_top'], glm_top_p=c['glm_top_p'], free_assignment=c['free'],
            glm_candidates=a['candidates'], glm_summary=a['summary'], glm_callsigns=a['callsigns'],
            glm_evidence=a['evidence'], words=ev['words'], own_words=ev['own_words'])
    mapped = {c['true_channel'] for c in channels.values() if c['true_channel']}
    anc = evidence['anchors']
    irig = solution.get('irig')
    true_channels = true_view(sh, channels, solution.get('shared', {}).get('group_missing', []), irig)
    return dict(
        version=MAP_VERSION, created=time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), model=model,
        tape=tape, head=evidence['head'], recorder=evidence['recorder'], reel=f'510-{tape[-3:]}',
        start=first.get('start'), stop=first.get('stop'),
        rule=solution['rule'], direction=solution['direction'], direction_posterior=solution['direction_posterior'],
        segments=solution['segments'], confidence=solution['confidence'],
        median_channel_posterior=solution['median_channel_posterior'], confirmed=solution['confirmed'],
        log_evidence=solution['log_evidence'], rigid=solution['rigid'],
        reference={k: dict(rule=v['rule'], score=v['score']) for k, v in solution['reference'].items()},
        anchor=dict(delivered_channel=solution['anchor_channel'], status=solution['anchor_status'],
                    strongest=anc['channel'], hits=anc['hits'], share=anc['share'],
                    consistent=solution['anchor_consistent'], hourly=anc['hourly'], ranking=anc['ranking']),
        true_channels=true_channels, irig=irig,
        true_channels_not_delivered=[t for t in range(1, 31) if t not in mapped],
        delivered_channels_without_true_track=[c['delivered_channel'] for c in channels.values()
                                               if not c['true_channel']],
        channels=channels,
        provenance=dict(evidence_version=evidence['version'], evidence_settings=evidence['settings'], transcripts={
            k: dict(source_filename=v['source_filename'], transcript_sha256=v['transcript_sha256'],
                    content_sha256=v['content_sha256']) for k, v in evidence['provenance'].items()}))


def true_view(sh, channels, group_missing, irig=None):
    """Every track-sheet channel 1..30: where it is in the delivery, or why it is missing."""
    by_true = {}
    for c in channels.values():
        if c['true_channel']:
            by_true.setdefault(c['true_channel'], []).append(c)
    lowest = min(by_true) if by_true else 31
    rows = []
    for t in range(1, 31):
        entry = dict(true_channel=t, true_label=sh[t]['label'], true_sheet=sh[t]['sheet'], true_position=sh[t]['position'])
        held = sorted(by_true.get(t, []), key=lambda c: c['delivered_channel'])
        if held:
            c = held[0]
            entry.update(status='delivered', delivered_channel=c['delivered_channel'],
                         delivered_label=c['delivered_label'], confidence=c['confidence'], posterior=c['posterior'],
                         also_delivered_on=[x['delivered_channel'] for x in held[1:]])
        else:
            if t == IRIG_CHANNEL:
                searched = ', '.join(f'CH{c:02d}' for c in (irig or {}).get('searched', []))
                reason = (f'IRIG time code not found on delivered {searched} (full-length decode); not digitized'
                          if searched else 'IRIG time code channel; not searched')
            elif t in group_missing:
                reason = "not delivered: gap in this head stack's pattern"
            elif t < lowest:
                reason = 'not delivered: beyond the last delivered channel'
            else:
                reason = 'not delivered: skipped between delivered channels'
            entry.update(status='missing', delivered_channel=None, reason=reason)
        rows.append(entry)
    return rows


def irig_line(m):
    irig = m.get('irig') or {}
    hit = [(int(c), v) for c, v in irig.get('channels', {}).items() if v['strict'] >= 1000]
    if hit:
        c, v = max(hit, key=lambda x: x[1]['strict'])
        tc = next((r for r in m.get('true_channels', []) if r['true_channel'] == IRIG_CHANNEL), {})
        where = 'mapped to true CH01, consistent' if tc.get('delivered_channel') == c else 'NOT mapped to true CH01'
        return (f'**Time code (IRIG-B):** delivered CH{c:02d}, {v["strict"]:,} strict frames '
                f'({v["seconds_share"]:.0%} of seconds), {v["first"]} to {v["last"]} GMT, '
                f'{v["rate"]:.4f} file-s per IRIG-s - {where}.  ')
    if irig.get('searched'):
        return ('**Time code (IRIG-B):** none on delivered ' + ', '.join(f'CH{c:02d}' for c in irig['searched'])
                + ' (full-length decode).  ')
    return '**Time code (IRIG-B):** not searched.  '


def label_markdown(m):
    """Human-readable correction label for one tape."""
    a = m['anchor']
    ref = m['reference']
    if a['delivered_channel']:
        anchor = (f'**Voice-annotation readout:** delivered CH{a["delivered_channel"]:02d} ({a["hits"]} readout lines, '
                  f'{a["share"]:.0%} of all) - ' + ('mapped to true CH30, consistent.' if a['consistent']
                                                    else 'NOT mapped to true CH30.'))
    elif a['status'].startswith('ambiguous'):
        anchor = (f'**Voice-annotation readout:** {a["status"]} ({a["ranking"]}); not used. Map puts true CH30 on '
                  + next((f'CH{int(d):02d}' for d, c in m['channels'].items() if c['true_channel'] == 30), 'none') + '.')
    else:
        anchor = f'**Voice-annotation readout:** {a["status"]} (strongest CH{a["strongest"] or 0:02d}, {a["hits"]} lines); not used.'
    lines = [
        f'# {m["tape"]}  {m["head"]}  reel {m["reel"]}  ({m["start"]} to {m["stop"]})', '',
        f'**Correction:** {m["rule"]}  ',
        f'**Numbering:** delivered channel numbers run '
        + ('opposite to the track sheet (counted from the other tape edge)' if m['direction'] == 'opposite'
           else 'the same way as the track sheet')
        + f' (posterior {m["direction_posterior"]:.4f}). Channel numbering only; the audio plays forward.  ',
        f'**Confidence:** {m["confidence"].upper()} (median channel posterior {m["median_channel_posterior"]:.2f}; '
        f'GLM independently names the mapped position on {m["confirmed"]}/{len(m["channels"])} channels)  ',
        f'**Rigid fits** (tempered log-likelihood): best `{ref["best_rigid"]["rule"]}` {ref["best_rigid"]["score"]:.1f}; '
        f'as delivered {ref["identity"]["score"]:.1f}; pure reversal (31 - d) {ref["pure_reverse"]["score"]:.1f}  ',
        anchor, irig_line(m), '',
        '| Delivered | File label | True | Track-sheet label (position) | Confidence | p | GLM | Evidence |',
        '|---:|---|---:|---|---|---:|---|---|']
    for d, c in sorted(m['channels'].items(), key=lambda kv: int(kv[0])):
        true = f'{c["true_channel"]:02d}' if c['true_channel'] else '-'
        label = f'{c["true_sheet"]} ({c["true_position"] or "-"})' if c['true_channel'] else '(no track)'
        summ = c['glm_summary'].replace('|', '/').replace('\n', ' ')
        glm = c['agreement']
        if c['agreement'] != 'confirmed':
            glm += f'; top {c["glm_top"]} @ {c["glm_top_p"]:.2f}'
        if c['note']:
            summ = f'[{c["note"]}] ' + summ
        lines.append(f'| CH{int(d):02d} | {c["delivered_label"]} | {true} | {label} | {c["confidence"]} | '
                     f'{c["posterior"]:.2f} | {glm} | {summ[:240]} |')
    tv = m.get('true_channels') or []
    missing = [r for r in tv if r['status'] == 'missing']
    lines += ['', f'## By track-sheet channel ({30 - len(missing)} of 30 delivered, {len(missing)} MISSING)', '',
              '| True | Track-sheet label (position) | Delivered | File label | Confidence |', '|---:|---|---|---|---|']
    for r in tv:
        head = f'| {r["true_channel"]:02d} | {r["true_sheet"]} ({r["true_position"] or "-"}) | '
        if r['status'] == 'missing':
            lines.append(head + f'**MISSING** | {r["reason"]} | - |')
        else:
            extra = (' (also ' + ', '.join(f'CH{x:02d}' for x in r['also_delivered_on']) + ')')                 if r['also_delivered_on'] else ''
            lines.append(head + f'CH{r["delivered_channel"]:02d}{extra} | {r["delivered_label"]} | '
                         f'{r["confidence"]} ({r["posterior"]:.2f}) |')
    lines += ['', 'True channels not present in the delivery: '
              + (', '.join(f'CH{t:02d}' for t in m['true_channels_not_delivered']) or 'none') + '  ',
              'Delivered channels with no true track: '
              + (', '.join(f'CH{d:02d}' for d in m['delivered_channels_without_true_track']) or 'none'), '']
    return '\n'.join(lines)


def write_tape(root: Path, m):
    (root / 'maps').mkdir(parents=True, exist_ok=True)
    (root / 'labels').mkdir(parents=True, exist_ok=True)
    (root / 'maps' / f'{m["tape"]}.json').write_text(json.dumps(m, indent=1, ensure_ascii=False), encoding='utf-8')
    (root / 'labels' / f'{m["tape"]}.md').write_text(label_markdown(m), encoding='utf-8')


def write_true_tsv(root: Path, maps):
    rows = ['\t'.join(('tape', 'head', 'true_channel', 'true_label', 'status', 'delivered_channel', 'delivered_label',
                       'confidence', 'posterior', 'reason'))]
    for m in maps:
        for r in m.get('true_channels', []):
            rows.append('\t'.join(str(x) for x in (
                m['tape'], m['head'], r['true_channel'], r['true_label'], r['status'],
                r.get('delivered_channel') or '', r.get('delivered_label') or '', r.get('confidence', ''),
                r.get('posterior', ''), r.get('reason', ''))))
    (root / 'true_channels.tsv').write_text('\n'.join(rows) + '\n', encoding='utf-8')


def write_combined(root: Path):
    maps = []
    for p in sorted((root / 'maps').glob('A15_T*.json')):
        m = json.loads(p.read_text(encoding='utf-8'))
        if m.get('version') == MAP_VERSION:
            maps.append(m)
    combined = {m['tape']: dict(head=m['head'], rule=m['rule'], direction=m['direction'], segments=m['segments'],
                                confidence=m['confidence'], anchor=m['anchor']['delivered_channel'],
                                anchor_consistent=m['anchor']['consistent'],
                                delivered_to_true={d: c['true_channel'] for d, c in m['channels'].items()},
                                true_to_delivered={str(r['true_channel']): r['delivered_channel']
                                                   for r in m.get('true_channels', [])},
                                missing_true={str(r['true_channel']): r['reason'] for r in m.get('true_channels', [])
                                              if r['status'] == 'missing'},
                                channel_confidence={d: c['confidence'] for d, c in m['channels'].items()},
                                channel_posterior={d: c['posterior'] for d, c in m['channels'].items()})
                for m in maps}
    (root / 'channel_map.json').write_text(json.dumps(combined, indent=1), encoding='utf-8')
    tsv = ['tape\thead\tdelivered_channel\tdelivered_label\ttrue_channel\ttrue_label\tconfidence\tposterior\t'
           'agreement\tglm_top\tglm_top_p\ttape_rule\ttape_confidence']
    for m in maps:
        for d, c in sorted(m['channels'].items(), key=lambda kv: int(kv[0])):
            tsv.append('\t'.join(str(x) for x in (
                m['tape'], m['head'], d, c['delivered_label'], c['true_channel'] or '', c['true_label'] or '',
                c['confidence'], c['posterior'], c['agreement'], c['glm_top'], c['glm_top_p'], m['rule'],
                m['confidence'])))
    (root / 'channel_map.tsv').write_text('\n'.join(tsv) + '\n', encoding='utf-8')
    write_true_tsv(root, maps)
    md = ['# Stage 10 channel identification: delivered to true channel', '',
          'Delivered CH*d* carries the track-sheet channel given by the correction. Per-tape labels with the '
          'evidence for every channel are in `labels/`; machine-readable maps in `maps/` and `channel_map.json`.', '',
          '| Tape | Head | Correction | Missing true channels | Confidence | Median p | GLM confirmed | Annotation on | '
          'Consistent |', '|---|---|---|---|---|---:|---:|---|---|']
    for m in maps:
        a = m['anchor']
        miss = ', '.join(str(r['true_channel']) for r in m['true_channels'] if r['status'] == 'missing')
        md.append(f'| {m["tape"]} | {m["head"]} | {m["rule"]} | {miss} | {m["confidence"].upper()} | '
                  f'{m["median_channel_posterior"]:.2f} | {m["confirmed"]}/{len(m["channels"])} | '
                  f'{"CH%02d" % a["delivered_channel"] if a["delivered_channel"] else a["status"]} | '
                  f'{ {True: "yes", False: "NO", None: "-"}[a["consistent"]] } |')
    (root / 'CHANNEL_MAP.md').write_text('\n'.join(md) + '\n', encoding='utf-8')
    return maps
