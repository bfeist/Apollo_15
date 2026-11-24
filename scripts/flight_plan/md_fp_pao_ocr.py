"""OXR page OCR of the PAO edition, run inside ``flight_plan.py ocr`` (one GPU OCR step).

The readings are optional input to ``sg_transcript.py sg pao`` (md_pao), where they are the
third OCR reading beside the PDF text layer and the Internet Archive OCR. Run the transcript
pipeline again after this step to use them.

Each page is rendered from the raw PDF with PyMuPDF (grey, 180 DPI) and posted to the local
OXR service at ``/v1/parse/sync``. The parsed page is kept as ``pNNNN.json`` with its
Markdown and a provenance record under ``md_pao.OXR_DIR``. A page is skipped only when its
provenance (PDF checksum, DPI, engine revisions, client version) and outputs match, so an
interrupted run resumes. Run discipline is the ingestion guide's: transient errors retry three
times, the run stops when the service is unreachable or after three consecutive failed pages,
and ``run-summary.json`` records the counts.
"""
from __future__ import annotations

import json
import time

from md_common import RAW_MANIFEST, RAW_ROOT, read_tsv, write_json
from md_fp_ocr import MODEL_REVISION, OXR_REVISION, oxr_parse, service_up
from md_pao import OXR_DIR, OXR_DPI

PDF = 'original_docs/AS15_PAO.PDF'
CLIENT_VERSION = 1


def parse_pages(spec: str, count: int) -> list[int]:
    pages = set()
    for part in spec.split(','):
        lo, _, hi = part.strip().partition('-')
        pages.update(range(int(lo), int(hi or lo) + 1))
    return sorted(p for p in pages if 1 <= p <= count)


def run(url: str, pages_spec: str | None = None) -> list[str]:
    """OCR the selected PAO pages (default: all). Returns the pages still incomplete."""
    import pymupdf
    manifest = {r['path']: r for r in read_tsv(RAW_MANIFEST)}
    if PDF not in manifest:
        raise SystemExit(f'{PDF} is not in raw_manifest.tsv; run `sg_transcript.py snapshot raw`')
    if not service_up(url):
        raise SystemExit(f'OXR is not answering at {url}; see `flight_plan.py ocr --help`')
    OXR_DIR.mkdir(parents=True, exist_ok=True)
    provenance = {'source': PDF, 'source_sha256': manifest[PDF]['sha256'], 'dpi': OXR_DPI,
                  'render': 'pymupdf grey png', 'oxr_revision': OXR_REVISION, 'model_revision': MODEL_REVISION,
                  'client_version': CLIENT_VERSION}
    done = failed = skipped = consecutive = 0
    failures, started = [], time.time()
    with pymupdf.open(RAW_ROOT / PDF) as doc:
        pages = parse_pages(pages_spec, len(doc)) if pages_spec else list(range(1, len(doc) + 1))
        for n in pages:
            stem = OXR_DIR / f'p{n:04d}'
            meta = stem.with_suffix('.provenance.json')
            if meta.is_file() and stem.with_suffix('.json').is_file() and \
                    json.loads(meta.read_text(encoding='utf-8')) == {**provenance, 'page': n}:
                skipped += 1
                continue
            try:
                png = doc[n - 1].get_pixmap(dpi=OXR_DPI, colorspace=pymupdf.csGRAY).tobytes('png')
                t = time.time()
                outputs = oxr_parse(png, url)['data']['result']['outputs']
                pages_out = (outputs.get('json') or {}).get('pages')
                if not isinstance(outputs.get('markdown'), str) or not isinstance(pages_out, list) or len(pages_out) != 1:
                    raise ValueError('expected Markdown and exactly one parsed page')
                stem.with_suffix('.md').write_text(outputs['markdown'], encoding='utf-8')
                write_json(stem.with_suffix('.json'), outputs['json'])
                write_json(meta, {**provenance, 'page': n})
                done += 1
                consecutive = 0
                left = len(pages) - done - skipped - failed
                print(f'[pao {done + skipped}/{len(pages)}] p{n:04d}: {time.time() - t:.1f} s '
                      f'(ETA {(time.time() - started) / done * left / 3600:.1f} h)', flush=True)
            except Exception as exc:  # recorded; the run continues unless failures repeat
                failed += 1
                consecutive += 1
                failures.append({'page': n, 'error': str(exc)[:300]})
                print(f'[pao] p{n:04d}: ERROR {exc}', flush=True)
                if consecutive >= 3 or not service_up(url):
                    print('[pao] stopping: three consecutive failures or the service is down', flush=True)
                    break
    incomplete = [f'pao:{n}' for n in pages if not (OXR_DIR / f'p{n:04d}.provenance.json').is_file()]
    write_json(OXR_DIR / 'run-summary.json', {**provenance, 'pages_selected': len(pages),
                                              'complete': len(pages) - len(incomplete), 'done_this_run': done,
                                              'skipped': skipped, 'failed': failed, 'failures': failures})
    print(f'pao ocr: {len(pages) - len(incomplete)}/{len(pages)} pages complete ({done} new, {skipped} reused, '
          f'{failed} failed) in {OXR_DIR}')
    return incomplete
