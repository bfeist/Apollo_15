"""Where NASA mission audio is available: JSC index, Internet Archive, NARA, local.

Subcommands (run in order by ``all``):
  jsc       parse the JSC audio-archive metadata PDF      -> jsc_index.tsv
  ia        list Internet Archive historical audio files  -> ia_files.tsv
  nara      search the NARA catalog per mission           -> nara_records.tsv
  join      one row per tape key, all sources              -> inventory.tsv, inventory_summary.tsv
  a15       Apollo 15 NARA records on GET vs our SG reels  -> a15_nara_get.tsv, a15_nara_get.md
  timeline  Apollo 15 by GET: JSC reels, NARA tapes, gaps   -> a15_get_timeline.tsv, a15_get_timeline.md
  download  fetch digitized NARA Apollo 15 records that fill SG gaps (``--all`` for every digitized one,
            ``--local-id`` for specific records)          -> a15_nara_downloads.tsv

Tables and cached API responses go to .local/mission_data/audio_inventory/ (not
committed; ``--refresh`` refetches). NARA queries use
NARA_API_KEY from the environment or .env when present.

Example:
  uv run python scripts/run.py audio_inventory.py all
  uv run python scripts/run.py audio_inventory.py download
"""
from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))
from _bootstrap import install as _install
_install()

import argparse
import collections
import json
import re

import a15_nara
import a15_timeline
import ia_catalog
import jsc_index
import nara_catalog
from inventory_common import (CACHE, EXTRA_MISSIONS, IA_AUDIO_ROOT, NARA_AUDIO_ROOT, OUT, CachedJSON, ia_file_key,
                              mission_of_text, mission_sort_key, nara_key, nara_queries)
from md_common import read_tsv, write_if_changed, write_json, write_tsv

JSC_TSV, IA_TSV, NARA_TSV = OUT / 'jsc_index.tsv', OUT / 'ia_files.tsv', OUT / 'nara_records.tsv'
OBJECTS = CACHE / 'nara_objects.json'  # digital objects (file URLs) by NARA local identifier
INVENTORY_FIELDS = ('mission', 'key', 'sources', 'jsc_description', 'jsc_date', 'jsc_met_start', 'jsc_met_end',
                    'ia_identifier', 'ia_files', 'nara_local_id', 'nara_title', 'nara_objects', 'nara_url',
                    'local', 'mission_check')
SUMMARY_FIELDS = ('mission', 'keys', 'jsc', 'ia', 'nara', 'nara_digital', 'jsc_on_ia', 'jsc_on_nara',
                  'jsc_nowhere_online', 'nara_not_in_jsc', 'local')


def cmd_jsc(args):
    rows = jsc_index.parse()
    write_tsv(JSC_TSV, jsc_index.FIELDS, rows)
    print(f'{len(rows)} JSC rows, {len({r["mission"] for r in rows})} missions -> {JSC_TSV}')


def cmd_ia(args):
    http = CachedJSON('ia', delay=0.3, refresh=args.refresh)
    items = [i for i in ia_catalog.items(http) if ia_catalog.selected(i)]
    rows = []
    for n, item in enumerate(items, 1):
        rows.extend(ia_catalog.files(item, http))
        if n % 100 == 0:
            print(f'  {n}/{len(items)} items', flush=True)
    rows.sort(key=lambda r: (r['ia_identifier'], r['ia_file']))
    write_tsv(IA_TSV, ia_catalog.FIELDS, rows)
    print(f'{len(rows)} audio files in {len(items)} items -> {IA_TSV}')


def cmd_nara(args):
    http, base = nara_catalog.client(args.refresh)
    missions = sorted({r['mission'] for r in read_tsv(JSC_TSV)} | set(EXTRA_MISSIONS), key=mission_sort_key)
    if args.mission:
        missions = [m for m in missions if m in args.mission]
    found: dict[int, dict] = {}
    objects = json.loads(OBJECTS.read_text(encoding='utf-8')) if OBJECTS.is_file() else {}
    for mission in missions:
        for phrase in nara_queries(mission):
            records = nara_catalog.search(phrase, http, base)
            print(f'{mission:<14} {phrase:<30} {len(records):>5}', flush=True)
            for record in records:
                if record.get('digitalObjects'):
                    objects[record.get('localIdentifier') or str(record['naId'])] = nara_catalog.objects(record)
                row = found.setdefault(record['naId'], {**nara_catalog.row(record), 'missions': set(), 'q': set()})
                row['missions'].add(mission)
                row['q'].add(phrase)
    rows = []
    for row in found.values():
        named = mission_of_text(row['nara_title'])
        candidates = sorted(row.pop('missions'), key=mission_sort_key)
        row['mission'] = named if named in candidates else (candidates[0] if len(candidates) == 1 else named
                                                            or candidates[0])
        row['nara_queries'] = ' '.join(sorted(row.pop('q')))
        rows.append(row)
    rows.sort(key=lambda r: (mission_sort_key(r['mission']), r['nara_local_id'], r['nara_naid']))
    write_tsv(NARA_TSV, ('mission', *nara_catalog.FIELDS), rows)
    write_json(OBJECTS, objects)
    print(f'{len(rows)} NARA records ({http.requests} new requests) -> {NARA_TSV}')


def local_index() -> dict[str, list[str]]:
    out = collections.defaultdict(list)
    nara_folder = lambda name, path: f'NARA {path.parent.name}' if path.parent != NARA_AUDIO_ROOT else ''
    for root, keyer in ((IA_AUDIO_ROOT, lambda name, path: ia_file_key(name)), (NARA_AUDIO_ROOT, nara_folder)):
        if root.is_dir():
            for path in root.rglob('*'):
                if path.suffix.lower() in ('.wav', '.mp3', '.flac') and (key := keyer(path.name, path)):
                    out[key].append(str(path.relative_to(root.parent)).replace('\\', '/'))
    return out


def cmd_join(args):
    jsc, ia, nara = read_tsv(JSC_TSV), read_tsv(IA_TSV), read_tsv(NARA_TSV)
    local = local_index()
    by = collections.defaultdict(lambda: {'jsc': [], 'ia': [], 'nara': []})
    for r in jsc:
        by[r['key'] or f'jsc:p{r["jsc_page"]}:{r["jsc_file"]}']['jsc'].append(r)
    for r in ia:
        by[r['key'] or f'ia:{r["ia_identifier"]}']['ia'].append(r)
    # NARA numbers its series independently (255-NWSa-540-AAA is STS-9, JSC 540-AAA is Apollo 15),
    # so a NARA record joins a JSC tape only when the mission also agrees.
    jsc_missions = collections.defaultdict(set)
    for r in jsc:
        jsc_missions[r['key']].add(r['mission'])
    for r in nara:
        same = r['key'] and r['mission'] in jsc_missions.get(r['key'], ())
        by[r['key'] if same else f'NARA {r["nara_local_id"] or r["nara_naid"]}']['nara'].append(r)
    rows = []
    for key, s in by.items():
        jm = {r['mission'] for r in s['jsc']}
        nm = {r['mission'] for r in s['nara'] if r['mission']}
        im = {mission_of_text(r['ia_identifier'] + ' ' + r['ia_title']) for r in s['ia']} - {''}
        # A bare tape number recurs across missions; keep JSC's mission and report disagreements.
        mission = sorted(jm or nm or im or {'General'}, key=mission_sort_key)[0]
        check = []
        if s['jsc'] and s['nara']:
            check.append('nara agrees' if jm & nm else f'nara says {"/".join(sorted(nm))}')
        if s['jsc'] and im:
            check.append('ia agrees' if jm & im else f'ia says {"/".join(sorted(im))}')
        sources = [n for n in ('jsc', 'ia', 'nara') if s[n]]
        if any(int(r['nara_objects'] or 0) for r in s['nara']):
            sources[-1] = 'nara(digital)'
        loc = local.get(key, []) if not key.startswith(('jsc:', 'ia:')) else []
        if loc:
            sources.append('local')
        join = lambda rs, f: ' | '.join(dict.fromkeys(r[f] for r in rs if r[f]))
        rows.append({'mission': mission, 'key': key, 'sources': '+'.join(sources),
                     'jsc_description': join(s['jsc'], 'jsc_description'), 'jsc_date': join(s['jsc'], 'jsc_date'),
                     'jsc_met_start': join(s['jsc'], 'jsc_met_start'), 'jsc_met_end': join(s['jsc'], 'jsc_met_end'),
                     'ia_identifier': join(s['ia'], 'ia_identifier'), 'ia_files': len(s['ia']),
                     'nara_local_id': join(s['nara'], 'nara_local_id'), 'nara_title': join(s['nara'], 'nara_title'),
                     'nara_objects': sum(int(r['nara_objects'] or 0) for r in s['nara']),
                     'nara_url': join(s['nara'], 'nara_url'), 'local': len(loc), 'mission_check': '; '.join(check)})
    rows.sort(key=lambda r: (mission_sort_key(r['mission']), _key_order(r['key'])))
    write_tsv(OUT / 'inventory.tsv', INVENTORY_FIELDS, rows)
    summary = []
    for mission, group in sorted(_group(rows).items(), key=lambda kv: mission_sort_key(kv[0])):
        has = lambda r, s: s in r['sources'].split('+') or (s == 'nara' and 'nara(digital)' in r['sources'])
        j = [r for r in group if has(r, 'jsc')]
        summary.append({'mission': mission, 'keys': len(group), 'jsc': len(j),
                        'ia': sum(has(r, 'ia') for r in group), 'nara': sum(has(r, 'nara') for r in group),
                        'nara_digital': sum('nara(digital)' in r['sources'] for r in group),
                        'jsc_on_ia': sum(has(r, 'ia') for r in j), 'jsc_on_nara': sum(has(r, 'nara') for r in j),
                        'jsc_nowhere_online': sum(not has(r, 'ia') and 'nara(digital)' not in r['sources'] for r in j),
                        'nara_not_in_jsc': sum(has(r, 'nara') and not has(r, 'jsc') for r in group),
                        'local': sum(bool(r['local']) for r in group)})
    write_tsv(OUT / 'inventory_summary.tsv', SUMMARY_FIELDS, summary)
    head = '| ' + ' | '.join(SUMMARY_FIELDS) + ' |\n|' + '---|' * len(SUMMARY_FIELDS) + '\n'
    body = ''.join('| ' + ' | '.join(str(r[f]) for f in SUMMARY_FIELDS) + ' |\n' for r in summary)
    write_if_changed(OUT / 'inventory_summary.md', (head + body).encode('utf-8'))
    print(f'{len(rows)} keys in {len(summary)} missions -> {OUT / "inventory.tsv"}')


def _group(rows):
    out = collections.defaultdict(list)
    for r in rows:
        out[r['mission']].append(r)
    return out


def _key_order(key: str):
    m = re.match(r'(\d+)-?(\w*)', key)
    return (0, m.group(2), int(m.group(1)), key) if m else (1, '', 0, key)


def a15_rows():
    return a15_nara.table([r for r in read_tsv(NARA_TSV) if r['mission'] == 'Apollo 15'])


def cmd_a15(args):
    rows = a15_rows()
    write_tsv(OUT / 'a15_nara_get.tsv', a15_nara.FIELDS, rows)
    text = a15_nara.markdown(rows)
    write_if_changed(OUT / 'a15_nara_get.md', text.encode('utf-8'))
    print(text if args.print else f'{len(rows)} Apollo 15 NARA records -> {OUT / "a15_nara_get.md"}')


def cmd_timeline(args):
    import md_sg
    have = {r['reel'] for r in read_tsv(md_sg.REELS_TSV)}
    jsc = a15_timeline.jsc_spans(read_tsv(JSC_TSV), md_sg.pao_segment_gets(), have)
    rows = a15_timeline.build(jsc, a15_rows())
    write_tsv(OUT / 'a15_get_timeline.tsv', a15_timeline.FIELDS, rows)
    write_if_changed(OUT / 'a15_get_timeline.md', a15_timeline.markdown(rows).encode('utf-8'))
    print(f'{len(rows)} GET intervals -> {OUT / "a15_get_timeline.md"}')


def cmd_download(args):
    rows = a15_rows()
    pick = [r for r in rows if int(r['nara_objects'] or 0) and
            (r['nara_local_id'] in args.local_id if args.local_id else args.all or r.get('needed') == 'yes')]
    objects = json.loads(OBJECTS.read_text(encoding='utf-8'))
    records = {r['nara_local_id']: objects.get(r['nara_local_id'], []) for r in pick}
    total = sum(o['size'] for v in records.values() for o in v)
    print(f'{len(records)} records, {sum(map(len, records.values()))} files, {total / 1e9:.1f} GB -> {NARA_AUDIO_ROOT}')
    if args.dry_run:
        for k, v in records.items():
            print(f'  {k}: {", ".join(o["name"] for o in v)}')
        return
    a15_nara.download(records)


def main(argv=None):
    parser = argparse.ArgumentParser(prog='audio_inventory.py', description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='command', required=True)
    for name in ('jsc', 'ia', 'nara', 'join', 'a15', 'timeline', 'all'):
        p = sub.add_parser(name)
        p.add_argument('--refresh', action='store_true', help='refetch cached API responses')
        p.add_argument('--mission', action='append', default=[], help='nara: only these missions ("Apollo 15")')
        p.add_argument('--print', action='store_true', help='a15: print the table')
    p = sub.add_parser('download')
    p.add_argument('--all', action='store_true', help='every digitized Apollo 15 record, not only SG gaps')
    p.add_argument('--local-id', action='append', default=[], help='specific NARA local identifiers')
    p.add_argument('--dry-run', action='store_true')
    args = parser.parse_args(argv)
    steps = {'jsc': cmd_jsc, 'ia': cmd_ia, 'nara': cmd_nara, 'join': cmd_join, 'a15': cmd_a15,
             'timeline': cmd_timeline, 'download': cmd_download}
    for name in (['jsc', 'ia', 'nara', 'join', 'a15', 'timeline'] if args.command == 'all' else [args.command]):
        steps[name](args)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
