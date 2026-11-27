"""Journal anchors (sg_journal.py): row matching and outlier rule."""
import sys
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'scripts'), str(ROOT / 'scripts/30-track/07_retime')]
import _bootstrap  # noqa: E402
_bootstrap.install()
import sg_journal as sj  # noqa: E402


def row(t, text):
    return dict(t=t, tokens=text.split(), event_id=f'r{t}', source='TEC')


class MatchTests(unittest.TestCase):
    def setUp(self):
        words = 'roger houston we copy the burn looks good on our side over and out'.split()
        self.tok = np.array(['noise'] * 10 + words + ['noise'] * 10)
        self.get = 1000.0 + np.arange(len(self.tok)) * 0.4

    def test_a_row_heard_once_is_matched_at_its_first_word(self):
        rows = [row(1004.0, 'roger houston we copy the burn looks good on our side')]
        self.assertEqual(sj.match_channel(self.tok, self.get, rows, 60.0), [(0, 10)])

    def test_a_row_outside_the_window_or_heard_twice_is_not_matched(self):
        far = [row(2000.0, 'roger houston we copy the burn looks good')]
        self.assertEqual(sj.match_channel(self.tok, self.get, far, 60.0), [])
        tok = np.r_[self.tok, self.tok]
        get = 1000.0 + np.arange(len(tok)) * 0.4
        self.assertEqual(sj.match_channel(tok, get, [row(1004.0, 'roger houston we copy the burn')], 60.0), [])

    def test_following_words_must_agree(self):
        rows = [row(1004.0, 'roger houston we copy something else entirely different words here now')]
        self.assertEqual(sj.match_channel(self.tok, self.get, rows, 60.0), [])

    def test_an_offset_far_from_its_neighbours_is_dropped(self):
        t = np.arange(40) * 60.0
        d = 0.3 + 0.01 * np.arange(40)
        d[17] = 62.0  # a minute misprint
        keep = sj.drop_outliers(t, d)
        self.assertFalse(keep[17])
        self.assertTrue(keep[np.arange(40) != 17].all())


if __name__ == '__main__':
    unittest.main()
