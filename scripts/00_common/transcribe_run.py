"""Resumable timing transcription: Whisper words, CTC-aligned word times, one chunk at a time.

``run`` brings a selection to the current state in one session:

1. Pending chunks are prepared (cut, speech gate) on CPU threads, decoded by Whisper in GPU
   groups, their words aligned (``mission_align``), and published.
2. Published chunks whose words are not yet aligned with the current aligner (chunks from
   before alignment existed, or from an older aligner) are aligned in place. Whisper is not run
   again; each word keeps its Whisper times as ``wsf``/``wef``.

Both phases are resumable chunk by chunk and stop cleanly on ``stop``.
"""
from __future__ import annotations

import math
import queue
import threading
import time
import traceback
from collections import Counter, defaultdict, deque
from concurrent.futures import ThreadPoolExecutor

import numpy as np

import mission_align as aligner_module
from mission_asr import ASR_RATE, ChunkSpec, Settings, chunk_specs, decode_group, prepare
from audio_sources import Source, gpu_description
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


def with_alignment(receipt, rows, stats, whisper_sha, seconds):
    """Receipt of aligned rows: counts recomputed, the alignment recorded."""
    words, word_flags, segment_flags = flag_counts(rows)
    return dict(receipt, segments=len(rows), words=words, word_flags=word_flags, segment_flags=segment_flags,
                alignment=aligner_module.alignment_record(stats, whisper_sha, seconds))


def neighbour_check(store, settings_id, item):
    """Adjacent completed chunks must report the same cut frames as this chunk."""
    source, index = item.spec.source, item.spec.index
    for other, mine, theirs in ((index - 1, item.cut_start, 1), (index + 1, item.cut_end, 0)):
        if other < 0:
            continue
        path = store.chunk_dir(settings_id, source, other) / 'chunk.json'
        if path.is_file():
            receipt = read_json(path)
            if receipt.get('identity_id') == source.identity_id and receipt['cut_frames'][theirs] != mine:
                raise ValueError(f'Boundary ownership disagrees with chunk {other} of {source.key}: '
                                 f'{receipt["cut_frames"][theirs]} != {mine}')


def plan(store, settings: Settings, sources, windows=None, verify=False, settings_id=None):
    """Split selected chunks into reusable and pending, keeping a stable order."""
    settings_id = settings_id or store.resolve_settings(settings)
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


def default_recognizer(settings):
    from mission_asr import WhisperRecognizer
    return WhisperRecognizer(settings)


def default_aligner():
    return aligner_module.Aligner()


class _Stop(Exception):
    pass


def run(store: Store, settings: Settings, sources, *, windows=None, recognizer_factory=None, aligner_factory=None,
        group_hours=5.0, prep_workers=8, max_hours=None, max_chunks=None, scope=None, log=print, detector=None):
    """Transcribe and align pending chunks, then align published chunks that are not yet aligned."""
    settings.validate()
    settings_id = store.resolve_settings(settings)
    description = settings.describe()
    store.register_settings(settings_id, description)
    with store.lock():
        removed = store.clean_temporaries(settings_id)
        for source in sources:
            store.register_source(settings_id, source)
        pending, reused = plan(store, settings, sources, windows, settings_id=settings_id)
        if max_chunks is not None:
            pending = pending[:max_chunks]
        unaligned = [(spec, receipt) for spec, receipt in reused if not aligner_module.is_aligned(receipt)]
        selected_hours = sum(spec_hours(s) for s in pending) + sum(spec_hours(s) for s, _ in reused)
        gpu = gpu_description()
        session, folder = store.new_session('first_pass', dict(
            settings_id=settings_id, settings=description, scope=scope or {}, gpu=gpu,
            align=aligner_module.describe(), align_id=aligner_module.align_id(),
            sources=len(sources), selected_channel_hours=selected_hours,
            reused_chunks=len(reused), reused_channel_hours=sum(spec_hours(s) for s, _ in reused),
            pending_chunks=len(pending), pending_channel_hours=sum(spec_hours(s) for s in pending),
            unaligned_chunks=len(unaligned), unaligned_channel_hours=sum(spec_hours(s) for s, _ in unaligned),
            removed_incomplete_temporaries=removed))
        write_json(folder / 'sources.json', dict(windows=windows, sources=[s.to_json() for s in sources]))
        log(f'Session {session}: {len(pending)} chunks to transcribe ({sum(spec_hours(s) for s in pending):.2f} '
            f'channel-hours), {len(unaligned)} published chunks to align ({sum(spec_hours(s) for s, _ in unaligned):.2f} '
            f'channel-hours); {len(reused) - len(unaligned)} complete; removed {removed} incomplete temporaries')
        began = time.perf_counter()
        status = 'complete'
        record = read_json(folder / 'session.json')
        aligner = None
        try:
            if pending or unaligned:
                aligner = (aligner_factory or default_aligner)()
                record['align_load_seconds'] = getattr(aligner, 'load_seconds', 0.0)
            if pending:
                status = _transcribe(store, settings, settings_id, pending, aligner, session, folder, record, gpu,
                                     recognizer_factory, group_hours, prep_workers, max_hours, began, log, detector)
            if unaligned and status == 'complete':
                status = _align_published(store, settings_id, unaligned, aligner, folder, max_hours, began,
                                          prep_workers, log)
        except KeyboardInterrupt:
            status = 'interrupted'
            log('Interrupted; incomplete work discarded, published chunks retained.')
            raise
        finally:
            record = dict(read_json(folder / 'session.json'), **{k: v for k, v in record.items()
                                                                 if k.endswith('load_seconds')})
            write_json(folder / 'session.json', dict(record, status=status, finished=now(),
                                                     wall_seconds=time.perf_counter() - began))
        log(f'Session {session} {status}.')
        return session


def _transcribe(store, settings, settings_id, pending, aligner, session, folder, record, gpu, recognizer_factory,
                group_hours, prep_workers, max_hours, began, log, detector):
    recognizer = (recognizer_factory or default_recognizer)(settings)
    record['model_load_seconds'] = getattr(recognizer, 'load_seconds', 0.0)
    caches = defaultdict(dict)
    done_hours = 0.0
    last = time.perf_counter()
    status = 'complete'
    pool = ThreadPoolExecutor(max_workers=prep_workers, thread_name_prefix='prepare')

    def prepare_group(group):
        return [(spec, pool.submit(prepare, spec, settings, caches[spec.source.key],
                                   *([detector] if detector else []))) for spec in group]

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
            align_seconds = 0.0
            ledger = []
            for position, item in enumerate(ready):
                share = decode_seconds * item.retained_seconds / retained
                receipt = receipt_for(item, rows[position], session, number, share, gpu)
                t0 = time.perf_counter()
                aligned, stats = aligner_module.align_chunk(aligner, rows[position], receipt, item.audio,
                                                            item.read_start)
                align_seconds += time.perf_counter() - t0
                receipt = with_alignment(receipt, aligned, stats, None, time.perf_counter() - t0)
                store.publish_chunk(settings_id, item.spec.source, item.spec.index, receipt, aligned)
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
                align_seconds=align_seconds, wall_seconds=wall, elapsed_seconds=elapsed, contexts=contexts,
                words=word_total, flagged_word_marks=flagged, done_hours=done_hours, remaining_hours=remaining,
                ledger=ledger))
            log(f'  group {number + 1}/{len(batches)}: {len(ready)} chunks, {hours:.2f} ch-h, '
                f'{retained_fraction:.0%} retained, {wall:.1f} s ({hours * 3600 / max(wall, 1e-9):.0f}x, '
                f'align {align_seconds:.1f} s); session {done_hours:.2f} ch-h at {rate:.0f}x, '
                f'ETA {remaining / max(rate, 1e-9):.2f} h')
            if stop:
                status = 'stopped'
                log('Stop requested; the completed groups are published.')
                break
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
        write_json(folder / 'session.json', dict(read_json(folder / 'session.json'),
                                                 processed_channel_hours=done_hours))
    return status


def _align_published(store, settings_id, unaligned, aligner, folder, max_hours, began, prep_workers, log,
                     report_every=100):
    """Align published chunks in place. CPU threads read audio and plan; the GPU scores frames;
    CPU threads find the paths and publish. The output of each chunk is deterministic."""
    status = 'complete'
    halt = threading.Event()
    planned = queue.Queue(maxsize=2 * prep_workers)
    totals = Counter()
    lock = threading.Lock()
    t_start = time.perf_counter()

    def load(spec, receipt):
        source = spec.source
        folder_ = store.chunk_dir(settings_id, source, spec.index)
        rows = store.chunk_rows(folder_)
        audio, origin = aligner_module.chunk_audio(source, receipt)
        return spec, receipt, aligner_module.plan_chunk(rows, receipt, audio, origin)

    def produce():
        try:
            with ThreadPoolExecutor(max_workers=prep_workers, thread_name_prefix='align-read') as pool:
                window = deque()
                for spec, receipt in unaligned:
                    if halt.is_set():
                        break
                    window.append((spec, receipt, pool.submit(load, spec, receipt)))
                    while len(window) > 2 * prep_workers:
                        planned.put(window.popleft())
                while window and not halt.is_set():
                    planned.put(window.popleft())
        finally:
            planned.put(None)

    def finish(spec, receipt, chunk_plan, seconds):
        t0 = time.perf_counter()
        rows, stats = aligner_module.finish_chunk(aligner, chunk_plan)
        new = with_alignment(receipt, rows, stats, receipt['segments_sha256'], seconds + time.perf_counter() - t0)
        store.replace_chunk(settings_id, spec.source, spec.index, new, rows)
        with lock:
            totals['chunks'] += 1
            totals['source_seconds'] += receipt['owned_seconds']
            totals['retained_seconds'] += receipt['retained_seconds']
            for key in ('contexts', 'failed_contexts', 'words', 'aligned_words', 'interpolated_words',
                        'unassigned_words'):
                totals[key] += stats[key]

    def record_failure(spec, phase, error):
        append_jsonl(folder / 'failures.jsonl', dict(
            t=now(), source_key=spec.source.key, chunk=spec.index, phase=phase,
            error=f'{type(error).__name__}: {error}', trace=traceback.format_exc(limit=3)))
        log(f'  align {phase} failed for {spec.source.key} c{spec.index:05d}: {error}')

    producer = threading.Thread(target=produce, name='align-producer', daemon=True)
    producer.start()
    finishers = ThreadPoolExecutor(max_workers=prep_workers, thread_name_prefix='align-finish')
    outstanding = deque()
    last_report = 0
    try:
        while True:
            item = planned.get()
            if item is None:
                break
            spec, receipt, future = item
            try:
                spec, receipt, chunk_plan = future.result()
                t0 = time.perf_counter()
                aligner_module.emit(aligner, chunk_plan)
                outstanding.append((spec, finishers.submit(finish, spec, receipt, chunk_plan,
                                                           time.perf_counter() - t0)))
            except Exception as error:  # noqa: BLE001 - recorded, session continues
                record_failure(spec, 'align', error)
            while len(outstanding) > 2 * prep_workers or (outstanding and outstanding[0][1].done()):
                done_spec, done = outstanding.popleft()
                try:
                    done.result()
                except Exception as error:  # noqa: BLE001
                    record_failure(done_spec, 'publish', error)
            if totals['chunks'] - last_report >= report_every:
                last_report = totals['chunks']
                _align_progress(folder, totals, t_start, len(unaligned), log)
            if store.stop_requested() or (max_hours and (time.perf_counter() - began) / 3600 >= max_hours):
                status = 'stopped'
                log('Stop requested; aligned chunks are published.')
                raise _Stop
    except _Stop:
        pass
    finally:
        halt.set()
        while producer.is_alive():
            try:
                planned.get(timeout=0.1)
            except queue.Empty:
                pass
        for done_spec, done in outstanding:
            try:
                done.result()
            except Exception as error:  # noqa: BLE001
                record_failure(done_spec, 'publish', error)
        finishers.shutdown(wait=True)
        _align_progress(folder, totals, t_start, len(unaligned), log)
    return status


def _align_progress(folder, totals, t_start, total_chunks, log):
    elapsed = time.perf_counter() - t_start
    hours = totals['source_seconds'] / 3600
    append_jsonl(folder / 'progress.jsonl', dict(t=now(), kind='align', elapsed_seconds=elapsed,
                                                 aligned_source_hours=hours, **dict(totals)))
    remaining = total_chunks - totals['chunks']
    eta = elapsed / totals['chunks'] * remaining / 3600 if totals['chunks'] else float('nan')
    log(f'  aligned {totals["chunks"]:,}/{total_chunks:,} published chunks ({hours:,.1f} ch-h, '
        f'{hours * 3600 / max(elapsed, 1e-9):.0f}x); {totals["failed_contexts"]} failed contexts; ETA {eta:.2f} h')


# --------------------------------------------------------------------------- reporting

def coverage(store, settings, sources, windows=None):
    settings_id = store.resolve_settings(settings)
    rows = []
    for source in sources:
        window = windows.get(source.key, windows.get('*')) if windows else None
        specs = chunk_specs(source, settings, window)
        done = {receipt['index']: receipt for _, receipt in store.source_chunks(settings_id, source)}
        complete = [done[s.index] for s in specs if s.index in done]
        rows.append(dict(
            key=source.key, collection=source.collection, hours=sum(spec_hours(s) for s in specs),
            done_hours=sum(r['owned_seconds'] for r in complete) / 3600,
            retained_hours=sum(r['retained_seconds'] for r in complete) / 3600,
            chunks=len(specs), done_chunks=len(complete), words=sum(r['words'] for r in complete),
            aligned_chunks=sum(aligner_module.is_aligned(r) for r in complete),
            failed_contexts=sum((r.get('alignment') or {}).get('failed_contexts', 0) for r in complete
                                if aligner_module.is_aligned(r)),
            flagged_words=sum(sum(r['word_flags'].values()) for r in complete),
            superseded_identities=[i for i in store.identities(settings_id, source.key) if i != source.identity_id]))
    return rows


def coverage_from_ledgers(sessions, settings, sources, windows=None, settings_id=None):
    """Coverage from session ledgers, without opening chunk receipts (alignment is not counted)."""
    settings_id = settings_id or settings.settings_id
    done = {}
    for session in sessions:
        if session.get('kind') != 'first_pass' or session.get('settings_id') != settings_id:
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
                         aligned_chunks=None, failed_contexts=None,
                         flagged_words=sum(c[3] for c in complete), superseded_identities=[]))
    return rows


def throughput_model(sessions, kind='first_pass'):
    """Least-squares wall = a * source_hours + b * retained_hours over measured transcription groups."""
    rows = [p for s in sessions if s.get('kind') == kind for p in s['progress'] if 'source_hours' in p]
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
    progress = [p for p in session['progress'] if 'source_hours' in p]
    aligned = [p for p in session['progress'] if p.get('kind') == 'align']
    hours = sum(p['source_hours'] for p in progress)
    wall = session.get('wall_seconds') or (session['progress'][-1]['elapsed_seconds'] if session['progress'] else 0.0)
    retained = sum(p.get('retained_seconds', 0) for p in progress) / 3600
    return dict(session=session['session'], kind=session.get('kind'), status=session.get('status'),
                started=session.get('started'), hours=hours, wall_seconds=wall,
                aligned_hours=aligned[-1]['aligned_source_hours'] if aligned else 0.0,
                model_load_seconds=session.get('model_load_seconds', 0.0),
                rate=(hours + (aligned[-1]['aligned_source_hours'] if aligned else 0.0)) * 3600 / wall if wall else None,
                occupancy=retained / hours if hours else None,
                failures=len(session['failures']), groups=len(progress))


def projection(remaining_hours, occupancy, model=None):
    a = model['seconds_per_source_hour'] if model else PILOT_SECONDS_PER_SOURCE_HOUR
    b = model['seconds_per_retained_hour'] if model else PILOT_SECONDS_PER_RETAINED_HOUR
    seconds = remaining_hours * (a + b * occupancy)
    return dict(occupancy=occupancy, hours=seconds / 3600,
                rate=remaining_hours * 3600 / seconds if seconds else math.inf)
