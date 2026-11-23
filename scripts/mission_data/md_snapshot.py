"""Step 1: snapshot the journals locally (.local/mission_data/journals) and the raw documents into the raw folder.

Journal chapter lists are derived from the index pages' links. A page whose
bytes are unchanged keeps its earlier manifest row, so reruns are byte-identical.
Raw documents never overwrite an earlier, different download: the old file is
kept under a dated name and both stay in ``raw_manifest.tsv``.
"""
from __future__ import annotations

import re
import shutil
from pathlib import Path

from bs4 import BeautifulSoup

from md_common import (AFJ_BASE, ALSJ_BASE, JOURNALS, RAW_MANIFEST, RAW_ROOT, Fetcher, absolute,
                       read_tsv, sha256_bytes, sha256_file, today, write_if_changed, write_tsv)

JOURNAL_FIELDS = ('file', 'url', 'final_url', 'fetch_date', 'bytes', 'sha256', 'role', 'order')
RAW_FIELDS = ('path', 'url', 'final_url', 'fetch_date', 'bytes', 'sha256', 'count', 'kind', 'note')

AFJ_SUPPORT = ('index.html', 'a15images.html', 'a15docs.html', 'a15videoindex.html', 'a15summary.html',
               'a15explan.html')
ALSJ_SUPPORT = ('a15.html', 'a15trans.html', 'images15.html', 'video15.html', 'a15tecdbrf.html',
                'a15.summary.html', 'a15.crew.html')
RAW_DOCUMENTS = (
    ('original_docs/AS15_TEC.PDF', ALSJ_BASE + 'AS15_TEC.PDF', 'transcript'),
    ('original_docs/AS15_PAO.PDF', ALSJ_BASE + 'AS15_PAO.PDF', 'transcript'),
    ('original_docs/AS15_CM.PDF', ALSJ_BASE + 'AS15_CM.PDF', 'transcript'),
    ('original_docs/AS15_LM.PDF', ALSJ_BASE + 'AS15_LM.PDF', 'transcript'),
    ('original_docs/a15.photidx.pdf', ALSJ_BASE + 'a15.photidx.pdf', 'photo_index'),
    ('original_docs/ap15_sim_index.pdf', 'https://apollo.im-ldi.com/SUPPORT_DATA/ap15_sim_index.pdf', 'photo_index'),
    ('original_docs/Apollo15VoiceTranscript-Geology.pdf', ALSJ_BASE + 'Apollo15VoiceTranscript-Geology.pdf',
     'transcript'),
)


def afj_chapters(index_html: bytes) -> list[str]:
    """Chapter pages in index order: numbered ``NN...html`` links."""
    soup = BeautifulSoup(index_html, 'html.parser')
    seen = []
    for a in soup.find_all('a', href=True):
        href = a['href'].strip()
        if re.fullmatch(r'\d\d[a-z]?-?[\w.-]*\.html', href) and href not in seen:
            seen.append(href)
    return seen


def alsj_chapters(index_html: bytes) -> list[str]:
    """Journal chapters between 'The Journal' and 'Background Material', in page order."""
    text = index_html.decode('utf-8', 'replace')
    start = text.find('The Journal')
    end = text.find('Background Material')
    if start < 0 or end < start:
        raise ValueError('ALSJ index layout changed: journal section not found')
    soup = BeautifulSoup(text[start:end], 'html.parser')
    seen = []
    for a in soup.find_all('a', href=True):
        href = a['href'].strip()
        if re.fullmatch(r'a15\.[\w.-]+\.html', href) and href not in seen:
            seen.append(href)
    return seen


def _snapshot(fetcher, folder: Path, base: str, pages: list[tuple[str, str]], refresh: bool) -> dict:
    manifest = {row['file']: row for row in read_tsv(folder / 'manifest.tsv')}
    stats = {'fetched': 0, 'changed': 0, 'kept': 0}
    rows = []
    for order, (name, role) in enumerate(pages):
        target = folder / name
        previous = manifest.get(name)
        if previous and target.is_file() and not refresh:
            rows.append({**previous, 'role': role, 'order': order})
            stats['kept'] += 1
            continue
        data, final = fetcher.get(base + name)
        stats['fetched'] += 1
        digest = sha256_bytes(data)
        if previous and previous['sha256'] == digest and target.is_file():
            rows.append({**previous, 'role': role, 'order': order})
            continue
        write_if_changed(target, data)
        stats['changed'] += 1
        rows.append({'file': name, 'url': base + name, 'final_url': final, 'fetch_date': today(),
                     'bytes': len(data), 'sha256': digest, 'role': role, 'order': order})
        print(f'  {name}: {len(data):,} bytes')
    write_tsv(folder / 'manifest.tsv', JOURNAL_FIELDS, rows)
    return stats


def snapshot_journals(fetcher: Fetcher | None = None, refresh: bool = False) -> dict:
    fetcher = fetcher or Fetcher()
    result = {}
    for key, folder, base, support, chapters in (
            ('afj', JOURNALS / 'ap15fj', AFJ_BASE, AFJ_SUPPORT, afj_chapters),
            ('alsj', JOURNALS / 'alsj15', ALSJ_BASE, ALSJ_SUPPORT, alsj_chapters)):
        index_name = support[0]
        index_path = folder / index_name
        if refresh or not index_path.is_file():
            data, _ = fetcher.get(base + index_name)
            write_if_changed(index_path, data)
        names = chapters(index_path.read_bytes())
        print(f'{key}: {len(names)} chapters')
        pages = [(n, 'support') for n in support] + [(n, 'chapter') for n in names]
        result[key] = {'chapters': len(names), **_snapshot(fetcher, folder, base, pages, refresh)}
    return result


def journal_chapters(source: str) -> list[Path]:
    folder = JOURNALS / ('ap15fj' if source == 'afj' else 'alsj15')
    rows = sorted(read_tsv(folder / 'manifest.tsv'), key=lambda r: int(r['order']))
    return [folder / r['file'] for r in rows if r['role'] == 'chapter']


def pdf_pages(path: Path) -> str:
    try:
        import pymupdf
        with pymupdf.open(path) as doc:
            return str(len(doc))
    except Exception:
        return ''


def record_raw(rows: dict, relative: str, url: str, final: str, kind: str, note: str = '',
               count: str = '', fetch_date: str | None = None) -> dict:
    path = RAW_ROOT / relative
    previous = rows.get(relative)
    size = path.stat().st_size
    if previous and previous.get('bytes') == str(size) and previous.get('sha256'):
        digest = previous['sha256']
        date = previous['fetch_date']
    else:
        digest = sha256_file(path)
        date = fetch_date or today()
    rows[relative] = {'path': relative, 'url': url, 'final_url': final, 'fetch_date': date,
                      'bytes': size, 'sha256': digest, 'count': count, 'kind': kind, 'note': note}
    return rows[relative]


def load_raw_manifest() -> dict:
    return {row['path']: row for row in read_tsv(RAW_MANIFEST)}


def save_raw_manifest(rows: dict) -> None:
    write_tsv(RAW_MANIFEST, RAW_FIELDS, [rows[k] for k in sorted(rows)])


def snapshot_raw_documents(fetcher: Fetcher | None = None, refresh: bool = False,
                           seed: dict[str, Path] | None = None) -> dict:
    """Download transcript PDFs and photo indexes. ``seed`` maps a relative path to a local copy."""
    fetcher = fetcher or Fetcher()
    rows = load_raw_manifest()
    for relative, url, kind in RAW_DOCUMENTS:
        target = RAW_ROOT / relative
        if seed and relative in seed and not target.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(seed[relative], target)
            print(f'  seeded {relative} from {seed[relative]}')
        if target.is_file() and not refresh:
            record_raw(rows, relative, url, rows.get(relative, {}).get('final_url', url), kind,
                       count=pdf_pages(target))
            continue
        temporary = target.with_name(target.name + '.new')
        digest, size, final = fetcher.download(url, temporary)
        if target.is_file() and sha256_file(target) != digest:
            old = rows.get(relative, {})
            dated = target.with_name(f'{target.stem}_{old.get("fetch_date", "previous")}{target.suffix}')
            target.rename(dated)
            moved = str(Path(relative).with_name(dated.name)).replace('\\', '/')
            if old:
                rows[moved] = {**old, 'path': moved, 'note': 'superseded upstream'}
            rows.pop(relative, None)
        temporary.replace(target)
        record_raw(rows, relative, url, final, kind, count=pdf_pages(target))
        print(f'  {relative}: {size:,} bytes')
    save_raw_manifest(rows)
    return rows


def verify_raw(relative: str) -> Path:
    """Return the raw file after checking it against the manifest. Missing or changed stops the build."""
    rows = load_raw_manifest()
    if relative not in rows:
        raise RuntimeError(f'{relative} is not in {RAW_MANIFEST.name}; run `mission_data.py snapshot raw` first')
    path = RAW_ROOT / relative
    if not path.is_file():
        raise RuntimeError(f'Raw file missing: {path}')
    row = rows[relative]
    if str(path.stat().st_size) != row['bytes'] or sha256_file(path) != row['sha256']:
        raise RuntimeError(f'Raw file changed since it was recorded: {path}')
    return path
