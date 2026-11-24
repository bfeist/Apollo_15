"""Step 4: speaker census and explicit map.

Every speaker label in every source is counted in
``scraped_data/speaker_census.tsv``. ``review/speakers.tsv`` maps each label
to the published ``who`` (CDR, CMP, LMP, CC, PAO, ...) and a name. The step
stops on any unmapped label; ``--seed`` appends proposals for review instead
of guessing silently. Labels are never truncated.
"""
from __future__ import annotations

import re
from collections import Counter

from md_common import REVIEW, SCRAPED, read_tsv, write_tsv

MAP_FIELDS = ('label', 'who', 'name', 'kind', 'status', 'note')
CENSUS_FIELDS = ('source', 'label', 'count', 'who')

CREW = {'Scott': ('CDR', 'David R. Scott'), 'Irwin': ('LMP', 'James B. Irwin'), 'Worden': ('CMP', 'Alfred M. Worden')}
CAPCOMS = {'Allen': 'Joseph P. Allen', 'Fullerton': 'C. Gordon Fullerton', 'Henize': 'Karl G. Henize',
           'Parker': 'Robert A. Parker', 'Brand': 'Vance D. Brand', 'Mitchell': 'Edgar D. Mitchell',
           'Gordon': 'Richard F. Gordon', 'Slayton': 'Donald K. Slayton', 'Swigert': 'John L. Swigert',
           'Schmitt': 'Harrison H. Schmitt'}
# Published who for transcript abbreviations, as printed in the 1971 transcripts.
TRANSCRIPT = {'CC': ('CC', 'Capsule Communicator', 'capcom'), 'CDR': ('CDR', 'David R. Scott', 'crew'),
              'LMP': ('LMP', 'James B. Irwin', 'crew'), 'CMP': ('CMP', 'Alfred M. Worden', 'crew'),
              'SC': ('SC', 'Spacecraft (unidentified crew member)', 'crew'),
              'S/C': ('SC', 'Spacecraft (unidentified crew member)', 'crew'),
              'CDR-EVA': ('CDR', 'David R. Scott', 'crew'), 'LMP-EVA': ('LMP', 'James B. Irwin', 'crew'),
              'CDR-LM': ('CDR', 'David R. Scott', 'crew'), 'LMP-LM': ('LMP', 'James B. Irwin', 'crew'),
              'PAO': ('PAO', 'Public Affairs Officer', 'pao'), 'MS': ('MS', 'Mission Specialist', 'ground'),
              'MCC': ('MCC', 'Mission Control', 'ground'), 'RECOVERY': ('Recovery', 'Recovery forces', 'recovery'),
              'HORNET': ('Recovery', 'Recovery forces', 'recovery'), 'OKINAWA': ('Recovery', 'USS Okinawa', 'recovery'),
              'SWIM 1': ('Swim 1', 'Recovery swimmer', 'recovery'), 'SWIM 2': ('Swim 2', 'Recovery swimmer', 'recovery'),
              'AIR BOSS': ('Air Boss', 'Recovery Air Boss', 'recovery'), 'PHOTO': ('Photo', 'Recovery photo helicopter', 'recovery'),
              'CDR-SEVA': ('CDR', 'David R. Scott', 'crew'), 'CMP-EVA': ('CMP', 'Alfred M. Worden', 'crew'),
              'CMP-LM': ('CMP', 'Alfred M. Worden', 'crew'), 'CT': ('Comm Tech', 'Communications technician', 'ground'),
              'LCC': ('LCC', 'Launch Control Center', 'ground'), 'CAPCOM': ('CC', 'Capsule Communicator', 'capcom'),
              'FALCON': ('SC', 'Falcon (LM crew)', 'crew'), 'ENDEAVOUR': ('CMP', 'Alfred M. Worden', 'crew'),
              'MADRID': ('Madrid', 'Madrid tracking station', 'ground'),
              'HONEYSUCKLE': ('Honeysuckle', 'Honeysuckle Creek tracking station', 'ground'),
              'TEXAS': ('Texas', 'Corpus Christi tracking station', 'ground'),
              'HOUSTON': ('CC', 'Capsule Communicator', 'capcom'), 'MOCR': ('MCC', 'Mission Control', 'ground'),
              'BOTH': ('SC', 'Spacecraft (two crew members)', 'crew')}
OTHERS = {
    'Goddard': ('Goddard', 'Goddard Space Flight Center', 'ground'),
    'Unknown': ('Unknown', 'Unidentified speaker', 'unknown'),
    'Unknown speaker': ('Unknown', 'Unidentified speaker', 'unknown'),
    'Unidentified voice': ('Unknown', 'Unidentified speaker', 'unknown'),
    'Unidentified speaker in Houston': ('Unknown', 'Unidentified speaker in Houston', 'unknown'),
    'Voice in Mission Control': ('Unknown', 'Voice in Mission Control', 'unknown'),
    'Speaker': ('Unknown', 'Unidentified speaker', 'unknown'),
    'Crew Member': ('SC', 'Spacecraft (unidentified crew member)', 'crew'),
    'Crewmember': ('SC', 'Spacecraft (unidentified crew member)', 'crew'),
    'Spacecraft': ('SC', 'Spacecraft (unidentified crew member)', 'crew'),
    'SC': ('SC', 'Spacecraft (unidentified crew member)', 'crew'),
    'S/C': ('SC', 'Spacecraft (unidentified crew member)', 'crew'),
    'LM Crew': ('SC', 'Lunar Module crew', 'crew'),
    'Falcon': ('SC', 'Falcon (LM crew)', 'crew'),
    'Endeavour': ('CMP', 'Alfred M. Worden', 'crew'),
    'MCC': ('MCC', 'Mission Control', 'ground'),
    'PAO': ('PAO', 'Public Affairs Officer', 'pao'),
    'Recovery': ('Recovery', 'Recovery forces', 'recovery'),
    'Okinawa': ('Recovery', 'USS Okinawa', 'recovery'),
    'Photo': ('Photo', 'Recovery photo helicopter', 'recovery'),
    'Photo 1': ('Photo', 'Recovery photo helicopter', 'recovery'),
    'Swim 1': ('Swim 1', 'Recovery swimmer', 'recovery'),
    'Swim 2': ('Swim 2', 'Recovery swimmer', 'recovery'),
    'ARIA': ('ARIA', 'Apollo Range Instrumentation Aircraft', 'recovery'),
    'Captain Huff': ('Recovery', 'Captain Huff, USS Okinawa', 'recovery'),
    'Chaplain': ('Recovery', 'USS Okinawa chaplain', 'recovery'),
    'Comm tech': ('Comm Tech', 'Communications technician', 'ground'),
    'Houston Comm Tech': ('Comm Tech', 'Houston communications technician', 'ground'),
    'Honeysuckle Comm Tech': ('Comm Tech', 'Honeysuckle Creek communications technician', 'ground'),
    'Surgeon': ('Surgeon', 'Flight Surgeon', 'ground'),
    'Silver': ('Silver', 'Leon T. Silver', 'ground'),
    'Griffin': ('Griffin', 'Gerald D. Griffin', 'ground'),
    'Hayward': ('Recovery', 'Hayward (recovery forces)', 'recovery'),
    'Clay': ('Recovery', 'Clay (recovery forces)', 'recovery'),
    'Music': ('', 'Music (non-speech)', 'event'),
}


def normalize_label(label: str) -> str:
    label = re.sub(r'\s+', ' ', label or '').strip()
    return label[:1].upper() + label[1:] if label else label


def default_entry(label: str) -> dict | None:
    """Known mapping for a label, combined labels ("Scott/Irwin") included."""
    fixed = {'SCott': 'Scott'}.get(label, label)
    crew = {k.casefold(): k for k in (*CREW, *CAPCOMS)}
    fixed = crew.get(fixed.casefold(), fixed)
    if fixed in CREW:
        who, name = CREW[fixed]
        return {'label': label, 'who': who, 'name': name, 'kind': 'crew', 'status': 'mapped', 'note': ''}
    if fixed in CAPCOMS:
        return {'label': label, 'who': 'CC', 'name': CAPCOMS[fixed], 'kind': 'capcom', 'status': 'mapped', 'note': ''}
    others = {k.casefold(): v for k, v in OTHERS.items()}
    if fixed.casefold() in others:
        who, name, kind = others[fixed.casefold()]
        return {'label': label, 'who': who, 'name': name, 'kind': kind, 'status': 'mapped', 'note': ''}
    if fixed.upper() in TRANSCRIPT:
        who, name, kind = TRANSCRIPT[fixed.upper()]
        return {'label': label, 'who': who, 'name': name, 'kind': kind, 'status': 'mapped', 'note': ''}
    if '/' in fixed:
        parts = [default_entry(p.strip()) for p in fixed.split('/')]
        if all(parts) and all(p['kind'] == 'crew' for p in parts):
            return {'label': label, 'who': '/'.join(p['who'] for p in parts), 'name': ' / '.join(p['name'] for p in parts),
                    'kind': 'crew', 'status': 'mapped', 'note': 'shared transmission'}
    return None


def census() -> Counter:
    counts: Counter = Counter()
    for source in ('afj', 'alsj'):
        for row in read_tsv(SCRAPED / f'journal_{source}.tsv'):
            if row['kind'] == 'utterance' and row['speaker_label']:
                counts[(source.upper(), normalize_label(row['speaker_label']))] += 1
    for edition in ('tec', 'pao', 'cm', 'lm'):
        for row in read_tsv(SCRAPED / f'transcript_{edition}.tsv'):
            label = (row.get('speaker') or '').strip()
            if label and (row.get('timed_row') or edition == 'pao'):
                counts[(edition.upper(), label)] += 1
    return counts


def load_map() -> dict[str, dict]:
    return {row['label']: row for row in read_tsv(REVIEW / 'speakers.tsv')}


def speaker_for(label: str, mapping: dict[str, dict]) -> dict | None:
    return mapping.get(normalize_label(label)) or mapping.get(label)


def main(args=None) -> int:
    counts = census()
    mapping = load_map()
    changed = False
    unmapped = []
    for (source, label), n in sorted(counts.items()):
        if label in mapping:
            continue
        entry = default_entry(label)
        if entry:
            mapping[label] = entry
            changed = True
        else:
            unmapped.append((source, label, n))
            if args is not None and getattr(args, 'seed', False):
                mapping[label] = {'label': label, 'who': '', 'name': '', 'kind': '', 'status': 'proposed',
                                  'note': f'first seen in {source} ({n}x)'}
                changed = True
    if changed or not (REVIEW / 'speakers.tsv').is_file():
        write_tsv(REVIEW / 'speakers.tsv', MAP_FIELDS, [mapping[k] for k in sorted(mapping, key=str.casefold)])
    write_tsv(SCRAPED / 'speaker_census.tsv', CENSUS_FIELDS,
              [{'source': s, 'label': l, 'count': n, 'who': (mapping.get(l) or {}).get('who', '')}
               for (s, l), n in sorted(counts.items())])
    proposed = [m for m in mapping.values() if m['status'] != 'mapped' or not m.get('kind')]
    print(f'speakers: {len(counts)} source labels, {len(mapping)} mapped labels, {len(unmapped)} unmapped, '
          f'{len(proposed)} awaiting review')
    for source, label, n in unmapped:
        print(f'  UNMAPPED {source}: {label!r} ({n}x)')
    if unmapped or proposed:
        print('Edit MISSION_DATA/review/speakers.tsv (status=mapped) and rerun.')
        return 3
    return 0
