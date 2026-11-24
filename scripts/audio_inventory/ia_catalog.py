"""Internet Archive: the historical audio JSC uploaded to ``nasaaudiocollection``.

The collection also holds thousands of recent ISS and podcast items. Item
metadata (file lists) is read for the historical sub-collections, for items
named after a tape (``045-AAC_STS-3_Mission_Audio.wav``) and for items whose
name or title cites a historical programme. Responses are cached under
``.local/mission_data/audio_inventory/ia``.
"""
from __future__ import annotations

import re

from inventory_common import CachedJSON, ia_file_key, url_with

SCRAPE = 'https://archive.org/services/search/v1/scrape'
COLLECTION = 'nasaaudiocollection'
HISTORICAL = {'apolloaudiocollection', 'geminiaudiocollection', 'mercuryaudiocollection',
              'skylabaudiocollection', 'apollosoyuztestprogram', 'spaceshuttleaudiocollection',
              'audiohighlightreels'}
PROGRAMME = re.compile(r'apollo|gemini|mercury|skylab|soyuz|astp|voyager|\bsts[-_ ]?\d|^a1\d_', re.I)
AUDIO = ('.wav', '.flac', '.mp3', '.aif', '.aiff')
FIELDS = ('key', 'ia_identifier', 'ia_file', 'ia_title', 'ia_collection', 'ia_size', 'ia_md5', 'ia_url')


def items(http: CachedJSON) -> list[dict]:
    out, cursor = [], None
    while True:
        params = {'q': f'collection:{COLLECTION}', 'fields': 'identifier,title,collection', 'count': 10000}
        if cursor:
            params['cursor'] = cursor
        body = http.get(url_with(SCRAPE, params))
        out.extend(body.get('items', []))
        cursor = body.get('cursor')
        if not cursor:
            return out


def _collections(item) -> list[str]:
    value = item.get('collection') or []
    return [value] if isinstance(value, str) else list(value)


def selected(item) -> bool:
    title = item.get('title') or ''
    title = title if isinstance(title, str) else ' '.join(title)
    return bool(HISTORICAL & set(_collections(item)) or ia_file_key(item['identifier'])
                or PROGRAMME.search(item['identifier']) or PROGRAMME.search(title))


# Apollo 11/13 Mission Operations Control Room 30-track transfers: one channel file per item,
# named after the file (A11_T648_HR1U_CH11_02-47-56_11-17-56_-_2015-10-01_Recording.wav).
CHANNEL_ITEM = re.compile(r'A1\d_T\d+_')


def files(item: dict, http: CachedJSON) -> list[dict]:
    """One row per original audio file of an item (derived MP3/OGG copies skipped)."""
    identifier = item['identifier']
    sub = ';'.join(sorted(set(_collections(item)) - {COLLECTION, 'nasa'}))
    if CHANNEL_ITEM.match(identifier):
        return [{'key': '', 'ia_identifier': identifier, 'ia_file': identifier, 'ia_title': item.get('title') or '',
                 'ia_collection': sub, 'ia_size': '', 'ia_md5': '',
                 'ia_url': f'https://archive.org/details/{identifier}'}]
    meta = http.get(f'https://archive.org/metadata/{identifier}')
    title = (meta.get('metadata') or {}).get('title') or identifier
    title = title if isinstance(title, str) else ' '.join(title)
    rows = []
    for f in meta.get('files', []):
        name = f['name']
        if not name.lower().endswith(AUDIO) or f.get('source') == 'derivative':
            continue
        key = ia_file_key(name) or ia_file_key(identifier)
        rows.append({'key': key, 'ia_identifier': identifier, 'ia_file': name, 'ia_title': title,
                     'ia_collection': sub, 'ia_size': int(f.get('size') or 0), 'ia_md5': f.get('md5', ''),
                     'ia_url': f'https://archive.org/download/{identifier}/{name}'})
    return rows

