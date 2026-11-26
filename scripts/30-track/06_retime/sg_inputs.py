"""Inputs of the SG retiming stage (docs/20_2026-11-25_sg_retime_30track.md).

Every observation this stage uses is read from saved artifacts and bound to their checksums:

* the 06a timing transcripts of the 30-track 05b channels, placed on canonical tape seconds by
  ``canonical_origin_s + sf / rate`` after checking that the 06a identity, the 05b review and the
  deflutter plan agree on each channel's origin (``tape_channels``);
* the IA reel transcripts, placed on GET through ``catalog/sg_drift`` (``GET = t + D(t) + c_k``);
* the NARA flight-tape transcripts, placed on GET through their respeed record segments
  (``GET = u + get_equals_output_plus_s``).

Word times are the CTC-aligned ones (``scripts/00_common/mission_align.py``): ``transcribe_run``
aligns every chunk it publishes and re-aligns older chunks in place, keeping Whisper's times as
``wsf``/``wef``. ``transcript_state`` (one transcript) and ``store_state`` (one store) say whether
that is finished: an edition is final only when every transcript it read is aligned with the
current aligner and no transcription session is running on its store. Each artifact binds the
chunk checksums it read (``transcript_read.identity_state``), so a chunk aligned later makes it
stale instead of silently mixing Whisper and aligned times.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from audio_sources import PROCESSING_ROOT
from mission_store import Store
from mission_align import align_id
from transcript_read import identity_rows, identity_state

REPO = Path(__file__).resolve().parents[3]
TAPE_DATA = Path(os.environ.get('A15_TAPE_ROOT', 'D:/Apollo_15_30-Track' if os.name == 'nt' else '/mnt/d/Apollo_15_30-Track'))
DEFLUTTERED_ROOT = TAPE_DATA / 'defluttered_shared'
RETIMING_ROOT = TAPE_DATA / 'stage_evidence' / 'retiming'
INVENTORY = REPO / 'research' / 'readme_figures' / 'inventory.json'
DRIFT_MAPS = REPO / 'catalog' / 'sg_drift' / 'sg_drift_maps.json'
IA_AUDIO_ROOT = Path(os.environ.get('A15_SG_ROOT', 'F:/Apollo_15_data/raw/audio/Internet Archive'))
NARA_RESPEED_ROOT = Path(os.environ.get('A15_NARA_RESPEED_ROOT', PROCESSING_ROOT / 'sg_nara' / 'respeed'))


@dataclass(frozen=True)
class TranscriptStore:
    """One transcript store: its root, session evidence and settings."""
    name: str
    transcript_root: Path
    evidence_root: Path
    settings_id: str

    @property
    def sources(self) -> Path:
        return self.transcript_root / self.settings_id / 'sources'


STORES = {
    'tape': TranscriptStore('tape', TAPE_DATA / 'timing_transcripts', TAPE_DATA / 'stage_evidence' / 'timing_transcription',
                            'asr-c1198b48a6d3'),
    'ia': TranscriptStore('ia', PROCESSING_ROOT / 'sg' / 'timing_transcripts',
                          PROCESSING_ROOT / 'sg' / 'evidence' / 'timing_transcription', 'asr-c1198b48a6d3'),
    'nara': TranscriptStore('nara', PROCESSING_ROOT / 'sg_nara' / 'retimed_transcripts',
                            PROCESSING_ROOT / 'sg_nara' / 'retimed_transcripts' / 'evidence', 'asr-08ce474a85b7'),
}

# ------------------------------------------------------------------ helpers
def sha256_file(path, block=8 * 1024 * 1024) -> str:
    h = hashlib.sha256()
    with open(path, 'rb') as stream:
        while chunk := stream.read(block):
            h.update(chunk)
    return h.hexdigest()


def digest(value, length=None) -> str:
    text = json.dumps(value, sort_keys=True, separators=(',', ':'), default=str)
    out = hashlib.sha256(text.encode('utf-8')).hexdigest()
    return out[:length] if length else out


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.partial')
    tmp.write_text(json.dumps(value, indent=1, allow_nan=False, default=_json_default) + '\n', encoding='utf-8')
    tmp.replace(path)


def _json_default(value):
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return float(value)
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f'Not JSON serializable: {type(value).__name__}')


def write_tsv(path, fields, rows, compress=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ['\t'.join(fields)]
    for row in rows:
        lines.append('\t'.join(_cell(row.get(f)) for f in fields))
    data = ('\n'.join(lines) + '\n').encode('utf-8')
    tmp = path.with_name(path.name + '.partial')
    tmp.write_bytes(gzip.compress(data, mtime=0) if compress else data)
    tmp.replace(path)


def read_tsv(path):
    path = Path(path)
    data = path.read_bytes()
    if path.suffix == '.gz':
        data = gzip.decompress(data)
    lines = data.decode('utf-8').splitlines()
    fields = lines[0].split('\t')
    return [dict(zip(fields, line.split('\t'))) for line in lines[1:] if line]


def _cell(value):
    if value is None:
        return ''
    if isinstance(value, float):
        return f'{value:.6f}'.rstrip('0').rstrip('.') if np.isfinite(value) else ''
    if isinstance(value, (list, tuple)):
        return ','.join(_cell(v) for v in value)
    return str(value).replace('\t', ' ').replace('\n', ' ')


# ---------------------------------------------------------- transcript state
def store_state(store: TranscriptStore) -> dict:
    """Running transcription sessions of a store. While one runs, chunks are being published or
    replaced (an in-place alignment renames a chunk aside for a moment), so nothing read from the
    store is final."""
    sessions = Store(store.transcript_root, store.evidence_root).sessions()
    running = [s['session'] for s in sessions if s.get('status') == 'running']
    return dict(store=store.name, settings_id=store.settings_id, align_id=align_id(), running=running)


def transcript_state(identity_dir: Path) -> dict:
    """Chunk checksums of one transcript and whether every chunk has the current alignment."""
    state = identity_state(identity_dir)
    return dict(align_id=align_id(), chunks=state['chunks'], aligned_chunks=state['aligned_chunks'],
                aligned=state['chunks'] > 0 and state['aligned_chunks'] == state['chunks'], sha256=state['sha256'])


# ------------------------------------------------------------------- words
_TOKENIZER = None


def tokenizer():
    """``md_sg``'s word handling: ``md_common.tokens`` + ``md_reconcile.comparable`` and hard flags."""
    global _TOKENIZER
    if _TOKENIZER is None:
        from md_common import tokens
        from md_reconcile import comparable
        from md_sg import BAD_SEGMENT_FLAGS, HARD_FLAGS
        _TOKENIZER = (tokens, comparable, frozenset(HARD_FLAGS), frozenset(BAD_SEGMENT_FLAGS))
    return _TOKENIZER


@dataclass
class Words:
    """Comparable tokens of one transcript, in source seconds."""
    tok: np.ndarray        # str
    s: np.ndarray          # word start, source seconds
    p: np.ndarray          # word probability
    aligned: np.ndarray    # bool: CTC-aligned word time (False: Whisper's time, chunk not yet aligned)
    meta: dict = field(default_factory=dict)

    def __len__(self):
        return len(self.tok)


def words_from_rows(rows, sample_rate) -> Words:
    tokens, comparable, hard, bad_segment = tokenizer()
    tok, s, p, aligned = [], [], [], []
    for row in rows:
        if bad_segment & set(row.get('flags') or ()) or row.get('sf') is None:
            continue
        row_aligned = 'aligned' in (row.get('flags') or ())
        for w in row['words']:
            if w.get('sf') is None or hard & set(w.get('flags') or ()):
                continue
            for t in comparable(tokens(w['w'])):
                tok.append(t)
                s.append(w['sf'] / sample_rate)
                p.append(float(w.get('p', 0.0)))
                aligned.append(row_aligned and w.get('ap') is not None)
    order = np.argsort(np.asarray(s, float), kind='stable')
    return Words(np.asarray(tok, dtype=str)[order] if tok else np.zeros(0, '<U1'),
                 np.asarray(s, float)[order], np.asarray(p, float)[order], np.asarray(aligned, bool)[order])


def load_words(identity_dir: Path, cache_root: Path | None = None) -> Words:
    """Tokens of one transcript identity, cached by its chunk checksums."""
    identity_dir = Path(identity_dir)
    source = read_json(identity_dir / 'source.json')
    state = transcript_state(identity_dir)
    rate = source['identity']['sample_rate']
    cache = None
    if cache_root is not None:
        settings_id = next(p for p in reversed(identity_dir.parts) if p.startswith('asr-'))
        cache = Path(cache_root) / settings_id / f"{source['source']['identity_id']}-{state['sha256'][:20]}.npz"
        if cache.is_file():
            with np.load(cache) as z:
                return Words(z['tok'], z['s'], z['p'], z['aligned'], dict(digest=state, source=source))
    for _ in range(3):
        rows = identity_rows(identity_dir)
        after = transcript_state(identity_dir)
        if after == state:
            break
        state = after  # a chunk was aligned while reading: read again
    else:
        raise RuntimeError(f'{identity_dir}: transcript kept changing while being read')
    if cache is not None:
        cache = cache.with_name(f"{source['source']['identity_id']}-{state['sha256'][:20]}.npz")
    words = words_from_rows(rows, rate)
    words.meta = dict(digest=state, source=source)
    if cache is not None:
        cache.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache.with_name(cache.stem + '.partial.npz')
        np.savez_compressed(tmp, tok=words.tok, s=words.s, p=words.p, aligned=words.aligned)
        tmp.replace(cache)
    return words


# ------------------------------------------------------------ tape channels
_PLANS = {}
_SHA = {}


def _file_sha(path):
    """Checksum memoized by (path, size, mtime) within one process."""
    st = Path(path).stat()
    key = (str(path), st.st_size, st.st_mtime_ns)
    if key not in _SHA:
        _SHA[key] = sha256_file(path)
    return _SHA[key]


def _plan_cache(path, sha):
    if sha not in _PLANS:
        _PLANS[sha] = read_json(path)
    return _PLANS[sha]


def tape_names(store: TranscriptStore = STORES['tape']) -> list[str]:
    base = store.sources / 'tape'
    return sorted(p.name for p in base.iterdir() if p.is_dir()) if base.is_dir() else []


def tape_channels(tape: str, store: TranscriptStore = STORES['tape'], deflutter_root: Path = DEFLUTTERED_ROOT) -> list[dict]:
    """Every published 05b channel of a tape with its canonical origin, checked in four records.

    The 06a identity must be the one made from the current 05b review, its origin must equal the
    review's ``start_seconds``, the review must name the plan it was rendered from and that plan
    must list the same start. A channel failing any check is returned with ``problem`` set and is
    never used (no fallback to local WAV time or to the acquisition start).
    """
    out = []
    for folder in sorted((Path(deflutter_root) / tape).glob('CH[0-9][0-9]')):
        review_path = folder / 'review.json'
        if not review_path.is_file() or not (folder / 'deflutter.wav').is_file():
            continue
        review = read_json(review_path)
        if review.get('status') != 'complete':
            continue
        channel = int(review['channel'])
        review_sha = sha256_file(review_path)
        row = dict(tape=tape, channel=channel, review_path=str(review_path), review_sha256=review_sha,
                   wav=str(folder / 'deflutter.wav'), origin_s=float(review['start_seconds']),
                   acquisition_start_s=review['source'].get('acquisition_start_seconds'),
                   duration_s=review['frames'] / review['sample_rate'], sample_rate=review['sample_rate'],
                   map_sha256=review.get('map_sha256'), plan_sha256=review.get('plan_sha256'),
                   identity_dir=None, identity_id=None, problem=None)
        base = store.sources / 'tape' / tape / f'CH{channel:02d}'
        candidates = []
        for identity in sorted(base.iterdir()) if base.is_dir() else []:
            if (identity / 'source.json').is_file():
                record = read_json(identity / 'source.json')['identity']
                if record.get('review_sha256') == review_sha:
                    candidates.append((identity, record))
        problems = []
        if len(candidates) != 1:
            problems.append(f'{len(candidates)} 06a transcripts of the current 05b review')
        else:
            identity, record = candidates[0]
            row.update(identity_dir=str(identity), identity_id=identity.name)
            if abs(float(record['canonical_origin_s']) - row['origin_s']) > 1e-9:
                problems.append(f"06a origin {record['canonical_origin_s']} != review start {row['origin_s']}")
            if record.get('map_sha256') != row['map_sha256']:
                problems.append('06a and review name different deflutter maps')
        # The review's own planning folder, else the published deflutter_maps copy; either must
        # carry the checksum the review was rendered from.
        candidates_plan = [Path(review['shared_map']).parent / 'plan.json'] if review.get('shared_map') else []
        candidates_plan.append(Path(deflutter_root).parent / 'deflutter_maps' / tape / 'plan.json')
        plan_path = next((p for p in candidates_plan if p.is_file() and _file_sha(p) == row['plan_sha256']), None)
        row['plan_path'] = str(plan_path) if plan_path else None
        if plan_path is None:
            problems.append('no readable deflutter plan with the checksum the review names')
        else:
            plan = _plan_cache(plan_path, row['plan_sha256'])
            listed = [c for c in plan.get('inventory', {}).get('channels', []) if c.get('channel') == channel]
            if len(listed) != 1 or abs(float(listed[0]['start_seconds']) - row['origin_s']) > 1e-9:
                problems.append('deflutter plan lists another start for this channel')
            row['alignment'] = plan.get('inventory', {}).get('alignment')
        row['problem'] = '; '.join(problems) or None
        out.append(row)
    return out


# ------------------------------------------------------------------- prior
def tape_prior(tape: str, inventory_path: Path = INVENTORY) -> dict:
    """Filename clock and hum speed: GET ~ start - launch + t * hum / 60 (minutes off)."""
    inventory = read_json(inventory_path)
    rows = [r for r in inventory['tapes'] if r['tape'] == tape]
    if len(rows) != 1:
        raise ValueError(f'{tape} is not in {inventory_path}')
    row = rows[0]
    hum = row.get('hum_hz') or inventory['hum_hz']['median']
    start = row.get('inferred_start', row['start'])
    return dict(tape=tape, get_at_zero_s=float(start - inventory['launch']), rate=float(hum / 60.0),
                hum_hz=float(hum), hum_from_tape=bool(row.get('hum_hz')), status=row.get('status'),
                head=row.get('head'), recorder=row.get('recorder'))


def prior_get(prior: dict, t):
    return prior['get_at_zero_s'] + np.asarray(t, float) * prior['rate']


# ------------------------------------------------------------- SG sources
def drift(reel: dict, t):
    scale = reel.get('drift_scale_s') or reel['duration_s']
    t = np.asarray(t, float)
    return sum(c * (t / scale) ** (d + 1) for d, c in enumerate(reel['drift_coefficients_s']))


def identity_by_content(store: TranscriptStore, collection: str, name: str, content_sha256: str) -> Path | None:
    """The transcript identity of exactly this audio, chosen by checksum (not size and mtime)."""
    base = store.sources / collection / name
    found = []
    for p in sorted(base.iterdir()) if base.is_dir() else []:
        if (p / 'source.json').is_file() and read_json(p / 'source.json')['identity'].get('content_sha256') == content_sha256:
            found.append(p)
    if len(found) > 1:
        raise RuntimeError(f'{name}: {len(found)} transcript identities with the same content')
    return found[0] if found else None


def ia_sources(maps_path: Path = DRIFT_MAPS, store: TranscriptStore = STORES['ia']) -> list[dict]:
    """IA reels with a GET placement, their segments and the transcript of the mapped audio."""
    maps = read_json(maps_path)
    out = []
    for reel in maps['reels']:
        if reel.get('status') != 'fitted' or not reel.get('segments'):
            continue
        identity = identity_by_content(store, 'sg', reel['reel'], reel['sha256'])
        segments = []
        for s in reel['segments']:
            a, b = float(s['source_start_s']), float(s['source_end_s'])
            c = float(s['constant_s'])
            segments.append(dict(key=f"ia:{reel['reel']}:{s['segment']}", kind='ia', source=reel['reel'],
                                 source_start_s=a, source_end_s=b, constant_s=c,
                                 get_start_s=float(a + drift(reel, a) + c), get_end_s=float(b + drift(reel, b) + c),
                                 prior_sigma_s=float(reel.get('cv_mae_s') or 2.0), anchors=s.get('anchors')))
        out.append(dict(kind='ia', name=reel['reel'], identity_dir=str(identity) if identity else None,
                        audio=str(IA_AUDIO_ROOT / reel['file']), audio_sha256=reel['sha256'], reel=reel,
                        segments=segments))
    return out


def nara_sources(root: Path = NARA_RESPEED_ROOT, store: TranscriptStore = STORES['nara']) -> list[dict]:
    """NARA flight tapes with IRIG placement: GET = output seconds + segment constant."""
    out = []
    for folder in sorted(Path(root).glob('255-PAOa-*')):
        short = folder.name.removeprefix('255-PAOa-')
        meta_path = folder / f'{short}.json'
        if not meta_path.is_file():
            continue
        meta = read_json(meta_path)
        meta_sha = sha256_file(meta_path)
        if not meta.get('segments'):
            continue
        identity = None
        base = store.sources / 'sg_nara' / short
        for p in sorted(base.iterdir()) if base.is_dir() else []:
            if (p / 'source.json').is_file() and read_json(p / 'source.json')['identity'].get('review_sha256') == meta_sha:
                identity = p
        segments = []
        for k, s in enumerate(meta['segments']):
            c = float(s['get_equals_output_plus_s'])
            a, b = float(s['output_start_s']), float(s['output_end_s'])
            segments.append(dict(key=f'nara:{short}:{k}', kind='nara', source=short, source_start_s=a, source_end_s=b,
                                 constant_s=c, get_start_s=a + c, get_end_s=b + c, prior_sigma_s=None,
                                 untimed_before_s=s.get('untimed_before_s'), untimed_after_s=s.get('untimed_after_s')))
        out.append(dict(kind='nara', name=short, identity_dir=str(identity) if identity else None,
                        audio=str(folder / meta['outputs']['mix']['file']), respeed=str(meta_path),
                        respeed_sha256=meta_sha, sample_rate=meta['sample_rate'], segments=segments))
    return out


def source_get(source: dict, s):
    """GET of source seconds ``s`` and the index of its segment (-1 outside every segment)."""
    s = np.asarray(s, float)
    get = np.full(s.shape, np.nan)
    seg = np.full(s.shape, -1, int)
    for k, g in enumerate(source['segments']):
        inside = (s >= g['source_start_s']) & (s < g['source_end_s'])
        if source['kind'] == 'ia':
            get[inside] = s[inside] + drift(source['reel'], s[inside]) + g['constant_s']
        else:
            get[inside] = s[inside] + g['constant_s']
        seg[inside] = k
    return get, seg


def source_seconds(source: dict, segment: int, get):
    """Inverse of ``source_get`` inside one segment."""
    g = source['segments'][segment]
    get = np.asarray(get, float)
    if source['kind'] == 'nara':
        return get - g['constant_s']
    grid = np.linspace(g['source_start_s'], g['source_end_s'], 4097)
    mapped = grid + drift(source['reel'], grid) + g['constant_s']
    return np.interp(get, mapped, grid)


def segment_at_get(source: dict, get_lo: float, get_hi: float) -> int | None:
    for k, g in enumerate(source['segments']):
        if g['get_start_s'] <= get_lo and get_hi <= g['get_end_s']:
            return k
    return None
