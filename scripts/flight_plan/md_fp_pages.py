"""Flight plan step 2: witness matching, LEP and index -> fp_pages.tsv (§2 of the flight-plan guide).

* AFJ GIF and AFJ PDF image: decoded without resampling and compared by pixel
  hash, with a perceptual hash (any quarter turn) as the fallback.
* Virtual AGC PDF: the same scan as the AFJ PDF, matched the same way. Its OCR
  text layer gives a weak footer reading and the first LEP reading.
* ALSJ JPG: a different scan, paired by the excerpt's page label and confirmed
  by ORB features and a RANSAC homography onto the AFJ GIF.

Pages without a footer reading, with two candidates or with an unmatched image
are written to the review queue rather than guessed.
"""
from __future__ import annotations

import hashlib
import io
import json
import re
from pathlib import Path

import numpy as np
from PIL import Image

from md_common import JOURNALS, RAW_ROOT, WORK, read_tsv, write_tsv
from md_flightplan import (AFJ_PDF, FLIGHT_PLAN, FP_RAW, ROMAN, SPECIAL, VAGC_PDF, normalise_page_id,
                           page_sort_key, parse_afj_index, parse_alsj_excerpt, unlinked_chapters)
from md_snapshot import load_raw_manifest

WITNESS_FIELDS = ('witness', 'key', 'page_id', 'width', 'height', 'pixel_sha256', 'phash', 'match', 'match_to',
                  'distance', 'rotation', 'note')
LEP_FIELDS = ('page_id', 'lep_date', 'lep_page', 'reading')
PHASH_TOLERANCE = 10


# ------------------------------------------------------------------- images
def load_gray(data_or_path) -> np.ndarray:
    with Image.open(io.BytesIO(data_or_path) if isinstance(data_or_path, bytes) else data_or_path) as opened:
        opened.load()
        image = opened.copy()
    if image.mode in ('1', 'P', 'RGB', 'RGBA', 'LA', 'I;16', 'CMYK'):
        image = image.convert('L')
    return np.asarray(image, dtype=np.uint8)


def pixel_hash(gray: np.ndarray) -> str:
    h = hashlib.sha256(f'{gray.shape[0]}x{gray.shape[1]}'.encode())
    h.update(np.ascontiguousarray(gray).tobytes())
    return h.hexdigest()


def phash(gray: np.ndarray) -> int:
    from scipy.fft import dctn
    small = np.asarray(Image.fromarray(gray).resize((32, 32), Image.Resampling.LANCZOS), dtype=np.float64)
    coeffs = dctn(small, norm='ortho')[:8, :8].flatten()[1:]
    bits = coeffs > np.median(coeffs)
    return int(''.join('1' if b else '0' for b in bits), 2)


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count('1')


def rotations(gray: np.ndarray) -> dict:
    """The eight orientations: quarter turns, and quarter turns of the vertically flipped image.

    PDF image XObjects are often stored bottom-up and flipped by the placement matrix, so the
    stored pixels equal the GIF only after a flip. Key 0 is the image as stored.
    """
    out = {k * 90: np.rot90(gray, -k) for k in range(4)}
    flipped = gray[::-1]
    out.update({f'flip{k * 90}': np.rot90(flipped, -k) for k in range(4)})
    return out


def pdf_images(path: Path) -> list[tuple[int, np.ndarray | None, str]]:
    """(1-based page, decoded largest embedded image, note) without resampling."""
    import pymupdf
    out = []
    with pymupdf.open(path) as doc:
        for index, page in enumerate(doc):
            images = page.get_images(full=True)
            if not images:
                out.append((index + 1, None, 'no image'))
                continue
            best = max(images, key=lambda im: im[2] * im[3])
            info = doc.extract_image(best[0])
            try:
                gray = load_gray(info['image'])
            except Exception as exc:  # e.g. JBIG2 that PIL cannot read: let PyMuPDF decode at native size
                pix = pymupdf.Pixmap(doc, best[0])
                if pix.n != 1:
                    pix = pymupdf.Pixmap(pymupdf.csGRAY, pix)
                gray = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width).copy()
                info['ext'] = f'{info.get("ext")} via pixmap ({exc.__class__.__name__})'
            if page.rotation:
                gray = np.rot90(gray, -page.rotation // 90)
            out.append((index + 1, gray, f'{info.get("ext")}; images={len(images)}; rotation={page.rotation}'))
    return out


def page_words_px(page, size: tuple[int, int] | None = None) -> tuple[list[tuple], tuple[float, float]]:
    """Text-layer words in the pixel frame of the page image as displayed (= the AFJ GIF).

    Words come in unrotated page points. They are rotated with the page, then placed relative to the
    rotated image rectangle and scaled to ``size`` (default: the image's displayed pixel size).
    Returns ([(x0, y0, x1, y1, word)], (width, height)).
    """
    import pymupdf
    images = page.get_images(full=True)
    matrix = page.rotation_matrix
    if images:
        best = max(images, key=lambda im: im[2] * im[3])
        rects = page.get_image_rects(best[0])
        box = (rects[0] * matrix) if rects else page.rect
        native = (best[2], best[3]) if page.rotation in (0, 180) else (best[3], best[2])
    else:
        box, native = page.rect, (page.rect.width, page.rect.height)
    width, height = size or native
    sx, sy = width / box.width, height / box.height
    out = []
    for x0, y0, x1, y1, word, *_ in page.get_text('words'):
        r = pymupdf.Rect(x0, y0, x1, y1) * matrix
        out.append(((r.x0 - box.x0) * sx, (r.y0 - box.y0) * sy, (r.x1 - box.x0) * sx, (r.y1 - box.y0) * sy, word))
    return out, (width, height)


def pdf_words(path: Path) -> dict[int, dict]:
    """Text-layer words per 1-based page, in displayed image pixels."""
    import pymupdf
    out = {}
    with pymupdf.open(path) as doc:
        for index, page in enumerate(doc):
            words, size = page_words_px(page)
            out[index + 1] = {'size': size, 'words': words}
    return out


# ---------------------------------------------------------- footer reading
FOOTER_RE = re.compile(r'^([1-6])\s*[-–]\s*(\d{1,3})$')


def footer_page(words: list[tuple], height: float, width: float) -> str | None:
    """Weak footer page reading from a text layer: the rightmost page-shaped token in the bottom strip."""
    candidates = []
    for x0, y0, x1, y1, w in words:
        token = w.strip().replace('I', '1').replace('l', '1').replace('O', '0').replace('o', '0')
        if y0 < 0.80 * height and x0 < 0.85 * width:
            continue
        m = FOOTER_RE.match(token) or (re.fullmatch(r'([1-6])-(\d{3})', token))
        if m:
            candidates.append((x1, y1, f'{m.group(1)}-{int(m.group(2)):03d}'))
        elif token.lower() in ROMAN and y0 < 0.12 * height:
            candidates.append((x1, -y1, token.lower()))
    if not candidates:
        return None
    return max(candidates)[2]


# ---------------------------------------------------------------------- LEP
LEP_ENTRY = re.compile(r'([ivx]{1,5}|[1-6]-\d{1,3})\s*[.\s·,]*\s*(\d{1,2}/\d{1,2}/7\d)')


def parse_lep(text: str) -> list[tuple[str, str]]:
    """``3-126 . . . 6/21/71`` pairs from a text reading of an LEP page."""
    text = text.replace('l', '1').replace('I', '1').replace('O', '0')
    text = re.sub(r'(?<=\d)\s*-\s*(?=\d)', '-', text)
    out = []
    for m in LEP_ENTRY.finditer(text.lower()):
        page = normalise_page_id(m.group(1))
        if page:
            out.append((page, m.group(2)))
    return out


def expected_lep_pages() -> list[str]:
    """Page IDs the LEP must list, from its structure (front matter, chapters 1–3, and the 5-series excerpt)."""
    pages = list(ROMAN) + [f'1-{n:03d}' for n in range(1, 10)] + [f'2-{n:03d}' for n in range(1, 24)]
    pages += [f'3-{n:03d}' for n in range(1, 409)]
    # LEP page iv/v: Chapter 4 is one page, Chapter 5 has 25 and Chapter 6 one (checked on the AFJ scan).
    pages += ['4-001'] + [f'5-{n:03d}' for n in range(1, 26)] + ['6-001']
    return pages


# ------------------------------------------------------------------ matching
def match_pdf(witness: str, images, gif_rows: dict, gif_px: dict, gif_ph: dict) -> list[dict]:
    """Match each PDF page image to an AFJ GIF: exact pixel hash, else unique nearest phash under a quarter turn."""
    by_hash = {}
    for page_id, h in gif_px.items():
        by_hash.setdefault(h, []).append(page_id)
    out = []
    for number, gray, note in images:
        row = {'witness': witness, 'key': str(number), 'match': '', 'match_to': '', 'distance': '',
               'rotation': '', 'note': note}
        if gray is None:
            row['match'] = 'no_image'
            out.append(row)
            continue
        row.update(width=gray.shape[1], height=gray.shape[0])
        found = None
        for rot, g in rotations(gray).items():
            h = pixel_hash(g)
            if rot == 0:
                row['pixel_sha256'] = h
            if h in by_hash:
                found = (by_hash[h], 0, rot, 'pixel')
                break
        if found is None:
            best = []
            for rot, g in rotations(gray).items():
                p = phash(g)
                if rot == 0:
                    row['phash'] = f'{p:016x}'
                for page_id, q in gif_ph.items():
                    best.append((hamming(p, q), str(rot), page_id))
            best.sort(key=lambda b: (b[0], page_sort_key(b[2]), b[1]))
            d, rot, page_id = best[0]
            ties = [b for b in best if b[0] <= d + 2 and b[2] != page_id]
            if ties and d <= PHASH_TOLERANCE and gif_rows:
                # Near-identical pages (rest periods, blanks): decide by mean absolute pixel difference.
                orient = rotations(gray)
                scored = []
                for dist, r, pid in [best[0]] + ties:
                    key = int(r) if r.lstrip('-').isdigit() else r
                    g, ref = orient[key], gif_rows.get(pid)
                    if ref is not None and g.shape == ref.shape:
                        scored.append((float(np.abs(g.astype(np.int16) - ref).mean()), pid, r, dist))
                scored.sort()
                if scored and (len(scored) == 1 and scored[0][0] < 12 or
                               len(scored) >= 2 and scored[0][0] < 0.6 * scored[1][0]):
                    _, page_id, rot, d = scored[0]
                    ties = []
                    row['note'] += '; tie broken by pixel MAD ' + ' vs '.join(f'{x[1]}={x[0]:.1f}' for x in scored)
            if d <= PHASH_TOLERANCE and not ties:
                found = ([page_id], d, rot, 'phash')
            elif d <= PHASH_TOLERANCE:
                row['match'] = 'ambiguous'
                row['match_to'] = ';'.join(sorted({page_id} | {t[2] for t in ties}, key=page_sort_key))
                row['distance'] = d
        else:
            row.setdefault('phash', f'{phash(gray):016x}')
        if found:
            ids, d, rot, method = found
            row['match'] = method if len(ids) == 1 else 'ambiguous'
            row['match_to'] = ';'.join(ids)
            row['page_id'] = ids[0] if len(ids) == 1 else ''
            row['distance'], row['rotation'] = d, rot
        elif not row['match']:
            row['match'] = 'unmatched'
        row.setdefault('phash', f'{phash(gray):016x}')
        out.append(row)
    return out


def homography(alsj: np.ndarray, afj: np.ndarray) -> tuple[list[float] | None, int, float]:
    """ORB + RANSAC homography mapping ALSJ pixels onto AFJ GIF pixels. Returns (H row-major, inliers, ratio)."""
    import cv2
    orb = cv2.ORB_create(nfeatures=4000)
    ka, da = orb.detectAndCompute(alsj, None)
    kb, db = orb.detectAndCompute(afj, None)
    if da is None or db is None or len(ka) < 20 or len(kb) < 20:
        return None, 0, 0.0
    matcher = cv2.BFMatcher(cv2.NORM_HAMMING)
    pairs = matcher.knnMatch(da, db, k=2)
    good = [m for m, n in (p for p in pairs if len(p) == 2) if m.distance < 0.75 * n.distance]
    if len(good) < 12:
        return None, len(good), 0.0
    src = np.float32([ka[m.queryIdx].pt for m in good]).reshape(-1, 1, 2)
    dst = np.float32([kb[m.trainIdx].pt for m in good]).reshape(-1, 1, 2)
    H, mask = cv2.findHomography(src, dst, cv2.RANSAC, 4.0)
    if H is None:
        return None, 0, 0.0
    inliers = int(mask.sum())
    return [round(float(v), 8) for v in H.flatten()], inliers, inliers / len(good)


# ---------------------------------------------------------------------- main
def raw_path(manifest: dict, relative: str) -> Path | None:
    return RAW_ROOT / relative if relative in manifest and (RAW_ROOT / relative).is_file() else None


def main(args) -> int:
    manifest = load_raw_manifest()
    index_html = (JOURNALS / 'ap15fj' / 'fplanindex.html').read_bytes()
    index_rows = parse_afj_index(index_html)
    excerpt = parse_alsj_excerpt((JOURNALS / 'alsj15' / 'a15.fltplan.html').read_bytes())
    queue = []

    # AFJ GIFs: the reference witness, named by printed page number.
    gif_px, gif_ph, gif_gray, witnesses = {}, {}, {}, []
    for row in index_rows:
        rel = f'{FP_RAW}/afj_gif/{Path(row["gif_url"]).name}'
        path = raw_path(manifest, rel)
        if not path:
            queue.append({'key': row['page_id'], 'issue': 'afj_gif_missing', 'detail': rel})
            continue
        gray = load_gray(path)
        gif_gray[row['page_id']] = gray
        gif_px[row['page_id']] = pixel_hash(gray)
        gif_ph[row['page_id']] = phash(gray)
        witnesses.append({'witness': 'afj_gif', 'key': row['page_id'], 'page_id': row['page_id'],
                          'width': gray.shape[1], 'height': gray.shape[0], 'pixel_sha256': gif_px[row['page_id']],
                          'phash': f'{gif_ph[row["page_id"]]:016x}', 'match': 'self'})
    print(f'afj_gif: {len(gif_px)} decoded')
    dup = {}
    for page_id, h in gif_px.items():
        dup.setdefault(h, []).append(page_id)
    for h, ids in dup.items():
        if len(ids) > 1:
            queue.append({'key': ';'.join(ids), 'issue': 'identical_gifs', 'detail': h[:16]})

    pdf_rows = {}
    for witness, rel in (('afj_pdf', AFJ_PDF), ('vagc_pdf', VAGC_PDF)):
        path = raw_path(manifest, rel)
        if not path:
            print(f'{witness}: not downloaded; skipped')
            continue
        rows = match_pdf(witness, pdf_images(path), gif_gray, gif_px, gif_ph)
        counts = {}
        for r in rows:
            counts[r['match']] = counts.get(r['match'], 0) + 1
        print(f'{witness}: {len(rows)} pages {counts}')
        pdf_rows[witness] = rows
        witnesses.extend(rows)

    # Weak footer readings and the LEP from the Virtual AGC text layer (same scan as the AFJ PDF).
    vagc_words = pdf_words(RAW_ROOT / VAGC_PDF) if 'vagc_pdf' in pdf_rows else {}
    vagc_footer, vagc_text = {}, {}
    for r in pdf_rows.get('vagc_pdf', []):
        number = int(r['key'])
        info = vagc_words.get(number)
        if not info:
            continue
        w, h = info['size']
        reading = footer_page(info['words'], h, w)
        r['note'] = (r.get('note') or '') + f'; footer_text_layer={reading or "-"}'
        if r.get('page_id'):
            vagc_footer[r['page_id']] = reading or ''
            vagc_text[r['page_id']] = ' '.join(x[4] for x in sorted(info['words'], key=lambda t: (round(t[1] / 4), t[0])))
            if reading and reading != r['page_id'] and r['page_id'] not in ROMAN + SPECIAL:
                queue.append({'key': r['page_id'], 'issue': 'footer_text_layer_disagrees',
                              'detail': f'vagc_pdf p{number} reads {reading}'})

    # Unmatched PDF pages: identify by the text-layer footer when it names a page no GIF covers.
    for witness, rows in pdf_rows.items():
        for r in rows:
            if r['match'] in ('unmatched', 'ambiguous', 'no_image'):
                twin = None
                if witness == 'afj_pdf':
                    twin = next((v for v in pdf_rows.get('vagc_pdf', []) if v['key'] == r['key']), None)
                elif witness == 'vagc_pdf':
                    twin = r
                reading = ''
                if twin and 'footer_text_layer=' in (twin.get('note') or ''):
                    reading = twin['note'].split('footer_text_layer=')[-1].split(';')[0]
                if r['key'] == '1' and witness in ('afj_pdf', 'vagc_pdf'):
                    r['page_id'], r['match'] = 'pdf_note', 'cover_note'
                    continue
                queue.append({'key': f'{witness}:p{r["key"]}', 'issue': r['match'],
                              'detail': f'text-layer footer {reading or "-"}; candidates {r.get("match_to") or "-"}'})

    # ALSJ JPGs: paired by excerpt label, confirmed by homography onto the AFJ GIF.
    for e in excerpt:
        rel = f'{FP_RAW}/alsj_jpg/{e["jpg"]}'
        path = raw_path(manifest, rel)
        if not path:
            queue.append({'key': e['jpg'], 'issue': 'alsj_jpg_missing', 'detail': rel})
            continue
        gray = load_gray(path)
        row = {'witness': 'alsj_jpg', 'key': e['jpg'], 'page_id': e['page_id'], 'width': gray.shape[1],
               'height': gray.shape[0], 'pixel_sha256': pixel_hash(gray), 'phash': f'{phash(gray):016x}',
               'note': f'label {e["alsj_span"]}'}
        if e['page_id'] in gif_gray:
            H, inliers, ratio = homography(gray, gif_gray[e['page_id']])
            row['match'] = 'homography' if H and inliers >= 40 and ratio >= 0.25 else 'unconfirmed'
            row['match_to'], row['distance'] = e['page_id'], f'{inliers}/{ratio:.2f}'
            row['note'] += f'; H={json.dumps(H)}' if H else ''
            if row['match'] != 'homography':
                queue.append({'key': e['jpg'], 'issue': 'alsj_alignment_weak',
                              'detail': f'{e["page_id"]}: inliers {inliers}, ratio {ratio:.2f}'})
        else:
            row['match'] = 'label_only'
        witnesses.append(row)
    print(f'alsj_jpg: {sum(w["witness"] == "alsj_jpg" for w in witnesses)} rows')

    # LEP: readings from the Virtual AGC text layer of pages i–v (weak; checked against the page structure).
    lep_rows = {}
    for page_id in ('i', 'ii', 'iii', 'iv', 'v'):
        for listed, date in parse_lep(vagc_text.get(page_id, '')):
            lep_rows.setdefault(listed, {'page_id': listed, 'lep_date': date, 'lep_page': page_id,
                                         'reading': 'vagc_text'})
    for page_id in expected_lep_pages():
        lep_rows.setdefault(page_id, {'page_id': page_id, 'lep_date': '', 'lep_page': '', 'reading': 'structure'})
    write_tsv(FLIGHT_PLAN / 'fp_lep.tsv', LEP_FIELDS, sorted(lep_rows.values(), key=lambda r: page_sort_key(r['page_id'])))

    write_tsv(FLIGHT_PLAN / 'fp_witnesses.tsv', WITNESS_FIELDS,
              sorted(witnesses, key=lambda r: (r['witness'], page_sort_key(r['page_id']) if r.get('page_id') and
                                               r['page_id'] != 'pdf_note' else (9, 0), int(r['key']) if
                                               r['key'].isdigit() else 0, r['key'])))
    # Duplicate matches: two PDF pages claiming one GIF.
    for witness, rows in pdf_rows.items():
        seen = {}
        for r in rows:
            if r.get('page_id') and r['page_id'] != 'pdf_note':
                seen.setdefault(r['page_id'], []).append(r['key'])
        for page_id, keys in seen.items():
            if len(keys) > 1:
                queue.append({'key': f'{witness}:{page_id}', 'issue': 'two_pdf_pages_one_gif',
                              'detail': ';'.join(keys)})
    # Page-count reconciliation (§1 open check): what accounts for the PDF and index totals.
    summary = []
    gif_ids = set(gif_px)
    for witness, rows in pdf_rows.items():
        matched = {r['page_id'] for r in rows if r.get('page_id') and r['page_id'] != 'pdf_note'}
        summary.append({'item': f'{witness}_pages', 'detail': str(len(rows))})
        summary.extend({'item': f'{witness}_cover_note', 'detail': f'p{r["key"]}'} for r in rows
                       if r.get('page_id') == 'pdf_note')
        summary.extend({'item': f'{witness}_blank_page', 'detail': f'p{r["key"]} has no image'} for r in rows
                       if r['match'] == 'no_image' and r.get('page_id') != 'pdf_note')
        seen = {}
        for r in rows:
            if r.get('page_id') and r['page_id'] != 'pdf_note':
                seen.setdefault(r['page_id'], []).append(r['key'])
        summary.extend({'item': f'{witness}_duplicate', 'detail': f'{pid} at pages {";".join(keys)}'}
                       for pid, keys in sorted(seen.items(), key=lambda kv: page_sort_key(kv[0])) if len(keys) > 1)
        summary.extend({'item': f'{witness}_lacks', 'detail': pid}
                       for pid in sorted(gif_ids - matched, key=page_sort_key))
    listed = {r['page_id'] for r in lep_rows.values()}
    summary.append({'item': 'afj_index_links', 'detail': str(len(index_rows))})
    summary.extend({'item': 'lep_page_without_scan', 'detail': pid} for pid in sorted(
        listed - gif_ids - {e['page_id'] for e in excerpt}, key=page_sort_key))
    write_tsv(FLIGHT_PLAN / 'fp_reconciliation.tsv', ('item', 'detail'), summary)
    write_tsv(WORK / 'fp_pages_queue.tsv', ('key', 'issue', 'detail'), queue)
    print(f'review queue: {len(queue)} rows -> {WORK / "fp_pages_queue.tsv"}')

    import md_fp_assemble
    md_fp_assemble.assemble(index_rows=index_rows, excerpt=excerpt, unlinked=unlinked_chapters(index_html))
    return 0
