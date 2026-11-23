"""Resumable first-pass and rescue transcription over the mission-timing sources."""
from __future__ import annotations

import math
import time
import traceback
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor

import numpy as np

from mission_asr import ASR_RATE, ChunkSpec, Settings, chunk_specs, decode_group, prepare, resample
from audio_sources import Reader, Source, gpu_description
from mission_store import Store, append_jsonl, now, read_json, write_json

# Pilot measurements: seconds of wall time per source hour and per retained hour
# (two-point fit of the gain-assisted census, 66.13 s for 9 h at 33.8 % retained,
# and the sustained T510093 run, 92.44 s for 7.5 h at 68.3 % retained).
PILOT_SECONDS_PER_SOURCE_HOUR = 2.47
PILOT_SECONDS_PER_RETAINED_HOUR = 14.43
COLLECTION_ORDER = {'sg': 0, 'dvd': 1, 'tape': 2}


def order_key(spec: ChunkSpec):
    s = spec.source
    return (COLLECTION_ORDER.get(s.collection, 9), s.tape or s.key.rsplit('/', 1)[0], spec.index, s.key)


def flag_counts(rows):
    words, segments = Counter(), Counter()
    total_words = 0
    for row in rows:
        segments.update(row['flags'])
        for word in row['words']:
            total_words += 1
            words.update(word['flags'])
    return total_words, dict(words), dict(segments)


def receipt_for(item, rows, session, group, decode_share, gpu):
    source = item.spec.source
    rate = source.sample_rate
    to_frame = lambda index: round(item.read_start + index * rate / ASR_RATE)
    windows = [[to_frame(a), to_frame(b), flags] for a, b, flags in item.windows]
    window_index = {(a, b): i for i, (a, b, _) in enumerate(item.windows)}
    contexts = [[window_index[w] for w in context] for context in item.contexts]
    words, word_flags, segment_flags = flag_counts(rows)
    return dict(
        source_key=source.key, index=item.spec.index, sample_rate=rate,
        nominal_frames=[item.spec.nominal_start, item.spec.nominal_end],
        cut_frames=[item.cut_start, item.cut_end], owned_sha256=item.owned_sha256,
        owned_seconds=item.owned_seconds, retained_seconds=item.retained_seconds,
        canonical_origin_s=source.canonical_origin_s, pts_origin_s=source.pts_origin_s,
        windows=windows, contexts=contexts, segments=len(rows), words=words,
        word_flags=word_flags, segment_flags=segment_flags,
        timing=dict(prepare_seconds=item.prepare_seconds, vad_seconds=item.vad_seconds,
                    decode_share_seconds=decode_share), session=session, group=group, gpu=gpu)


def neighbour_check(store, settings_id, item, layer=None):
    """Adjacent completed chunks must report the same cut frames as this chunk."""
    source, index = item.spec.source, item.spec.index
    for other, mine, theirs in ((index - 1, item.cut_start, 1), (index + 1, item.cut_end, 0)):
        if other < 0:
            continue
        path = store.chunk_dir(settings_id, source, other, layer) / 'chunk.json'
        if path.is_file():
            receipt = read_json(path)
            if receipt.get('identity_id') == source.identity_id and receipt['cut_frames'][theirs] != mine:
                raise ValueError(f'Boundary ownership disagrees with chunk {other} of {source.key}: '
                                 f'{receipt["cut_frames"][theirs]} != {mine}')


def plan(store, settings: Settings, sources, windows=None, verify=False):
    """Split selected chunks into reusable and pending, keeping a stable order."""
    settings_id = settings.settings_id
    pending, reused = [], []
    for source in sources:
        window = windows.get(source.key, windows.get('*')) if windows else None
        for spec in chunk_specs(source, settings, window):
            receipt = store.completed(settings_id, source, spec.index, (spec.nominal_start, spec.nominal_end),
                                      verify=verify)
            (reused if receipt else pending).append((spec, receipt))
    pending.sort(key=lambda item: order_key(item[0]))
    return [s for s, _ in pending], reused


def spec_hours(spec):
    return (spec.nominal_end - spec.nominal_start) / spec.source.sample_rate / 3600


def groups_of(specs, group_hours):
    group, hours = [], 0.0
    for spec in specs:
        group.append(spec)
        hours += spec_hours(spec)
        if hours >= group_hours:
            yield group
            group, hours = [], 0.0
    if group:
        yield group


def run(store: Store, settings: Settings, sources, *, windows=None, recognizer_factory=None, group_hours=5.0,
        prep_workers=8, max_hours=None, max_chunks=None, scope=None, log=print, detector=None):
    """Transcribe pending chunks; every completed group publishes its chunks atomically."""
    settings.validate()
    settings_id = settings.settings_id
    description = settings.describe()
    store.register_settings(settings_id, description)
    with store.lock():
        removed = store.clean_temporaries(settings_id)
        for source in sources:
            store.register_source(settings_id, source)
        pending, reused = plan(store, settings, sources, windows)
        if max_chunks is not None:
            pending = pending[:max_chunks]
        selected_hours = sum(spec_hours(s) for s in pending) + sum(spec_hours(s) for s, _ in reused)
        gpu = gpu_description()
        session, folder = store.new_session('first_pass', dict(
            settings_id=settings_id, settings=description, scope=scope or {}, gpu=gpu,
            sources=len(sources), selected_channel_hours=selected_hours,
            reused_chunks=len(reused), reused_channel_hours=sum(spec_hours(s) for s, _ in reused),
            pending_chunks=len(pending), pending_channel_hours=sum(spec_hours(s) for s in pending),
            removed_incomplete_temporaries=removed))
        write_json(folder / 'sources.json', dict(windows=windows, sources=[s.to_json() for s in sources]))
        log(f'Session {session}: {len(pending)} pending chunks ({sum(spec_hours(s) for s in pending):.2f} channel-hours); '
            f'reusing {len(reused)} completed chunks; removed {removed} incomplete temporaries')
        began = time.perf_counter()
        status = 'complete'
        if not pending:
            write_json(folder / 'session.json', dict(read_json(folder / 'session.json'), status=status,
                                                     finished=now(), wall_seconds=0.0))
            return session
        recognizer = (recognizer_factory or default_recognizer)(settings)
        load_seconds = getattr(recognizer, 'load_seconds', 0.0)
        record = read_json(folder / 'session.json')
        write_json(folder / 'session.json', dict(record, model_load_seconds=load_seconds))
        caches = defaultdict(dict)
        done_hours = 0.0
        last = time.perf_counter()
        pool = ThreadPoolExecutor(max_workers=prep_workers, thread_name_prefix='prepare')

        def prepare_group(group):
            futures = [(spec, pool.submit(prepare, spec, settings, caches[spec.source.key],
                                          *( [detector] if detector else []))) for spec in group]
            return futures

        try:
            batches = list(groups_of(pending, group_hours))
            upcoming = prepare_group(batches[0])
            for number, group in enumerate(batches):
                current = upcoming
                ready = []
                for spec, future in current:
                    try:
                        item = future.result()
                        neighbour_check(store, settings_id, item)
                        ready.append(item)
                    except Exception as error:  # noqa: BLE001 - recorded, run continues
                        append_jsonl(folder / 'failures.jsonl', dict(
                            t=now(), source_key=spec.source.key, chunk=spec.index, phase='prepare',
                            error=f'{type(error).__name__}: {error}', trace=traceback.format_exc(limit=3)))
                        log(f'  failed to prepare {spec.source.key} c{spec.index:05d}: {error}')
                # Read and gate the next group on CPU while this group decodes.
                stop = store.stop_requested() or (max_hours and (time.perf_counter() - began) / 3600 >= max_hours)
                upcoming = prepare_group(batches[number + 1]) if number + 1 < len(batches) and not stop else None
                if not ready:
                    continue
                try:
                    rows, decode_seconds, contexts = decode_group(recognizer, ready)
                except Exception as error:  # noqa: BLE001
                    for item in ready:
                        append_jsonl(folder / 'failures.jsonl', dict(
                            t=now(), source_key=item.spec.source.key, chunk=item.spec.index, phase='decode',
                            error=f'{type(error).__name__}: {error}', trace=traceback.format_exc(limit=3)))
                    log(f'  decode failed for group {number}: {error}')
                    continue
                retained = sum(item.retained_seconds for item in ready) or 1.0
                word_total = flagged = 0
                ledger = []
                for position, item in enumerate(ready):
                    share = decode_seconds * item.retained_seconds / retained
                    receipt = receipt_for(item, rows[position], session, number, share, gpu)
                    store.publish_chunk(settings_id, item.spec.source, item.spec.index, receipt, rows[position])
                    word_total += receipt['words']
                    flagged += sum(receipt['word_flags'].values())
                    ledger.append([item.spec.source.key, item.spec.source.identity_id, item.spec.index,
                                   item.owned_seconds, item.retained_seconds, receipt['words'],
                                   sum(receipt['word_flags'].values())])
                    item.audio = None
                now_t = time.perf_counter()
                hours = sum(item.owned_seconds for item in ready) / 3600
                done_hours += hours
                wall = now_t - last
                last = now_t
                retained_fraction = sum(item.retained_seconds for item in ready) / max(1e-9, hours * 3600)
                elapsed = now_t - began
                remaining = sum(spec_hours(s) for g in batches[number + 1:] for s in g)
                rate = done_hours / (elapsed / 3600)
                append_jsonl(folder / 'progress.jsonl', dict(
                    t=now(), group=number, chunks=len(ready), source_hours=hours,
                    retained_seconds=sum(item.retained_seconds for item in ready),
                    prepare_seconds=sum(item.prepare_seconds for item in ready), decode_seconds=decode_seconds,
                    wall_seconds=wall, elapsed_seconds=elapsed, contexts=contexts, words=word_total,
                    flagged_word_marks=flagged, done_hours=done_hours, remaining_hours=remaining, ledger=ledger))
                log(f'  group {number + 1}/{len(batches)}: {len(ready)} chunks, {hours:.2f} ch-h, '
                    f'{retained_fraction:.0%} retained, {wall:.1f} s ({hours * 3600 / max(wall, 1e-9):.0f}x); '
                    f'session {done_hours:.2f} ch-h at {rate:.0f}x, ETA {remaining / max(rate, 1e-9):.2f} h')
                if stop:
                    status = 'stopped'
                    log('Stop requested; the completed groups are published.')
                    break
        except KeyboardInterrupt:
            status = 'interrupted'
            log('Interrupted; incomplete group discarded, completed chunks retained.')
            raise
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
            record = read_json(folder / 'session.json')
            write_json(folder / 'session.json', dict(record, status=status, finished=now(),
                                                     wall_seconds=time.perf_counter() - began,
                                                     processed_channel_hours=done_hours))
        return session


def default_recognizer(settings):
    from mission_asr import WhisperRecognizer
    return WhisperRecognizer(settings)


# --------------------------------------------------------------------------- rescue

RESCUE_FLAGS = ('collapsed_timing', 'high_compression', 'word_spans_vad_gap')


def rescue_candidates(store, settings, sources, *, include_empty_windows=False, pad_seconds=2.0):
    """Spans whose first-pass timing or text is questionable, in source frames."""
    settings_id = settings.settings_id
    found = []
    for source in sources:
        rate = source.sample_rate
        pad = round(pad_seconds * rate)
        for folder, receipt in store.source_chunks(settings_id, source):
            spans = []
            for row in store.chunk_rows(folder):
                if any(f in row['flags'] for f in RESCUE_FLAGS) and row['sf'] is not None:
                    spans.append((row['sf'], row['ef'], [f for f in row['flags'] if f in RESCUE_FLAGS]))
            if include_empty_windows:
                covered = [(r['sf'], r['ef']) for r in store.chunk_rows(folder) if r['sf'] is not None]
                for a, b, _ in receipt['windows']:
                    if (b - a) / rate >= 1.0 and not any(x < b and y > a for x, y in covered):
                        spans.append((a, b, ['empty_window']))
            lo, hi = receipt['cut_frames']
            merged = []
            for a, b, reasons in sorted(spans):
                a, b = max(lo, a - pad), min(hi, b + pad)
                if merged and a <= merged[-1][1]:
                    merged[-1] = (merged[-1][0], max(merged[-1][1], b), sorted(set(merged[-1][2] + reasons)))
                else:
                    merged.append((a, b, reasons))
            if merged:
                found.append((source, receipt, merged))
    return found


class ContinuousClip:
    """Prepared-like object for a rescue clip: original continuous samples, no gating."""

    def __init__(self, source, chunk_receipt, start, end, reasons, settings):
        self.source = source
        self.spec = ChunkSpec(source, chunk_receipt['index'], *chunk_receipt['nominal_frames'])
        self.receipt = chunk_receipt
        mono, _ = Reader(source).read(start, end)
        self.audio = resample(mono, source.sample_rate)
        self.read_start = start
        limit = round(settings.context_max_s * ASR_RATE)
        self.windows = [(a, min(a + limit, len(self.audio)), reasons) for a in range(0, len(self.audio), limit)]
        self.contexts = [[(a, b)] for a, b, _ in self.windows]
        self.reasons = reasons

    @property
    def rate(self):
        return self.source.sample_rate

    def to_source(self, index16):
        return self.read_start + index16 * self.rate / ASR_RATE


def rescue(store: Store, settings: Settings, sources, *, recognizer_factory=None, include_empty_windows=False,
           log=print, batch_hours=1.0):
    """Re-decode questionable spans with intervening audio intact; record its cost separately."""
    settings_id = settings.settings_id
    layer = 'continuous-' + settings_id.removeprefix('asr-')
    with store.lock():
        candidates = rescue_candidates(store, settings, sources, include_empty_windows=include_empty_windows)
        pending = []
        for source, receipt, spans in candidates:
            store.register_source(settings_id, source, layer)
            done = store.completed(settings_id, source, receipt['index'], receipt['nominal_frames'], layer)
            if not done:
                pending.append((source, receipt, spans))
        clip_seconds = sum((b - a) / s.sample_rate for s, _, spans in pending for a, b, _ in spans)
        examined = sum(r['owned_seconds'] for s in sources for _, r in store.source_chunks(settings_id, s)) / 3600
        session, folder = store.new_session('rescue', dict(
            settings_id=settings_id, layer=layer, chunks=len(pending), clip_seconds=clip_seconds,
            examined_channel_hours=examined,
            include_empty_windows=include_empty_windows, sources=len(sources)))
        log(f'Rescue session {session}: {len(pending)} chunks, {clip_seconds / 3600:.3f} h of continuous clips')
        began = time.perf_counter()
        if not pending:
            write_json(folder / 'session.json', dict(read_json(folder / 'session.json'), status='complete',
                                                     finished=now(), wall_seconds=0.0))
            return session
        recognizer = (recognizer_factory or default_recognizer)(settings)
        done_seconds = 0.0
        # Batch continuous clips from many chunks per GPU call; publish each chunk afterwards.
        batch, batch_seconds = [], 0.0

        def flush(batch):
            nonlocal done_seconds
            t0 = time.perf_counter()
            clips = [(source, receipt, spans, ContinuousClip(source, receipt, a, b, reasons, settings))
                     for source, receipt, spans in batch for a, b, reasons in spans]
            rows, decode_seconds, _ = decode_group(recognizer, [c for *_, c in clips])
            by_chunk = defaultdict(list)
            for position, (source, receipt, spans, clip) in enumerate(clips):
                for row in rows[position]:
                    row['rescue_span'] = [clip.read_start, clip.read_start + round(len(clip.audio) * clip.rate / ASR_RATE)]
                    row['rescue_reasons'] = clip.reasons
                    by_chunk[source.key, receipt['index']].append(row)
            wall = time.perf_counter() - t0
            seconds = sum(len(c.audio) / ASR_RATE for *_, c in clips)
            done_seconds += seconds
            for source, receipt, spans in batch:
                rows_all = by_chunk[source.key, receipt['index']]
                words, word_flags, segment_flags = flag_counts(rows_all)
                store.publish_chunk(settings_id, source, receipt['index'], dict(
                    source_key=source.key, index=receipt['index'], sample_rate=source.sample_rate,
                    nominal_frames=receipt['nominal_frames'], cut_frames=receipt['cut_frames'],
                    first_pass_segments_sha256=receipt['segments_sha256'], spans=[[a, b, r] for a, b, r in spans],
                    clip_seconds=sum((b - a) / source.sample_rate for a, b, _ in spans), segments=len(rows_all),
                    words=words, word_flags=word_flags, segment_flags=segment_flags, session=session), rows_all, layer)
            append_jsonl(folder / 'progress.jsonl', dict(
                t=now(), chunks=len(batch), clips=len(clips), clip_seconds=seconds, decode_seconds=decode_seconds,
                wall_seconds=wall, source_hours=sum(r['owned_seconds'] for _, r, _ in batch) / 3600,
                elapsed_seconds=time.perf_counter() - began))
            log(f'  rescued {len(clips)} clips ({seconds / 3600:.3f} h) from {len(batch)} chunks in {wall:.1f} s')

        for item in pending:
            batch.append(item)
            batch_seconds += sum((b - a) / item[0].sample_rate for a, b, _ in item[2])
            if batch_seconds >= batch_hours * 3600:
                flush(batch)
                batch, batch_seconds = [], 0.0
        if batch:
            flush(batch)
        write_json(folder / 'session.json', dict(read_json(folder / 'session.json'), status='complete', finished=now(),
                                                 wall_seconds=time.perf_counter() - began,
                                                 model_load_seconds=getattr(recognizer, 'load_seconds', 0.0),
                                                 rescued_clip_seconds=done_seconds))
        return session


# --------------------------------------------------------------------------- reporting

def coverage(store, settings, sources, windows=None):
    settings_id = settings.settings_id
    rows = []
    for source in sources:
        window = windows.get(source.key, windows.get('*')) if windows else None
        specs = chunk_specs(source, settings, window)
        done = {receipt['index']: receipt for _, receipt in store.source_chunks(settings_id, source)}
        selected = [s for s in specs]
        complete = [done[s.index] for s in selected if s.index in done]
        rows.append(dict(
            key=source.key, collection=source.collection, hours=sum(spec_hours(s) for s in selected),
            done_hours=sum(r['owned_seconds'] for r in complete) / 3600,
            retained_hours=sum(r['retained_seconds'] for r in complete) / 3600,
            chunks=len(selected), done_chunks=len(complete), words=sum(r['words'] for r in complete),
            flagged_words=sum(sum(r['word_flags'].values()) for r in complete),
            superseded_identities=[i for i in store.identities(settings_id, source.key) if i != source.identity_id]))
    return rows


def coverage_from_ledgers(sessions, settings, sources, windows=None):
    """Coverage from session ledgers, without opening chunk receipts."""
    done = {}
    for session in sessions:
        if session.get('kind') != 'first_pass' or session.get('settings_id') != settings.settings_id:
            continue
        for progress in session['progress']:
            for key, identity, index, owned, retained, words, flagged in progress.get('ledger', []):
                done[key, identity, index] = (owned, retained, words, flagged)
    rows = []
    for source in sources:
        window = windows.get(source.key, windows.get('*')) if windows else None
        specs = chunk_specs(source, settings, window)
        complete = [done[source.key, source.identity_id, s.index] for s in specs
                    if (source.key, source.identity_id, s.index) in done]
        rows.append(dict(key=source.key, collection=source.collection, hours=sum(spec_hours(s) for s in specs),
                         done_hours=sum(c[0] for c in complete) / 3600, retained_hours=sum(c[1] for c in complete) / 3600,
                         chunks=len(specs), done_chunks=len(complete), words=sum(c[2] for c in complete),
                         flagged_words=sum(c[3] for c in complete), superseded_identities=[]))
    return rows


def throughput_model(sessions, kind='first_pass'):
    """Least-squares wall = a * source_hours + b * retained_hours over measured groups."""
    rows = [p for s in sessions if s.get('kind') == kind for p in s['progress']]
    if len(rows) < 2:
        return None
    x = np.array([[p['source_hours'], p['retained_seconds'] / 3600] for p in rows])
    y = np.array([p['wall_seconds'] for p in rows])
    coefficients, *_ = np.linalg.lstsq(x, y, rcond=None)
    a, b = (max(0.0, float(c)) for c in coefficients)
    total_hours = float(x[:, 0].sum())
    return dict(groups=len(rows), seconds_per_source_hour=a, seconds_per_retained_hour=b,
                measured_channel_hours=total_hours, measured_wall_seconds=float(y.sum()),
                measured_rate=total_hours * 3600 / float(y.sum()),
                measured_occupancy=float(x[:, 1].sum() / total_hours))


def session_summary(session):
    progress = session['progress']
    hours = sum(p['source_hours'] for p in progress) if session.get('kind') == 'first_pass' else \
        sum(p['clip_seconds'] for p in progress) / 3600
    wall = session.get('wall_seconds') or (progress[-1]['elapsed_seconds'] if progress else 0.0)
    retained = sum(p.get('retained_seconds', 0) for p in progress) / 3600
    return dict(session=session['session'], kind=session.get('kind'), status=session.get('status'),
                started=session.get('started'), hours=hours, wall_seconds=wall,
                model_load_seconds=session.get('model_load_seconds', 0.0),
                rate=hours * 3600 / wall if wall else None,
                occupancy=retained / hours if hours and session.get('kind') == 'first_pass' else None,
                failures=len(session['failures']), groups=len(progress))


def projection(remaining_hours, occupancy, model=None):
    a = model['seconds_per_source_hour'] if model else PILOT_SECONDS_PER_SOURCE_HOUR
    b = model['seconds_per_retained_hour'] if model else PILOT_SECONDS_PER_RETAINED_HOUR
    seconds = remaining_hours * (a + b * occupancy)
    return dict(occupancy=occupancy, hours=seconds / 3600,
                rate=remaining_hours * 3600 / seconds if seconds else math.inf)
