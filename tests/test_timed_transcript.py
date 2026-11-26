"""Stage 07b carries the stage-06 transcript through the same timing map as the audio."""
import gzip
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import _bootstrap  # noqa: E402,F401
from sg_inputs import TranscriptStore  # noqa: E402
import timed_transcript as tt  # noqa: E402


class TimedTranscriptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.source = root/'defluttered'/'A15_T1'/'CH03'/'deflutter.wav'
        self.source.parent.mkdir(parents=True)
        self.source.write_bytes(b'')
        review = self.source.parent/'review.json'
        review.write_text(json.dumps(dict(status='complete', tape='A15_T1', channel=3)))
        digest = hashlib.sha256(review.read_bytes()).hexdigest()
        self.store = TranscriptStore('tape', root/'transcripts', root/'evidence', 'asr-test')
        identity = self.store.sources/'tape'/'A15_T1'/'CH03'/'id1'
        chunk = identity/'chunks'/'c00000'
        chunk.mkdir(parents=True)
        (identity/'source.json').write_text(json.dumps(dict(identity=dict(
            review_sha256=digest, sample_rate=8000, canonical_origin_s=10.0))))
        rows = [dict(sf=8000, ef=24000, text=' hello there', avg_logprob=-0.2, flags=['aligned'],
                     words=[dict(w=' hello', sf=8000, ef=16000, p=0.9, flags=[]),
                            dict(w=' there', sf=16000, ef=24000, p=0.8, flags=[])])]
        data = gzip.compress(''.join(json.dumps(r)+'\n' for r in rows).encode())
        (chunk/'segments.jsonl.gz').write_bytes(data)
        (chunk/'chunk.json').write_text(json.dumps(dict(status='complete', index=0,
                                                        segments_sha256=hashlib.sha256(data).hexdigest())))
        self.out = root/'retimed'/'A15_T1'/'CH03'
        self.out.mkdir(parents=True)
        # Output runs at half speed: local output = 2 x local input.
        np.savez_compressed(self.out/'timing_map.npz', input_seconds=np.array([0., 100.]),
                            output_seconds=np.array([0., 200.]), recorded_speed_ratio=np.array([.5, .5]))
        plan = root/'plan.json'
        plan.write_text(json.dumps(dict(get_placement=dict(rule='GET = output + c', segments=[
            dict(segment=0, tape_start_s=0., tape_end_s=1e6, get_equals_output_plus_s=1000.)]))))
        (self.out/'retime.json').write_text(json.dumps(dict(status='complete', shared_plan=dict(
            path=str(plan), output_start_seconds=50.))))

    def tearDown(self):
        self.tmp.cleanup()

    def rows(self):
        lines = (self.out/'timed_transcript.tsv').read_text(encoding='utf-8').splitlines()
        header = lines[0].split('\t')
        return [dict(zip(header, line.split('\t'))) for line in lines[1:]]

    def test_words_follow_the_audio_map_tape_clock_and_get(self):
        record = tt.write_timed_transcript(self.out, self.source, progress=lambda _: None, store=self.store)
        self.assertEqual(record['counts'], dict(segments=1, words=2))
        rows = self.rows()
        self.assertEqual([r['kind'] for r in rows], ['segment', 'word', 'word'])
        word = rows[2]
        self.assertEqual(word['text'], 'there')
        self.assertAlmostEqual(float(word['source_start_s']), 2.0)
        self.assertAlmostEqual(float(word['start_s']), 4.0)
        self.assertAlmostEqual(float(word['tape_start_s']), 54.0)
        self.assertAlmostEqual(float(word['get_start_s']), 1054.0)
        self.assertFalse(record['aligned'], 'chunk receipts without an alignment record')

    def test_completed_output_is_reused_and_never_rewritten(self):
        tt.write_timed_transcript(self.out, self.source, progress=lambda _: None, store=self.store)
        stamp = (self.out/'timed_transcript.tsv').stat().st_mtime_ns
        tt.write_timed_transcript(self.out, self.source, progress=lambda _: None, store=self.store)
        self.assertEqual((self.out/'timed_transcript.tsv').stat().st_mtime_ns, stamp)
        with (self.out/'timing_map.npz').open('ab') as f:
            f.write(b'changed')
        with self.assertRaisesRegex(ValueError, 'inputs changed'):
            tt.write_timed_transcript(self.out, self.source, progress=lambda _: None, store=self.store)

    def test_channel_without_a_transcript_is_skipped(self):
        other = TranscriptStore('tape', Path(self.tmp.name)/'none', Path(self.tmp.name)/'none', 'asr-test')
        self.assertIsNone(tt.write_timed_transcript(self.out, self.source, progress=lambda _: None, store=other))
        self.assertFalse((self.out/'timed_transcript.tsv').exists())


if __name__ == '__main__':
    unittest.main()
