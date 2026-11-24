"""Flight plan step 8: hosted page images (§4).

``A15/flightplan/{full,medium,thumbnail}/<page_id>.png`` from each page's default
witness (the AFJ scan, or the ALSJ scan where the AFJ has no page), and
``A15/flightplan/alsj/{full,medium,thumbnail}/<page_id>.png`` for every ALSJ scan.
``full`` is the native scan as lossless PNG (line art); ``medium`` is 640 px and
``thumbnail`` 120 px on the long side. An output whose recorded source checksum
is unchanged is not rewritten.
"""
from __future__ import annotations

import hashlib
import io
from pathlib import Path

from PIL import Image

from md_common import MEDIA_ROOT, RAW_ROOT, read_tsv, sha256_file, write_tsv
from md_flightplan import FLIGHT_PLAN, FP_RAW, MEDIA_PREFIX, page_sort_key

MEDIA_FIELDS = ('page_id', 'witness', 'size', 'media_path', 'source', 'source_sha256', 'width', 'height', 'sha256')
SIZES = (('full', None), ('medium', 640), ('thumbnail', 120))
CREDIT = {'afj_gif': 'Apollo Flight Journal scan (W. David Woods, Frank O\'Brien), hand-written additions by David Woods',
          'alsj_jpg': 'Apollo Lunar Surface Journal scan by Frank O\'Brien'}


def render(source: Path, long_side: int | None) -> tuple[bytes, int, int]:
    image = Image.open(source)
    image.load()
    image = image.convert('L')
    if long_side and max(image.size) > long_side:
        scale = long_side / max(image.size)
        image = image.resize((max(1, round(image.width * scale)), max(1, round(image.height * scale))),
                             Image.Resampling.LANCZOS)
    out = io.BytesIO()
    image.save(out, format='PNG', optimize=True)
    return out.getvalue(), image.width, image.height


def jobs(pages: list[dict]) -> list[tuple[str, str, str, Path]]:
    """(page_id, witness key, folder prefix, source path) for every hosted image."""
    out = []
    for p in pages:
        if p['status'] in ('present', 'blank'):
            out.append((p['page_id'], 'default', MEDIA_PREFIX, RAW_ROOT / f'{FP_RAW}/afj_gif/{p["page_id"]}.gif'))
        if p.get('alsj_jpg_url'):
            jpg = RAW_ROOT / f'{FP_RAW}/alsj_jpg/{Path(p["alsj_jpg_url"]).name}'
            if p['status'] == 'alsj_only':
                out.append((p['page_id'], 'default', MEDIA_PREFIX, jpg))
            out.append((p['page_id'], 'alsj', f'{MEDIA_PREFIX}/alsj', jpg))
    return out


def main(args) -> int:
    pages = sorted(read_tsv(FLIGHT_PLAN / 'fp_pages.tsv'), key=lambda p: page_sort_key(p['page_id']))
    previous = {(r['page_id'], r['witness'], r['size']): r for r in read_tsv(FLIGHT_PLAN / 'fp_media.tsv')}
    root = MEDIA_ROOT.parent  # MEDIA_ROOT is .../A15; media paths start with A15/
    rows, written = [], 0
    todo = jobs(pages)
    if args.limit:
        todo = todo[:args.limit]
        planned = {(t[0], t[1]) for t in todo}
        rows = [r for k, r in previous.items() if (k[0], k[1]) not in planned]
    for page_id, witness, prefix, source in todo:
        if not source.is_file():
            print(f'  missing source {source}')
            continue
        digest = sha256_file(source)
        for size, long_side in SIZES:
            media_path = f'{prefix}/{size}/{page_id}.png'
            target = root / media_path
            old = previous.get((page_id, witness, size))
            if old and old['source_sha256'] == digest and target.is_file():
                rows.append(old)
                continue
            if args.dry_run:
                print(f'  would write {media_path}')
                continue
            data, w, h = render(source, long_side)
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_name(target.name + '.tmp')
            tmp.write_bytes(data)
            tmp.replace(target)
            written += 1
            rows.append({'page_id': page_id, 'witness': witness, 'size': size, 'media_path': media_path,
                         'source': source.relative_to(RAW_ROOT).as_posix(), 'source_sha256': digest, 'width': w,
                         'height': h, 'sha256': hashlib.sha256(data).hexdigest()})
    if not args.dry_run:
        write_tsv(FLIGHT_PLAN / 'fp_media.tsv', MEDIA_FIELDS,
                  sorted(rows, key=lambda r: (page_sort_key(r['page_id']), r['witness'], r['size'])))
        write_tsv(FLIGHT_PLAN / 'fp_credits.tsv', ('witness', 'credit'), [{'witness': k, 'credit': v}
                                                                           for k, v in sorted(CREDIT.items())])
        import md_fp_assemble
        md_fp_assemble.assemble()
    print(f'media: {written} files written, {len(rows)} recorded under {root / MEDIA_PREFIX}')
    return 0
