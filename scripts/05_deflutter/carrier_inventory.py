"""Read source-bound survey checkpoints for the archive register.

Feature block hashes are verified by carrier_survey.load_survey when evidence is
used for planning or acceptance. Inventory does not reread every feature block.
"""
import hashlib
import json
from pathlib import Path

import carrier_survey


def load_inventory(path, require_complete=True):
    path = Path(path).resolve()
    if path.is_dir():
        path /= 'survey.json'
    index = json.loads(path.read_text(encoding='utf-8'))
    if require_complete and not index['complete']:
        raise ValueError('Carrier survey is incomplete')
    identity = carrier_survey.digest(carrier_survey.__file__)
    rows = []
    for record in index['files']:
        cache = (path.parent/record['cache']).resolve()
        if path.parent not in cache.parents:
            raise ValueError('Carrier survey artifact changed')
        raw = cache.read_bytes()
        if hashlib.sha256(raw).hexdigest() != record['sha256']:
            raise ValueError('Carrier survey artifact changed')
        row = json.loads(raw)
        if (row['source'] != carrier_survey.source_record(record['path']) or
                row['settings'] != carrier_survey.SETTINGS or
                row['implementation_sha256'] != identity):
            raise ValueError('Stale carrier survey')
        if (record['complete'] != row['complete'] or
                row['complete'] != (row['next_sample'] >= row['source']['frames']) or
                (index['complete'] and not row['complete'])):
            raise ValueError('Carrier survey completion disagrees with its checkpoint')
        rows.append(row)
    if not rows:
        raise ValueError('Carrier survey has no source checkpoints')
    return index, rows
