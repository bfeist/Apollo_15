"""Timing transcript: ownership, attribution, alignment, resume and invalidation."""
import gzip
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import soundfile as sf

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from _bootstrap import install as _install
_install()

import mission_asr as asr
from mission_sources import Reader, tape_sources, wav_layout, decode_pcm
from mission_store import Store
import transcribe_run as runner
import mission_align as align

RATE = 8000


def tone_bursts(seconds, seed, period=7.0, burst=2.5):
    rng = np.random.default_rng(seed)
    t = np.arange(int(seconds * RATE)) / RATE
    on = (t % period) < burst
    return (0.2 * np.sin(2 * np.pi * 600 * t) * on + rng.normal(0, 1e-3, len(t))).astype(np.float32)


def make_tape(root, tape='A15_T999', channels=(1, 2), seconds=200.0, origin=0.9785):
    for channel in channels:
        folder = root / 'defluttered_shared' / tape / f'CH{channel:02d}'
        folder.mkdir(parents=True, exist_ok=True)
        wav = folder / 'deflutter.wav'
        sf.write(wav, tone_bursts(seconds, channel), RATE, subtype='FLOAT')
        digest = hashlib.sha256(wav.read_bytes()).hexdigest()
        frames = sf.info(wav).frames
        (folder / 'review.json').write_text(json.dumps(dict(
            status='complete', tape=tape, channel=channel, start_seconds=origin, map_sha256='m' * 64,
            source=dict(path=f'{tape}_CH{channel}.wav', start_seconds=origin, acquisition_start_seconds=0),
            files={'deflutter.wav': dict(sha256=digest, frames=frames, sample_rate=RATE)})))


def energy_detector(audio16, settings):
    """Deterministic stand-in for Silero: contiguous runs above an energy floor."""
    frame = 320
    count = len(audio16) // frame
    loud = np.sqrt(np.mean(audio16[:count * frame].reshape(count, frame) ** 2, axis=1)) > 0.02
    windows, start = [], None
    for i, value in enumerate(np.r_[loud, False]):
        if value and start is None:
            start = i
        elif not value and start is not None:
            windows.append((start * frame, i * frame))
            start = None
    return windows


class FakeRecognizer:
    """Emit one word at the start of every retained window in every context."""
    load_seconds = 0.0

    def __init__(self, settings=None, interrupt_on_call=None):
        self.calls = 0
        self.interrupt_on_call = interrupt_on_call

    def __call__(self, audio, contexts):
        self.calls += 1
        if self.interrupt_on_call == self.calls:
            raise KeyboardInterrupt
        out = []
        for begin, end, _ in contexts:
            start = begin / asr.ASR_RATE
            stop = min(end, begin + 3200) / asr.ASR_RATE
            word = SimpleNamespace(word=f' w{begin}', start=start, end=stop, probability=0.9)
            out.append(SimpleNamespace(seek=int(begin / asr.ASR_RATE * 100), start=start, end=stop, text=word.word,
                                       words=[word], avg_logprob=-0.1, no_speech_prob=0.0, compression_ratio=1.0))
        return out


SETTINGS = asr.Settings(chunk_seconds=60.0, cut_search_s=3.0)


def all_words(store, sources):
    words = []
    for source in sources:
        for folder, receipt in store.source_chunks(SETTINGS.settings_id, source):
            for row in store.chunk_rows(folder):
                for word in row['words']:
                    words.append((source.key, word['sf'], word['ef']))
    return sorted(words)


class OwnershipTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        make_tape(self.root, seconds=250.0)
        self.sources = tape_sources(self.root)

    def test_review_supplies_canonical_origin_and_checksum(self):
        source = self.sources[0]
        self.assertEqual(source.canonical_origin_s, 0.9785)
        self.assertEqual(source.review['acquisition_start_seconds'], 0)
        self.assertEqual(source.content_sha256, hashlib.sha256(Path(source.path).read_bytes()).hexdigest())
        self.assertEqual(source.relpath, 'defluttered_shared/A15_T999/CH01/deflutter.wav')

    def test_cuts_are_shared_by_neighbours_and_partition_the_source(self):
        source = self.sources[0]
        specs = asr.chunk_specs(source, SETTINGS)
        self.assertGreater(len(specs), 2)
        reader = Reader(source)
        owned = []
        for spec in specs:
            # Separate caches: each chunk computes its boundaries independently.
            left = 0 if spec.nominal_start == 0 else asr.cut_at(reader, spec.nominal_start, SETTINGS, {})
            right = source.frames if spec.nominal_end == source.frames else asr.cut_at(reader, spec.nominal_end, SETTINGS, {})
            owned.append((left, right))
        self.assertEqual(owned[0][0], 0)
        self.assertEqual(owned[-1][1], source.frames)
        for (a, b), (c, d) in zip(owned, owned[1:]):
            self.assertEqual(b, c)
            self.assertLess(a, b)
        # The cut lands in a quiet gap between bursts, not inside speech.
        audio, _ = reader.read(0, source.frames)
        for _, b in owned[:-1]:
            self.assertLess(abs(audio[b]), 0.02)

    def test_window_selects_whole_grid_chunks_in_canonical_seconds(self):
        source = self.sources[0]
        chosen = asr.chunk_specs(source, SETTINGS, (61.0, 119.0))
        self.assertEqual([s.index for s in chosen], [1])
        self.assertEqual(asr.chunk_specs(source, SETTINGS, (1.0, 1.5)), asr.chunk_specs(source, SETTINGS)[:1])
        self.assertEqual(asr.chunk_specs(source, SETTINGS, (0.5, 0.9)), [], "window before the canonical origin")

    def test_pcm24_reader_matches_soundfile(self):
        path = self.root / 'pcm24.wav'
        data = np.stack([tone_bursts(2, 1), tone_bursts(2, 2)], axis=1) * 0.5
        sf.write(path, data, 48000, subtype='PCM_24')
        layout = wav_layout(path)
        with open(path, 'rb') as f:
            f.seek(layout.data_offset)
            raw = f.read(layout.data_bytes)
        expected, _ = sf.read(path, dtype='float32')
        np.testing.assert_allclose(decode_pcm(raw, layout), expected, atol=1e-7)


class RestoreTests(unittest.TestCase):
    def prepared(self, origin, windows):
        audio = np.zeros(20 * asr.ASR_RATE, np.float32)
        spec = SimpleNamespace(source=SimpleNamespace(sample_rate=RATE))
        item = asr.Prepared(spec, 0, 0, '', audio, origin, [(a * asr.ASR_RATE, b * asr.ASR_RATE, []) for a, b in windows], 0, 0)
        item.contexts = asr.pack_contexts(item.windows, SETTINGS)
        return item

    def segment(self, seek, *words):
        return SimpleNamespace(seek=seek, start=words[0].start, end=words[-1].end, text=''.join(w.word for w in words),
                               words=list(words), avg_logprob=-0.2, no_speech_prob=0.1, compression_ratio=1.0)

    def word(self, text, start, end):
        return SimpleNamespace(word=text, start=start, end=end, probability=0.8)

    def test_each_source_keeps_its_words_and_gap_endpoints_are_flagged(self):
        first = self.prepared(1000 * RATE, [(1, 2), (6, 7)])
        second = self.prepared(5000 * RATE, [(5, 6)])
        audio, spans, contexts = asr.pack_group([first, second])
        self.assertEqual([(a, b, p) for a, b, p in contexts], [(0, 32000, 0), (32000, 48000, 1)])
        rows = asr.restore([
            self.segment(0, self.word(' first', 0.2, 0.4), self.word(' across', 0.9, 1.2), self.word(' edge', 1.5, 2.0),
                         self.word(' late', 1.9, 2.4)),
            self.segment(200, self.word(' other', 2.1, 2.3), self.word(' backwards', 2.7, 2.4),
                         self.word(' none', float('nan'), 2.9)),
        ], spans, contexts, [first, second])
        words = {w['w']: w for w in rows[0][0]['words']}
        self.assertEqual((words[' first']['sf'], words[' first']['ef']), (1001.2 * RATE, 1001.4 * RATE))
        # Crosses the gap between retained windows: start in window 1, end in window 2.
        self.assertEqual((words[' across']['sf'], words[' across']['ef']), (1001.9 * RATE, 1006.2 * RATE))
        self.assertIn('spans_vad_gap', words[' across']['flags'])
        self.assertIn('long', words[' across']['flags'])
        # An end exactly at the context end stays in the last window.
        self.assertEqual(words[' edge']['ef'], 1007 * RATE)
        self.assertIn('clipped', words[' late']['flags'])
        other = {w['w']: w for w in rows[1][0]['words']}
        self.assertEqual(other[' other']['sf'], 5005.1 * RATE)
        self.assertIn('invalid_time', other[' backwards']['flags'])
        self.assertIn('zero_duration', other[' backwards']['flags'])
        self.assertIsNone(other[' none']['sf'])
        self.assertIn('invalid_time', other[' none']['flags'])
        self.assertEqual(len(rows[1][0]['words']), 3, 'questionable words are preserved, not dropped')


class ResumeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        make_tape(self.root, channels=(1, 2, 3), seconds=250.0)
        self.sources = tape_sources(self.root)

    def store(self, name):
        return Store(self.root / name / 't', self.root / name / 'e')

    def run_once(self, store, recognizer, **kwargs):
        return runner.run(store, SETTINGS, self.sources, recognizer_factory=lambda s: recognizer,
                          aligner_factory=FakeAligner, group_hours=60 / 3600 * 3, prep_workers=2, log=lambda m: None, detector=energy_detector, **kwargs)

    def test_interrupted_run_resumes_without_lost_or_duplicated_words(self):
        reference = self.store('reference')
        self.run_once(reference, FakeRecognizer())
        expected = all_words(reference, self.sources)
        self.assertGreater(len(expected), 10)
        store = self.store('interrupted')
        with self.assertRaises(KeyboardInterrupt):
            self.run_once(store, FakeRecognizer(interrupt_on_call=2))
        partial = all_words(store, self.sources)
        self.assertTrue(0 < len(partial) < len(expected))
        self.assertFalse(list((self.root / 'interrupted').rglob('.tmp-*')))
        session = store.sessions()[-1]
        self.assertEqual(session['status'], 'interrupted')
        self.run_once(store, FakeRecognizer())
        self.assertEqual(all_words(store, self.sources), expected)
        # A third run reuses everything and decodes nothing.
        recognizer = FakeRecognizer()
        self.run_once(store, recognizer)
        self.assertEqual(recognizer.calls, 0)
        self.assertEqual(len(set(expected)), len(expected))

    def test_stale_temporary_is_discarded_and_lock_released(self):
        store = self.store('stale')
        source = self.sources[0]
        stale = store.chunk_dir(SETTINGS.settings_id, source, 0).parent / '.tmp-c00000-dead'
        stale.mkdir(parents=True)
        (stale / 'segments.jsonl.gz').write_bytes(b'partial')
        self.run_once(store, FakeRecognizer())
        self.assertFalse(stale.exists())
        self.assertFalse((store.evidence / 'transcribe.lock').exists())

    def test_source_change_invalidates_dependent_chunks(self):
        store = self.store('changed')
        self.run_once(store, FakeRecognizer())
        before = {s.key: s.identity_id for s in self.sources}
        # Publish a new 05b edition of CH02 with a different origin.
        review_path = self.root / 'defluttered_shared/A15_T999/CH02/review.json'
        review = json.loads(review_path.read_text())
        review['start_seconds'] = 1.5
        review_path.write_text(json.dumps(review))
        self.sources = tape_sources(self.root)
        changed = next(s for s in self.sources if s.key.endswith('CH02'))
        self.assertNotEqual(changed.identity_id, before[changed.key])
        pending, reused = runner.plan(store, SETTINGS, self.sources)
        self.assertEqual({s.source.key for s in pending}, {changed.key})
        self.assertEqual(len({s.source.key for s, _ in reused}), 2)
        rows = runner.coverage(store, SETTINGS, self.sources)
        row = next(r for r in rows if r['key'] == changed.key)
        self.assertEqual(row['done_hours'], 0)
        self.assertEqual(row['superseded_identities'], [before[changed.key]])

    def test_receipts_bind_owned_samples_and_ownership(self):
        store = self.store('receipts')
        self.run_once(store, FakeRecognizer())
        source = self.sources[0]
        chunks = store.source_chunks(SETTINGS.settings_id, source)
        reader = Reader(source)
        for (_, a), (_, b) in zip(chunks, chunks[1:]):
            self.assertEqual(a['cut_frames'][1], b['cut_frames'][0])
        folder, receipt = chunks[1]
        _, checksum = reader.read(*receipt['cut_frames'])
        self.assertEqual(receipt['owned_sha256'], checksum)
        self.assertEqual(hashlib.sha256((folder / 'segments.jsonl.gz').read_bytes()).hexdigest(), receipt['segments_sha256'])
        for row in store.chunk_rows(folder):
            for word in row['words']:
                self.assertTrue(receipt['cut_frames'][0] <= word['sf'] <= receipt['cut_frames'][1])
        model = runner.throughput_model(store.sessions())
        self.assertIsNotNone(model)
        # Status sums the session ledgers; they must agree with the receipts on disk.
        fast = runner.coverage_from_ledgers(store.sessions(), SETTINGS, self.sources)
        slow = runner.coverage(store, SETTINGS, self.sources)
        for a, b in zip(fast, slow):
            self.assertEqual((a['done_chunks'], a['words']), (b['done_chunks'], b['words']))
            self.assertAlmostEqual(a['done_hours'], b['done_hours'])


class WatchTests(unittest.TestCase):
    setUp, store, run_once = ResumeTests.setUp, ResumeTests.store, ResumeTests.run_once

    def test_watcher_tails_groups_and_paces_their_segments(self):
        import io
        from rich.console import Console
        import transcribe_watch as watch
        store = self.store('watch')
        self.run_once(store, FakeRecognizer())
        folder = watch.latest_session(store.evidence)
        tail = watch.ProgressTail(folder)
        rows = tail.poll()
        self.assertGreater(len(rows), 1)
        self.assertEqual(tail.poll(), [], 'consumed lines are not reread')
        record = json.loads((folder / 'session.json').read_text(encoding='utf-8'))
        segments = [s for row in rows for s in watch.group_segments(store.root, record['settings_id'], row['ledger'])]
        published = sum(len(store.chunk_rows(f)) for s in self.sources for f, _ in store.source_chunks(SETTINGS.settings_id, s))
        self.assertEqual(len(segments), published)
        first = watch.group_segments(store.root, record['settings_id'], rows[0]['ledger'])
        self.assertEqual([s['t'] for s in first], sorted(s['t'] for s in first))
        self.assertGreaterEqual(min(s['t'] for s in first), self.sources[0].canonical_origin_s)
        only = watch.group_segments(store.root, record['settings_id'], rows[0]['ledger'], channels=[2])
        self.assertTrue(only and all(s['key'].endswith('/CH02') for s in only))

        feed = watch.Feed(max_per_second=0)
        feed.schedule(first, 10.0, now=0.0)
        self.assertEqual(feed.tick(now=0.0), 1)
        feed.tick(now=5.0)
        self.assertTrue(0 < feed.backlog < len(first))
        feed.tick(now=10.0)
        self.assertEqual((feed.backlog, feed.shown), (0, len(first)))
        capped = watch.Feed(max_per_second=0.2)
        capped.schedule(first, 10.0, now=0.0)
        capped.tick(now=10.0)
        self.assertEqual((capped.shown, capped.sampled_out), (2, len(first) - 2))

        state = watch.State()
        state.folder, state.record, state.rows = folder, record, rows
        console = Console(file=io.StringIO(), width=120, height=24, force_terminal=False)
        console.print(watch.dashboard(state, feed), height=24)
        text = console.file.getvalue()
        self.assertIn('complete', text)
        self.assertIn('T999 CH0', text)


class FakeAligner:
    """Stand-in for the CTC model: one 20 ms frame per token from the start of each context."""
    load_seconds = 0.0

    def __init__(self):
        self.calls = 0

    def emissions(self, audio):
        self.calls += 1
        return np.zeros((max(1, len(audio) // 320), len(align.LABELS)), np.float32)

    def path(self, log_probs, tokens):
        frames = log_probs.shape[0]
        if not tokens or frames < len(tokens):
            return None
        return [(i, i + 1, 0.5) for i in range(len(tokens))]


class AlignTests(unittest.TestCase):
    setUp, store = ResumeTests.setUp, ResumeTests.store

    def run_once(self, store, recognizer=None, aligner=None, settings=SETTINGS, **kwargs):
        return runner.run(store, settings, self.sources, recognizer_factory=lambda s: recognizer or FakeRecognizer(),
                          aligner_factory=lambda: aligner or FakeAligner(), group_hours=60 / 3600 * 3, prep_workers=2,
                          log=lambda m: None, detector=energy_detector, **kwargs)

    def test_spelling_for_the_model(self):
        self.assertEqual(align.spell('P20'), ['P', 'TWENTY'])
        self.assertEqual(align.spell('213'), ['TWO', 'ONE', 'THREE'])
        self.assertEqual(align.spell('5.0,'), ['FIVE', 'POINT', 'ZERO'])
        self.assertEqual(align.spell('19,656'), ['ONE', 'NINE', 'SIX', 'FIVE', 'SIX'])
        self.assertEqual(align.spell("it\u2019s,"), ["IT'S"])
        self.assertEqual(align.spell('...'), [])
        tokens, owner = align.tokens_of([dict(w=' Go'), dict(w='...'), dict(w=' 08')])
        self.assertEqual(''.join(align.LABELS[t] for t in tokens), 'GO|EIGHT')
        self.assertEqual(owner, [0, 0, None, 2, 2, 2, 2, 2])

    def test_run_publishes_aligned_words_and_keeps_whisper_times(self):
        store = self.store('aligned')
        self.run_once(store)
        receipts = [r for s in self.sources for _, r in store.source_chunks(SETTINGS.settings_id, s)]
        self.assertTrue(receipts and all(align.is_aligned(r) for r in receipts))
        self.assertTrue(all(r['alignment']['failed_contexts'] == 0 for r in receipts))
        for s in self.sources:
            for folder, receipt in store.source_chunks(SETTINGS.settings_id, s):
                lo, hi = receipt['cut_frames']
                for row in store.chunk_rows(folder):
                    self.assertIn('aligned', row['flags'])
                    for w in row['words']:
                        self.assertIn('wsf', w)
                        self.assertTrue(lo <= w['sf'] < w['ef'] <= hi)
                        # Each word lies inside one retained window.
                        self.assertTrue(any(a <= w['sf'] and w['ef'] <= b for a, b, _ in receipt['windows']))

    def test_published_chunks_are_aligned_in_place_without_whisper(self):
        store = self.store('legacy')
        real = align.align_id
        align.align_id = lambda: 'align-older'
        try:
            self.run_once(store)
        finally:
            align.align_id = real
        before = {(s.key, r['index']): (r['segments_sha256'], [w['sf'] for row in store.chunk_rows(f)
                                                                for w in row['words']])
                  for s in self.sources for f, r in store.source_chunks(SETTINGS.settings_id, s)}
        recognizer, aligner = FakeRecognizer(), FakeAligner()
        self.run_once(store, recognizer, aligner)
        self.assertEqual(recognizer.calls, 0, 'Whisper is not run again')
        self.assertGreater(aligner.calls, 0)
        session = store.sessions()[-1]
        self.assertEqual((session['status'], session['pending_chunks'], session['unaligned_chunks']),
                         ('complete', 0, len(before)))
        for s in self.sources:
            for folder, receipt in store.source_chunks(SETTINGS.settings_id, s):
                old_sha, old_starts = before[s.key, receipt['index']]
                self.assertTrue(align.is_aligned(receipt))
                self.assertEqual(receipt['alignment']['whisper_segments_sha256'], old_sha)
                words = [w for row in store.chunk_rows(folder) for w in row['words']]
                self.assertEqual(sorted(w['wsf'] for w in words), sorted(old_starts))
        self.assertFalse(list(store.root.rglob('.old-*')) + list(store.root.rglob('.tmp-*')))
        # Nothing left to do.
        aligner = FakeAligner()
        self.run_once(store, FakeRecognizer(), aligner)
        self.assertEqual(aligner.calls, 0)

    def test_stopped_alignment_resumes(self):
        store = self.store('stop')
        real = align.align_id
        align.align_id = lambda: 'align-older'
        try:
            self.run_once(store)
        finally:
            align.align_id = real
        (store.evidence / 'STOP').write_text('x')
        self.run_once(store)
        stopped = store.sessions()[-1]
        self.assertEqual(stopped['status'], 'stopped')
        aligned = sum(align.is_aligned(r) for s in self.sources for _, r in store.source_chunks(SETTINGS.settings_id, s))
        self.assertLess(aligned, stopped['unaligned_chunks'])
        (store.evidence / 'STOP').unlink()
        self.run_once(store)
        self.assertEqual(store.sessions()[-1]['unaligned_chunks'], stopped['unaligned_chunks'] - aligned)
        self.assertTrue(all(align.is_aligned(r) for s in self.sources for _, r in store.source_chunks(SETTINGS.settings_id, s)))

    def test_interrupted_replacement_is_restored(self):
        store = self.store('replace')
        self.run_once(store)
        source = self.sources[0]
        final = store.chunk_dir(SETTINGS.settings_id, source, 0)
        old = final.parent / '.old-c00000-dead'
        final.rename(old)
        store.clean_temporaries(SETTINGS.settings_id)
        self.assertTrue((final / 'chunk.json').is_file())
        self.assertFalse(old.exists())

    def test_store_is_found_whatever_the_batch_size(self):
        store = self.store('batch')
        self.run_once(store)
        other = asr.Settings(chunk_seconds=60.0, cut_search_s=3.0, batch_size=4)
        self.assertEqual(store.resolve_settings(other), SETTINGS.settings_id)
        self.assertEqual(other.settings_id, SETTINGS.settings_id, 'batch size is not part of a new identity')
        recognizer = FakeRecognizer()
        self.run_once(store, recognizer, settings=other)
        self.assertEqual(recognizer.calls, 0)
        # A store made under an identity that still included the batch size is found by its settings.
        legacy = store.root / 'asr-legacy000000'
        store.settings_dir(SETTINGS.settings_id).rename(legacy)
        saved = json.loads((legacy / 'settings.json').read_text())
        (legacy / 'settings.json').write_text(json.dumps(dict(saved, settings_id='asr-legacy000000')))
        self.assertEqual(store.resolve_settings(other), 'asr-legacy000000')

    def test_reader_reports_alignment_state(self):
        import transcript_read
        store = self.store('reader')
        real = align.align_id
        align.align_id = lambda: 'align-older'
        try:
            self.run_once(store)
        finally:
            align.align_id = real
        identity = store.source_dir(SETTINGS.settings_id, self.sources[0])
        before = transcript_read.identity_state(identity)
        self.assertEqual(before['aligned_chunks'], 0)
        self.run_once(store)
        after = transcript_read.identity_state(identity)
        self.assertEqual(after['aligned_chunks'], after['chunks'])
        self.assertNotEqual(after['sha256'], before['sha256'])
        rows = transcript_read.identity_rows(identity)
        self.assertEqual([r['sf'] for r in rows], sorted(r['sf'] for r in rows))


class PipelineEntryTests(unittest.TestCase):
    def test_stage_06_is_one_resumable_job_before_retime(self):
        import pipeline
        numbers = [stage[0] for stage in pipeline.STAGES]
        self.assertEqual(numbers[numbers.index('05b'):], ['05b', '06', '07a', '07b', '08'])
        args, selected = pipeline.parse_args(['plan', '--stages', '06', '--tape', 'A15_T510093', '--channel', '18',
                                              '--transcribe-start', '37000'])
        job, = pipeline.make_jobs(args, selected, Path('unused'))
        self.assertEqual(job['script'], '06_transcribe.py')
        command = job['command']
        self.assertEqual(command[command.index('06_transcribe.py') + 1], 'run')
        for expected in ('--tape=A15_T510093', '--start'):
            self.assertIn(expected, command)
        args, selected = pipeline.parse_args(['plan', '--stages', '06', '--all-tapes'])
        job, = pipeline.make_jobs(args, selected, Path('unused'))
        self.assertIn('--all-tapes', job['command'])

    def test_stage_07a_is_one_job_over_the_selected_tapes(self):
        import pipeline
        args, selected = pipeline.parse_args(['plan', '--stages', '07a', '--all-tapes'])
        job, = pipeline.make_jobs(args, selected, Path('unused'))
        command = job['command']
        self.assertEqual(command[command.index('07a_sg_retime.py') + 1:], ['run', '--all-tapes'])
        with self.assertRaises(SystemExit):
            pipeline.parse_args(['plan', '--stages', '07a', '--tape', 'A15_T510093', '--channel', '3'])

    def test_stage_06_rejects_mixed_or_empty_selection(self):
        import pipeline
        for argv in (['plan', '--stages', '06,07b', '--tape', 'A15_T510093'], ['plan', '--stages', '06'],
                     ['plan', '--stages', '06,07a', '--all-tapes']):
            with self.subTest(argv=argv), self.assertRaises(SystemExit):
                pipeline.parse_args(argv)


if __name__ == '__main__':
    unittest.main()
