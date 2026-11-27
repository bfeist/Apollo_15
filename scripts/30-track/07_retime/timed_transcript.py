"""Stage 07b: carry the stage-06 transcript of one channel onto its retimed output clock.

The 06 transcript of a 05b channel holds word and segment frames (``sf``/``ef``) on that
channel's local 05b WAV clock. 07b retimes the WAV with ``timing_map.npz`` (local input
seconds -> local output seconds), so the same map places every word on the retimed WAV. The
result is written beside ``retimed.wav``:

* ``timed_transcript.tsv``: one ``segment`` row per transcript segment followed by its ``word``
  rows. ``start_s``/``end_s`` are seconds in ``retimed.wav``; ``tape_start_s``/``tape_end_s``
  are the same instants on the retimed tape clock (``shared_plan.output_start_seconds`` +
  local); ``get_start_s``/``get_end_s`` are mission GET when the plan defines a placement (the
  earlier, real-time head row; a later-row channel was moved onto it by its head-row shift);
  ``source_start_s``/``source_end_s`` keep the 05b-local times.
* ``timed_transcript.json``: what was read and its checksums (transcript identity, chunk
  state, timing map, retime record and plan), the column meanings and the TSV checksum.

A completed pair is reused when its inputs are unchanged and is never rewritten. Downstream
packaging (web playback data) reads these files; this stage defines no web format.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np

from sg_inputs import STORES
from transcript_read import identity_rows, identity_state

VERSION = 'timed-transcript-v2'  # v2: head-row shift (docs/23)
COLUMNS = ('kind', 'segment', 'word', 'text', 'start_s', 'end_s', 'tape_start_s', 'tape_end_s',
           'get_start_s', 'get_end_s', 'source_start_s', 'source_end_s', 'probability', 'flags')


def sha256_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def transcript_identity(review_path, store=STORES['tape']):
    """The one 06 transcript identity made from this exact 05b review, or None."""
    review = json.loads(Path(review_path).read_text(encoding='utf-8'))
    digest = sha256_file(review_path)
    base = store.sources/'tape'/review['tape']/f"CH{int(review['channel']):02d}"
    found = [d for d in sorted(base.iterdir()) if (d/'source.json').is_file()
             and json.loads((d/'source.json').read_text(encoding='utf-8'))['identity'].get('review_sha256') == digest
             ] if base.is_dir() else []
    if len(found) > 1:
        raise ValueError(f'{len(found)} stage-06 transcripts of {review_path}')
    return found[0] if found else None


def get_rule(record):
    """(segments, rule) of the applied 07a plan's GET placement, or (None, None)."""
    shared = record.get('shared_plan') or {}
    if not shared.get('path'):
        return None, None
    plan = json.loads(Path(shared['path']).read_text(encoding='utf-8'))
    placement = plan.get('get_placement')
    return (placement['segments'], placement['rule']) if placement else (None, None)


def _cell(value):
    if value is None:
        return ''
    if isinstance(value, float):
        return f'{value:.4f}'
    return str(value).replace('\t', ' ').replace('\n', ' ')


def write_timed_transcript(destination, source, progress=print, store=STORES['tape']):
    """Write (or verify and reuse) ``timed_transcript.tsv/.json`` for one completed 07b channel."""
    destination, source = Path(destination), Path(source)
    record_path, review_path = destination/'retime.json', source.parent/'review.json'
    record = json.loads(record_path.read_text(encoding='utf-8'))
    if record.get('status') != 'complete':
        raise ValueError(f'Retime of {destination} is not complete')
    if not review_path.is_file():
        progress(f'{destination}: no 05b review beside the source; no timed transcript')
        return None
    identity = transcript_identity(review_path, store)
    if identity is None:
        progress(f'{destination}: no stage-06 transcript of this 05b channel; no timed transcript')
        return None
    state = identity_state(identity)
    source_record = json.loads((identity/'source.json').read_text(encoding='utf-8'))['identity']
    inputs = dict(transcript_identity=str(identity), transcript_settings=identity.parents[4].name,
                  transcript_state=state, review_sha256=sha256_file(review_path),
                  timing_map_sha256=sha256_file(destination/'timing_map.npz'),
                  retime_record_sha256=sha256_file(record_path),
                  shared_plan=record.get('shared_plan'))
    tsv, meta = destination/'timed_transcript.tsv', destination/'timed_transcript.json'
    if meta.is_file():
        old = json.loads(meta.read_text(encoding='utf-8'))
        if old.get('inputs') != inputs or not tsv.is_file() or sha256_file(tsv) != old.get('tsv_sha256'):
            raise ValueError(f'{destination}: the timed transcript inputs changed; write 07b to a new retimed root')
        progress(f'{destination}: reusing verified timed transcript')
        return old

    rate = float(source_record['sample_rate'])
    with np.load(destination/'timing_map.npz') as archive:
        local_in, local_out = archive['input_seconds'], archive['output_seconds']
    origin_out = float((record.get('shared_plan') or {}).get('output_start_seconds', 0.0))
    origin_in = float(source_record['canonical_origin_s'])
    segments, rule = get_rule(record)
    knots = ((record.get('shared_plan') or {}).get('head_row_shift') or {}).get('knots')

    def place(frame):
        if frame is None:
            return None, None, None, None
        local = frame/rate
        out = float(np.interp(local, local_in, local_out))
        tape = origin_out+out
        get = None
        if segments:
            canonical = origin_in+local
            if knots:  # the real-time (earlier-row) tape position of a later-row channel
                from head_rows import shift_at
                canonical -= float(shift_at(knots, canonical))
            seg = next((s for s in segments if s['tape_start_s'] <= canonical <= s['tape_end_s']), None)
            get = None if seg is None else tape+seg['get_equals_output_plus_s']
        return out, tape, get, local

    lines = ['\t'.join(COLUMNS)]
    counts = dict(segments=0, words=0)
    for index, row in enumerate(identity_rows(identity)):
        rows = [('segment', None, row)] + [('word', w, word) for w, word in enumerate(row.get('words') or [])]
        for kind, w, item in rows:
            start, tape_start, get_start, src_start = place(item.get('sf'))
            end, tape_end, get_end, src_end = place(item.get('ef'))
            text = item.get('text') if kind == 'segment' else item.get('w')
            probability = item.get('p') if kind == 'word' else item.get('avg_logprob')
            lines.append('\t'.join(_cell(v) for v in (kind, index, w, (text or '').strip(), start, end, tape_start,
                                                      tape_end, get_start, get_end, src_start, src_end, probability,
                                                      ','.join(item.get('flags') or []))))
            counts['segments' if kind == 'segment' else 'words'] += 1
    pending = tsv.with_name(tsv.name+'.partial')
    pending.write_bytes(('\n'.join(lines)+'\n').encode('utf-8'))
    pending.replace(tsv)
    out = dict(schema=1, version=VERSION, status='complete', inputs=inputs, counts=counts,
               tsv_sha256=sha256_file(tsv), implementation_sha256=sha256_file(__file__),
               time_basis=dict(start_s='seconds in retimed.wav (07b timing_map.npz applied to 05b-local seconds)',
                               tape_start_s='retimed tape clock: shared_plan.output_start_seconds + start_s',
                               get_start_s=rule or 'no GET placement in the applied plan',
                               source_start_s='05b-local seconds; canonical tape seconds = canonical_origin_s + source',
                               canonical_origin_s=origin_in, probability='word: recognizer word probability; '
                               'segment: average log-probability'),
               aligned=state['aligned_chunks'] == state['chunks'], columns=list(COLUMNS))
    pending = meta.with_name(meta.name+'.partial')
    pending.write_text(json.dumps(out, indent=1, allow_nan=False)+'\n', encoding='utf-8')
    pending.replace(meta)
    progress(f"{destination}: timed transcript, {counts['segments']:,} segments, {counts['words']:,} words")
    return out
