"""Benchmark word timelines stay within the input of their decoder context."""

from types import SimpleNamespace
import unittest

import numpy as np

from research.mission_timing.benchmark_asr import ASR_RATE, pack, restore


def clip(source_id, origin, *windows):
    ranges = [{"start": round(left * ASR_RATE), "end": round(right * ASR_RATE)}
              for left, right in windows]
    return source_id, origin, np.zeros(8 * ASR_RATE, dtype=np.float32), ranges


def word(text, start, end):
    return SimpleNamespace(word=text, start=start, end=end, probability=0.8)


def segment(seek, *words):
    return SimpleNamespace(seek=seek, words=words, avg_logprob=-0.2, no_speech_prob=0.1,
                           compression_ratio=1.0)


class AsrTimelineTests(unittest.TestCase):
    def test_restore_vad_gap_and_keep_decoder_ownership(self):
        _, spans, contexts = pack([
            clip("CH01", 100, (1, 2), (6, 7)),
            clip("CH02", 200, (5, 6)),
        ])
        self.assertEqual(contexts, [(0, 2 * ASR_RATE), (2 * ASR_RATE, 3 * ASR_RATE)])
        rows = list(restore([
            segment(0, word(" first", 0.2, 0.4), word(" second", 1.7, 2.1), word(" off", 2.2, 2.5)),
            segment(200, word(" third", 1.9, 2.3), word(" inverted", 2.7, 2.4)),
        ], spans, contexts))
        self.assertEqual([(row["source_id"], row["text"], row["start"], row["end"]) for row in rows], [
            ("CH01", " first", 101.2, 101.4),
            ("CH01", " second", 106.7, 107.0),
            ("CH02", " third", 205.0, 205.3),
        ])
        self.assertEqual([row["words"][0]["timing_clipped"] for row in rows], [False, True, True])

    def test_short_gap_keeps_contiguous_samples_and_word_endpoints(self):
        _, spans, contexts = pack([clip("CH01", 100, (1, 2), (2.5, 3.5), (6, 7))],
                                  max_gap_samples=ASR_RATE)
        self.assertEqual(contexts, [(0, 40000), (40000, 56000)])
        row, = restore([segment(0, word(" boundary", 0.9, 1.4))], spans, contexts)
        self.assertEqual((row["start"], row["end"]), (101.9, 102.4))
        self.assertFalse(row["words"][0]["timing_clipped"])


if __name__ == "__main__":
    unittest.main()
