"""National Archives catalog: NASA (Record Group 255) sound recordings by mission.

With ``NARA_API_KEY`` set (environment or .env) the documented Catalog API v2
is used (https://www.archives.gov/research/catalog/help/api). Without a key the
same search runs through the public catalog site's own endpoint. Both take the
same parameters; phrases must be quoted (``"Apollo 15"``), and results are
sorted by naId so pages are stable. Responses are cached under
``.local/mission_data/audio_inventory/nara``.
"""
from __future__ import annotations

import os

from inventory_common import CachedJSON, nara_key, url_with
from md_common import load_env

API = 'https://catalog.archives.gov/api/v2/records/search'
SITE = 'https://catalog.archives.gov/proxy/records/search'
RG255 = 582  # naId of Record Group 255, Records of the National Aeronautics and Space Administration
PAGE = 100   # the site endpoint refuses some smaller page sizes
FIELDS = ('key', 'nara_naid', 'nara_local_id', 'nara_title', 'nara_series', 'nara_date', 'nara_scope',
          'nara_objects', 'nara_bytes', 'nara_media', 'nara_queries', 'nara_url')


def client(refresh=False) -> tuple[CachedJSON, str]:
    key = os.environ.get('NARA_API_KEY') or load_env().get('NARA_API_KEY', '')
    if key:
        return CachedJSON('nara', delay=0.5, refresh=refresh, headers={'x-api-key': key}), API
    return CachedJSON('nara', delay=1.0, refresh=refresh), SITE


def search(phrase: str, http: CachedJSON, base: str, ancestor: int = RG255) -> list[dict]:
    """All sound-recording descriptions under ``ancestor`` matching ``phrase``."""
    out, page = [], 1
    while True:
        params = {'q': phrase, 'ancestorNaId': ancestor, 'typeOfMaterials': 'Sound Recordings',
                  'limit': PAGE, 'page': page, 'sort': 'naId:asc'}
        body = http.get(url_with(base, params))
        hits = body['body']['hits'] if 'body' in body else body['hits']
        batch = [h['_source']['record'] for h in hits['hits']]
        out.extend(batch)
        total = hits['total']['value'] if isinstance(hits['total'], dict) else hits['total']
        if not batch or page * PAGE >= total:
            return out
        page += 1


def row(record: dict) -> dict:
    objects = record.get('digitalObjects') or []
    series = next((a for a in record.get('ancestors', []) if a.get('distance') == 1), {})
    dates = record.get('productionDates') or [{}]
    media = sorted({m.get('dimension') or m.get('specificMediaType', '')
                    for p in record.get('physicalOccurrences', []) for m in p.get('mediaOccurrences', [])} - {''})
    return {'key': nara_key(record.get('localIdentifier', '')), 'nara_naid': record['naId'],
            'nara_local_id': record.get('localIdentifier', ''), 'nara_title': record.get('title', ''),
            'nara_series': series.get('title', ''), 'nara_date': dates[0].get('logicalDate', ''),
            'nara_scope': (record.get('scopeAndContentNote') or '').strip(),
            'nara_objects': len(objects), 'nara_bytes': sum(o.get('objectFileSize') or 0 for o in objects),
            'nara_media': '; '.join(media), 'nara_url': f'https://catalog.archives.gov/id/{record["naId"]}'}


def objects(record: dict) -> list[dict]:
    return [{'name': o['objectFilename'], 'url': o['objectUrl'], 'size': o.get('objectFileSize') or 0,
             'designator': o.get('objectDesignator', ''), 'type': o.get('objectType', '')}
            for o in record.get('digitalObjects') or []]
