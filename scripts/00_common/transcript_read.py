"""Read published timing transcripts (scripts/00_common/transcribe_run.py).

A chunk's ``segments.jsonl.gz`` holds its final rows. Once aligned (``chunk.json`` has an
``alignment`` record from the current aligner), word ``sf``/``ef`` are the CTC-aligned times
and ``wsf``/``wef`` keep Whisper's. Chunks published before alignment carry Whisper's times
only; ``identity_state`` reports how many chunks of a source are aligned, and its checksum
changes when a chunk is aligned, so artifacts bound to it become stale rather than mixed.
"""
from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path

from mission_align import is_aligned


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def chunk_rows(folder):
    with gzip.open(Path(folder) / 'segments.jsonl.gz', 'rt', encoding='utf-8') as stream:
        return [json.loads(line) for line in stream if line.strip()]


def identity_chunks(identity_dir):
    """[(folder, receipt)] of every chunk of one source identity; an incomplete chunk is an error."""
    out = []
    for folder in sorted((Path(identity_dir) / 'chunks').glob('c[0-9]*')):
        path = folder / 'chunk.json'
        receipt = read_json(path) if path.is_file() else {}
        if receipt.get('status') != 'complete':
            raise RuntimeError(f'Incomplete transcript chunk {folder}')
        out.append((folder, receipt))
    return out


def identity_state(identity_dir):
    """Chunk count, aligned-chunk count and a checksum over the chunks' row checksums."""
    chunks = identity_chunks(identity_dir)
    entries = [[r['index'], r['segments_sha256']] for _, r in chunks]
    return dict(chunks=len(chunks), aligned_chunks=sum(is_aligned(r) for _, r in chunks),
                sha256=hashlib.sha256(json.dumps(entries).encode()).hexdigest())


def identity_rows(identity_dir):
    """Rows of every chunk of one source identity, in source order."""
    rows = []
    for folder, _ in identity_chunks(identity_dir):
        rows.extend(chunk_rows(folder))
    rows.sort(key=lambda r: r['sf'] if r.get('sf') is not None else -1)
    return rows
