"""Forced alignment of recognized words to the audio with a CTC acoustic model (WhisperX method).

Whisper supplies the words. Its own word times (cross-attention DTW) are unreliable on this
material: collapsed segments put many words at one instant, and openings can be tens of seconds
off. Here a character CTC model (torchaudio ``WAV2VEC2_ASR_BASE_960H``) scores every 20 ms frame
of the exact samples Whisper decoded, and a Viterbi path through those scores places each
character of the known text. Word times follow from their characters.

The unit is one decoder context (at most ~29 s of retained speech):

* Its text is every segment Whisper produced from it, in decoding order.
* Each word is spelled for the model: upper-case letters and apostrophes. Digits become words
  (two digits or fewer as a cardinal, longer numbers digit by digit, ``.`` inside a number as
  POINT). A word with nothing to align (punctuation alone) is placed between its neighbours and
  flagged ``interpolated``.
* Its audio is the context's retained windows, concatenated as Whisper saw them. A frame maps
  back to the source sample through its window.

Aligned words keep Whisper's times as ``wsf``/``wef`` and gain ``ap``, the mean probability of
their characters' frames. On this narrowband audio ``ap`` is often low for correctly placed words
(the pilot found words at 0.005-0.02 within 0.16 s of a visual pick), so it is not a flag. When a
context cannot be aligned (more characters than frames, usually invented text) its words keep the
Whisper times and flags and its segments carry ``align_failed``.

Measured (research/mission_timing/align_pilot.py, November 25): on the 30 matched reviewed call
openings of PAO reel 572-AAA the aligned onsets have a median error of 0.076 s and a worst of
0.30 s; Whisper's own were 0.165 s and 27.9 s.

Work is split so the GPU and CPU overlap: ``plan_chunk`` (CPU: spelling, context audio),
``emit`` (GPU: frame scores), ``finish_chunk`` (CPU: Viterbi paths and word times).
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from bisect import bisect_right

import numpy as np

ALIGN_RATE = 16000
ALIGN_MODEL = 'WAV2VEC2_ASR_BASE_960H'
ALIGN_VERSION = 1
LONG_WORD_SECONDS = 2.5

ONES = ['ZERO', 'ONE', 'TWO', 'THREE', 'FOUR', 'FIVE', 'SIX', 'SEVEN', 'EIGHT', 'NINE', 'TEN', 'ELEVEN',
        'TWELVE', 'THIRTEEN', 'FOURTEEN', 'FIFTEEN', 'SIXTEEN', 'SEVENTEEN', 'EIGHTEEN', 'NINETEEN']
TENS = ['', '', 'TWENTY', 'THIRTY', 'FORTY', 'FIFTY', 'SIXTY', 'SEVENTY', 'EIGHTY', 'NINETY']
SYMBOLS = {'%': 'PERCENT', '&': 'AND', '+': 'PLUS', '@': 'AT', '#': 'NUMBER', '$': 'DOLLARS'}
# Whisper timing flags, replaced by the aligned ones.
WORD_TIMING_FLAGS = {'zero_duration', 'clipped', 'spans_vad_gap', 'long', 'invalid_time', 'outside_context'}
SEGMENT_TIMING_FLAGS = {'collapsed_timing', 'word_spans_vad_gap', 'no_word_times', 'clipped'}
LABELS = ('-', '|', 'E', 'T', 'A', 'O', 'N', 'I', 'H', 'S', 'R', 'D', 'L', 'U', 'M', 'W', 'C', 'F', 'G', 'Y',
          'P', 'B', 'V', 'K', "'", 'X', 'J', 'Q', 'Z')   # the model's labels; checked when it loads


def describe():
    return dict(model=ALIGN_MODEL, version=ALIGN_VERSION, rate=ALIGN_RATE, labels=''.join(LABELS),
                speller=hashlib.sha256(json.dumps([ONES, TENS, SYMBOLS]).encode()).hexdigest()[:12])


def align_id():
    return 'align-' + hashlib.sha256(json.dumps(describe(), sort_keys=True).encode()).hexdigest()[:12]


def is_aligned(receipt):
    return (receipt.get('alignment') or {}).get('align_id') == align_id()


# --------------------------------------------------------------------------- spelling

def cardinal(n: int):
    if n < 20:
        return [ONES[n]]
    tens, ones = divmod(n, 10)
    return [TENS[tens]] + ([ONES[ones]] if ones else [])


def say_digits(digits: str):
    return cardinal(int(digits)) if len(digits) <= 2 else [ONES[int(d)] for d in digits]


def spell(word: str):
    """Model spelling of one recognized word: a list of letter strings (may be empty)."""
    out = []
    for piece in re.findall(r"[A-Za-z']+|\d+(?:[.,]\d+)*|[%&+@#$]", word.replace('\u2019', "'")):
        if piece[0].isdigit():
            for i, group in enumerate(piece.replace(',', '').split('.')):
                if i:
                    out.append('POINT')
                if group:
                    out.extend(say_digits(group))
        elif piece in SYMBOLS:
            out.append(SYMBOLS[piece])
        else:
            letters = piece.upper().strip("'")
            if letters:
                out.append(letters)
    return out


INDEX = {c: i for i, c in enumerate(LABELS)}
BLANK, SEPARATOR = INDEX['-'], INDEX['|']


def tokens_of(words):
    """Model tokens for a word sequence and, per token, the index of its word (None: separator)."""
    tokens, owner = [], []
    for k, word in enumerate(words):
        for part in spell(word['w']):
            if tokens and tokens[-1] != SEPARATOR:
                tokens.append(SEPARATOR)
                owner.append(None)
            for ch in part:
                if ch in INDEX:
                    tokens.append(INDEX[ch])
                    owner.append(k)
    return tokens, owner


# --------------------------------------------------------------------------- model

class Aligner:
    """The CTC model: ``emissions`` on the GPU, ``path`` (Viterbi) on the CPU."""

    def __init__(self, device='cuda'):
        import torch
        import torchaudio
        self.torch = torch
        self.functional = torchaudio.functional
        bundle = getattr(torchaudio.pipelines, ALIGN_MODEL)
        if bundle.sample_rate != ALIGN_RATE or tuple(bundle.get_labels()) != LABELS:
            raise RuntimeError(f'{ALIGN_MODEL} does not have the expected rate and labels')
        began = time.perf_counter()
        self.device = device
        self.model = bundle.get_model().to(device).eval()
        self.load_seconds = time.perf_counter() - began

    def emissions(self, audio):
        """Frame log probabilities (frames x labels) of 16 kHz ``audio``, as a CPU tensor."""
        torch = self.torch
        with torch.inference_mode():
            x = torch.from_numpy(np.ascontiguousarray(audio, dtype=np.float32))[None].to(self.device)
            logits, _ = self.model(x)
            return torch.log_softmax(logits[0].float(), dim=-1).cpu()

    def path(self, log_probs, tokens):
        """[(start_frame, end_frame, probability)] per token, or None when no path exists."""
        torch = self.torch
        if not tokens or log_probs.shape[0] < len(tokens):
            return None
        targets = torch.tensor([tokens], dtype=torch.int32)
        try:
            labels, scores = self.functional.forced_align(log_probs[None], targets, blank=BLANK)
        except RuntimeError:
            return None
        spans = self.functional.merge_tokens(labels[0], scores[0].exp())
        if len(spans) != len(tokens):
            return None
        return [(s.start, s.end, float(s.score)) for s in spans]


# --------------------------------------------------------------------------- one chunk

def context_of_rows(rows, windows, contexts):
    """{context number: [row positions in decoding order]} from the window holding each row."""
    starts = [w[0] for w in windows]
    owner = {i: number for number, context in enumerate(contexts) for i in context}
    out = {}
    for position, row in enumerate(rows):
        frame = row.get('sf')
        if frame is None:
            frame = next((w['sf'] for w in row['words'] if w.get('sf') is not None), None)
        if frame is None:
            continue
        i = bisect_right(starts, frame) - 1
        if i >= 0 and i in owner:
            out.setdefault(owner[i], []).append(position)
    for positions in out.values():
        positions.sort(key=lambda p: (rows[p].get('seg_sf') if rows[p].get('seg_sf') is not None
                                      else rows[p].get('sf') or 0, rows[p].get('sf') or 0))
    return out


def plan_chunk(rows, receipt, audio16, origin):
    """CPU work for one chunk: a copy of its rows and one job per decoder context with text.

    ``audio16`` holds 16 kHz samples whose first sample is source frame ``origin``.
    """
    rate = receipt['sample_rate']
    rows = json.loads(json.dumps(rows))
    windows, contexts = receipt['windows'], receipt['contexts']
    to16 = lambda frame: round((frame - origin) * ALIGN_RATE / rate)
    jobs, assigned = [], set()
    for number, positions in sorted(context_of_rows(rows, windows, contexts).items()):
        assigned.update(positions)
        words = [w for p in positions for w in rows[p]['words']]
        pieces, parts, offset = [], [], 0
        for i in contexts[number]:
            a, b = max(0, to16(windows[i][0])), min(len(audio16), to16(windows[i][1]))
            if b > a:
                parts.append(audio16[a:b])
                pieces.append((offset, offset + b - a, windows[i][0]))
                offset += b - a
        tokens, owner = tokens_of(words)
        jobs.append(dict(positions=positions, pieces=pieces, tokens=tokens, owner=owner,
                         audio=np.concatenate(parts) if parts and words else None))
    unassigned = [p for p in range(len(rows)) if p not in assigned]
    return dict(rows=rows, jobs=jobs, unassigned=unassigned, rate=rate)


def emit(aligner, plan):
    """GPU work: frame scores of every job that has text and audio."""
    for job in plan['jobs']:
        if job['audio'] is not None and any(o is not None for o in job['owner']):
            job['log_probs'] = aligner.emissions(job['audio'])
            job['frames16'] = len(job['audio'])
        job['audio'] = None


def _place(job, spans, words, rate, frames):
    pieces = job['pieces']
    starts16 = [p[0] for p in pieces]
    ratio = job['frames16'] / frames

    def to_source(index16, end=False):
        i = max(0, bisect_right(starts16, index16 - (1 if end else 0)) - 1)
        a, b, frame = pieces[i]
        return round(frame + (min(max(index16, a), b) - a) * rate / ALIGN_RATE), i

    found = {}
    for (start, end, score), k in zip(spans, job['owner']):
        if k is not None:
            lo, hi, total, count = found.get(k, (start, end, 0.0, 0))
            found[k] = (min(lo, start), max(hi, end), total + score, count + 1)
    for k, word in enumerate(words):
        word['wsf'], word['wef'] = word.get('sf'), word.get('ef')
        flags = [f for f in word.get('flags', []) if f not in WORD_TIMING_FLAGS]
        if k in found:
            lo, hi, total, count = found[k]
            sf, si = to_source(round(lo * ratio))
            ef, ei = to_source(round(hi * ratio), end=True)
            word['sf'], word['ef'], word['ap'] = sf, max(ef, sf + 1), round(total / count, 4)
            if si != ei:
                flags.append('spans_vad_gap')
            if (word['ef'] - word['sf']) / rate > LONG_WORD_SECONDS:
                flags.append('long')
        else:
            word['sf'] = word['ef'] = word['ap'] = None
            flags.append('interpolated')
        word['flags'] = sorted(set(flags))
    for k, word in enumerate(words):
        if word['sf'] is None:
            before = next((words[j]['ef'] for j in range(k - 1, -1, -1) if words[j]['sf'] is not None), None)
            after = next((words[j]['sf'] for j in range(k + 1, len(words)) if words[j]['sf'] is not None), None)
            word['sf'] = before if before is not None else after
            word['ef'] = after if before is not None and after is not None else word['sf']
    return len(found)


def finish_chunk(aligner, plan):
    """CPU work: paths and word times. Returns (aligned rows, stats)."""
    rows, rate = plan['rows'], plan['rate']
    stats = dict(contexts=len(plan['jobs']), failed_contexts=0, words=0, aligned_words=0, interpolated_words=0,
                 unassigned_words=sum(len(rows[p]['words']) for p in plan['unassigned']))
    for job in plan['jobs']:
        group = [rows[p] for p in job['positions']]
        words = [w for row in group for w in row['words']]
        stats['words'] += len(words)
        log_probs = job.pop('log_probs', None)
        spans = aligner.path(log_probs, job['tokens']) if log_probs is not None else None
        if spans is None:
            if words:
                stats['failed_contexts'] += 1
                for row in group:
                    row['flags'] = sorted(set(row['flags']) | {'align_failed'})
            continue
        placed = _place(job, spans, words, rate, log_probs.shape[0])
        stats['aligned_words'] += placed
        stats['interpolated_words'] += len(words) - placed
        for row in group:
            timed = [w for w in row['words'] if w['sf'] is not None]
            flags = {f for f in row['flags'] if f not in SEGMENT_TIMING_FLAGS}
            if timed:
                row['wsf'], row['wef'] = row.get('sf'), row.get('ef')
                row['sf'], row['ef'] = min(w['sf'] for w in timed), max(w['ef'] for w in timed)
            if any('spans_vad_gap' in w['flags'] for w in row['words']):
                flags.add('word_spans_vad_gap')
            row['flags'] = sorted(flags | {'aligned'})
    for p in plan['unassigned']:
        rows[p]['flags'] = sorted(set(rows[p]['flags']) | {'align_failed'})
    rows.sort(key=lambda r: (r['sf'] if r.get('sf') is not None else -1))
    return rows, stats


def align_chunk(aligner, rows, receipt, audio16, origin):
    """All three phases for one chunk."""
    plan = plan_chunk(rows, receipt, audio16, origin)
    emit(aligner, plan)
    return finish_chunk(aligner, plan)


def chunk_audio(source, receipt):
    """16 kHz samples of a chunk's owned range and the source frame of its first sample."""
    from audio_sources import Reader
    from mission_asr import resample
    lo, hi = receipt['cut_frames']
    mono, _ = Reader(source).read(lo, hi)
    return resample(mono, source.sample_rate), lo


def alignment_record(stats, whisper_segments_sha256, seconds):
    return dict(align_id=align_id(), **describe(), whisper_segments_sha256=whisper_segments_sha256,
                seconds=round(seconds, 3), **stats)
