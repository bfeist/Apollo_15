"""Deterministic transcript evidence for channel identification (stage 10).

For one tape this reads the stage-06a timing transcript of every delivered channel and

* marks how widely each segment is heard. A segment's words are matched against words
  of the other channels at the same canonical tape time (+-``SHARE_WINDOW_S``). Speech
  heard on many channels at once is broadcast background (air-to-ground, the flight
  loop); speech heard on one channel, or on it and one or two neighbours (crosstalk), is
  the distinctive content of that loop;
* finds the voice-annotation readout ("Channel 14, CAPCOM left, position 3010 ...") on
  each channel, per hour, as an independent anchor for true channel 30;
* keeps a pairwise sharing matrix, so physical adjacency (crosstalk) can be checked;
* selects time-stamped excerpts of distinctive speech for the language model.

Nothing here knows or uses the delivered channel labels.
"""
from __future__ import annotations

import collections
import gzip
import hashlib
import json
import math
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / '00_common'))
from transcript_read import identity_state  # noqa: E402

EVIDENCE_VERSION = 'channelid-evidence-v1'
SHARE_WINDOW_S = 2.0          # word match tolerance across channels (crosstalk repeats are ~1.3 s)
BROADCAST_MIN_CHANNELS = 4    # a segment heard on >= this many *other* channels is background
EXCERPT_WINDOW_S = 300.0
EXCERPT_WORD_BUDGET = 4500
EXCERPT_MAX_WINDOWS = 14
EXCERPT_MIN_SEPARATION_S = 1800.0

STOP = set('''the and that this with have from they them then than there their what when where which
will would could should about your you're we're it's that's okay roger yeah yes copy just like
been were over into here some they're going know think right well good thank thanks were what's
let's that'll there's don't didn't can't won't i'll we'll you'll he's she's also only very much
more then them okay. alright'''.split())
HALLUCINATIONS = {'you', 'thank you', 'thank you.', 'bye', 'bye.', '.', 'okay.', 'so', 'uh', 'um'}

WORD = re.compile(r"[a-z0-9']+")
NUM = r'(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty(?:[- ]\w+)?|thirty)'
ANNO_CHANNEL = re.compile(rf'\b(?:channel|panel|title|tunnel|chamber)\s+{NUM}\b', re.I)
ANNO_POSITION = re.compile(r'\b(?:position|division|physician|posit)s?\s+\d', re.I)
ANNO_RECORDER = re.compile(r'\b(?:on\s+)?h\.?\s?[rlvi]\.?\s?[12]\b|historical recorder', re.I)


def norm_words(text):
    return [w.strip("'") for w in WORD.findall(text.lower())]


def content_words(text):
    return [w for w in norm_words(text) if len(w) >= 4 and w not in STOP]


def is_noise(row):
    text = row['text'].strip().lower()
    if text in HALLUCINATIONS or not norm_words(text):
        return True
    if (row.get('compression_ratio') or 0) > 2.6:
        return True
    if (row.get('avg_logprob') or 0) < -1.2:
        return True
    return False


def annotation_score(text):
    """1 for a line that looks like a track-sheet readout, else 0."""
    if ANNO_RECORDER.search(text) and ANNO_CHANNEL.search(text):
        return 1
    if ANNO_CHANNEL.search(text) and ANNO_POSITION.search(text):
        return 1
    if ANNO_POSITION.search(text) and len(norm_words(text)) <= 6:
        return 1
    return 0


# ---------------------------------------------------------------------------- sources

def tape_sources(transcript_root: Path, tape: str):
    """{delivered channel: identity dir} of one tape (newest registration wins)."""
    out = {}
    tape_dir = Path(transcript_root) / 'sources' / 'tape' / tape
    for chd in sorted(tape_dir.glob('CH[0-9][0-9]')):
        idents = []
        for sj in chd.glob('*/source.json'):
            meta = json.loads(sj.read_text(encoding='utf-8'))
            idents.append((meta.get('registered', ''), sj.parent, meta))
        if idents:
            _, folder, meta = max(idents, key=lambda x: x[0])
            out[int(chd.name[2:])] = (folder, meta)
    return out


def read_rows(identity_dir: Path):
    rows = []
    for f in sorted((Path(identity_dir) / 'chunks').glob('c[0-9]*/segments.jsonl.gz')):
        with gzip.open(f, 'rt', encoding='utf-8') as stream:
            rows.extend(json.loads(line) for line in stream if line.strip())
    rows.sort(key=lambda r: r.get('sf') if r.get('sf') is not None else -1)
    return rows


# ---------------------------------------------------------------------------- evidence

def build(transcript_root: Path, tape: str, log=print):
    sources = tape_sources(transcript_root, tape)
    if not sources:
        raise FileNotFoundError(f'No transcripts for {tape}')
    head = None
    segs = {}           # ch -> list of dict(t, e, text, cw)
    provenance = {}
    for ch, (folder, meta) in sorted(sources.items()):
        src = meta['source']
        review = src.get('review') or {}
        name = review.get('source_filename', '')
        m = re.search(r'_(HR[12][UL])_', name)
        head = head or (m[1] if m else None)
        origin = float(src.get('canonical_origin_s') or 0.0)
        rate = float(src['sample_rate'])
        state = identity_state(folder)
        provenance[ch] = dict(identity=str(folder), source_filename=name, relpath=src.get('relpath'),
                              content_sha256=src.get('content_sha256'), transcript_sha256=state['sha256'],
                              chunks=state['chunks'], aligned_chunks=state['aligned_chunks'],
                              hours=src.get('hours'), canonical_origin_s=origin)
        out = []
        for r in read_rows(folder):
            if r.get('sf') is None or is_noise(r):
                continue
            t = origin + r['sf'] / rate
            e = origin + (r.get('ef') or r['sf']) / rate
            words = [(origin + w['sf'] / rate, w['w']) for w in (r.get('words') or []) if w.get('sf') is not None]
            out.append(dict(t=t, e=e, text=r['text'].strip(), words=words))
        # drop lines repeated very often on one channel (decoder loops)
        freq = collections.Counter(s['text'].lower() for s in out)
        segs[ch] = [s for s in out if freq[s['text'].lower()] <= 25 or len(s['text']) > 40]
        log(f'  {tape} CH{ch:02d}: {len(segs[ch])} segments')

    # word index: (word, time bin) -> set of channels
    binw = SHARE_WINDOW_S
    index = collections.defaultdict(set)
    for ch, rows in segs.items():
        for s in rows:
            for t, w in s['words']:
                for cw in content_words(w):
                    index[(cw, int(t // binw))].add(ch)

    share_pairs = collections.Counter()
    channels = {}
    for ch, rows in segs.items():
        for s in rows:
            hits = collections.Counter()
            n = 0
            for t, w in s['words']:
                for cw in content_words(w):
                    n += 1
                    b = int(t // binw)
                    seen = set()
                    for bb in (b - 1, b, b + 1):
                        seen |= index.get((cw, bb), set())
                    seen.discard(ch)
                    hits.update(seen)
            need = max(1, math.ceil(0.5 * n))
            shared = sorted(c for c, k in hits.items() if n and k >= need)
            s['n'] = n
            s['shared'] = shared
            if 0 < len(shared) < BROADCAST_MIN_CHANNELS:
                for c in shared:
                    share_pairs[(ch, c)] += 1
        channels[ch] = summarize(rows)

    anchors = annotation_anchors(segs)
    for ch, rows in segs.items():
        channels[ch]['excerpt'] = excerpt(rows)

    return dict(version=EVIDENCE_VERSION, tape=tape, head=head, recorder=head[:3] if head else None,
                delivered_channels=sorted(segs), provenance=provenance, channels=channels, anchors=anchors,
                share_pairs=[[a, b, k] for (a, b), k in sorted(share_pairs.items())],
                settings=dict(share_window_s=SHARE_WINDOW_S, broadcast_min_channels=BROADCAST_MIN_CHANNELS,
                              excerpt_word_budget=EXCERPT_WORD_BUDGET, excerpt_window_s=EXCERPT_WINDOW_S))


def summarize(rows):
    words = sum(len(norm_words(s['text'])) for s in rows)
    own = sum(len(norm_words(s['text'])) for s in rows if not s['shared'])
    local = sum(len(norm_words(s['text'])) for s in rows if 0 < len(s['shared']) < BROADCAST_MIN_CHANNELS)
    broad = words - own - local
    hours = collections.Counter(int(s['t'] // 3600) for s in rows if not s['shared'])
    return dict(segments=len(rows), words=words, own_words=own, local_shared_words=local,
                broadcast_words=broad, own_words_by_hour={str(k): v for k, v in sorted(hours.items())})


def annotation_anchors(segs):
    """Channel carrying the track-sheet readouts, overall and per hour."""
    per_ch = collections.Counter()
    per_hour = collections.defaultdict(collections.Counter)
    examples = {}
    for ch, rows in segs.items():
        for s in rows:
            if annotation_score(s['text']) and len(s['shared']) < BROADCAST_MIN_CHANNELS:
                per_ch[ch] += 1
                per_hour[int(s['t'] // 3600)][ch] += 1
                examples.setdefault(ch, []).append([round(s['t'], 1), s['text'][:140]])
    ranked = per_ch.most_common()
    best = ranked[0][0] if ranked else None
    total = sum(per_ch.values())
    hourly = []
    for h in sorted(per_hour):
        c = per_hour[h].most_common()
        hourly.append(dict(hour=h, channel=c[0][0], hits=c[0][1], total=sum(per_hour[h].values())))
    return dict(channel=best, hits=ranked[0][1] if ranked else 0, total=total,
                share=round(ranked[0][1] / total, 3) if total else 0.0,
                ranking=[[c, k] for c, k in ranked[:6]], hourly=hourly,
                examples={str(c): ex[:12] for c, ex in examples.items() if c in dict(ranked[:3])})


def excerpt(rows):
    """Time-stamped distinctive lines from up to EXCERPT_MAX_WINDOWS spread-out windows."""
    keep = [s for s in rows if len(s['shared']) < BROADCAST_MIN_CHANNELS]
    if not keep:
        return []
    windows = collections.defaultdict(list)
    for s in keep:
        windows[int(s['t'] // EXCERPT_WINDOW_S)].append(s)

    def weight(ws):
        return sum(len(norm_words(s['text'])) * (1.0 if not s['shared'] else 0.5) for s in ws)

    ranked = sorted(windows, key=lambda w: -weight(windows[w]))
    chosen = []
    for w in ranked:
        if len(chosen) >= EXCERPT_MAX_WINDOWS:
            break
        if all(abs(w - c) * EXCERPT_WINDOW_S >= EXCERPT_MIN_SEPARATION_S for c in chosen):
            chosen.append(w)
    for w in ranked:                      # quiet channel: allow closer windows
        if len(chosen) >= EXCERPT_MAX_WINDOWS or weight(windows[w]) <= 0:
            break
        if w not in chosen:
            chosen.append(w)
    budget = EXCERPT_WORD_BUDGET
    per_window = max(120, budget // max(1, len(chosen)))
    out = []
    for w in sorted(chosen):
        used = 0
        for s in windows[w]:
            n = len(norm_words(s['text']))
            if used + n > per_window:
                break
            used += n
            out.append([round(s['t'], 1), s['text'], s['shared']])
    return out


def evidence_sha(evidence):
    return hashlib.sha256(json.dumps(evidence, sort_keys=True).encode()).hexdigest()


def write(path: Path, evidence):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    with gzip.open(tmp, 'wt', encoding='utf-8') as stream:
        json.dump(evidence, stream)
    tmp.replace(path)


def read(path: Path):
    with gzip.open(path, 'rt', encoding='utf-8') as stream:
        return json.load(stream)
