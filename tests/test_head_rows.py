"""Head-row placement (07b), NARA voice-row pieces, staged copies and stage-10 row evidence (docs/23)."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'scripts'), str(ROOT / 'scripts/30-track/07_retime'), str(ROOT / 'scripts/30-track/10_channelid'),
                str(ROOT / 'scripts/00_common')]
import _bootstrap  # noqa: E402,F401
_bootstrap.install()
import head_rows  # noqa: E402
import sg_stage  # noqa: E402
import staged_io  # noqa: E402
from sg_inputs import voice_pieces  # noqa: E402
import channelid_rows  # noqa: E402


def write_anchors(root, tape, rows, groups=(0.0, 1.36)):
    ed = Path(root) / tape / 'sg_anchors' / 'anchors-test'
    ed.mkdir(parents=True)
    (ed / 'manifest.json').write_text(json.dumps(dict(status='complete', version=sg_stage.VERSION, result='curve',
                                                      stagger_groups_s=list(groups))))
    head = 'channel\tinterval\tstart_s\tend_s\tpairs\tstagger_s\tmad_s\ttape_level_s\tgroup\tmeasured\tmixed'
    lines = [head] + [f'{c}\t{k}\t{k * 1800}\t{(k + 1) * 1800}\t50\t{s}\t0.01\t{s}\t0\t{m}\t{x}' for c, k, s, m, x in rows]
    (ed / 'stagger.tsv').write_text('\n'.join(lines) + '\n')


class HeadRowTests(unittest.TestCase):
    def plan(self, rows, channels, duration=6 * 1800.0, tc=None):
        with tempfile.TemporaryDirectory() as tmp:
            write_anchors(tmp, 'T', rows)
            return head_rows.row_plan('T', channels, duration, timecode_channel=tc, root=Path(tmp),
                                      maps_root=Path(tmp) / 'none')

    def test_later_row_is_moved_earlier_by_its_own_stagger(self):
        rows = [(c, k, 1.36 if c % 2 else 0.0, 'True', 'False') for c in (1, 2) for k in range(6)]
        p = self.plan(rows, [1, 2])
        self.assertEqual(p['channels']['1']['rows'], 'LLLLLL')
        self.assertEqual(p['channels']['2']['rows'], 'EEEEEE')
        self.assertAlmostEqual(float(head_rows.shift_at(p['channels']['1']['knots'], 5000.0)), 1.36)
        self.assertEqual(float(head_rows.shift_at(p['channels']['2']['knots'], 5000.0)), 0.0)

    def test_row_change_along_the_tape_ramps_between_the_runs(self):
        rows = [(1, k, 1.36 if k < 3 else 0.0, 'True', 'False') for k in range(6)]
        p = self.plan(rows, [1])
        knots = p['channels']['1']['knots']
        self.assertAlmostEqual(float(head_rows.shift_at(knots, 1000.0)), 1.36)
        self.assertAlmostEqual(float(head_rows.shift_at(knots, 5400.0)), 0.68, places=2)   # mid-ramp at the boundary
        self.assertEqual(float(head_rows.shift_at(knots, 9000.0)), 0.0)

    def test_isolated_interval_against_long_runs_is_an_outlier(self):
        rows = [(1, k, 0.0 if k == 3 else 1.36, 'True', 'False') for k in range(7)]
        p = self.plan(rows, [1], duration=7 * 1800.0)
        self.assertEqual(p['channels']['1']['rows'], 'LLLLLLL')

    def test_unmeasured_channel_between_equal_rows_takes_the_other_row_and_timecode_is_earlier(self):
        rows = [(c, k, 1.36, 'True', 'False') for c in (1, 3) for k in range(6)]
        p = self.plan(rows, [1, 2, 3, 4], tc=4)
        self.assertEqual(p['channels']['2']['rows'], 'EEEEEE')
        self.assertEqual(set(p['channels']['2']['basis']), {'neighbours'})
        self.assertEqual(p['channels']['4']['rows'], 'EEEEEE')

    def test_shifted_map_is_the_tape_map_at_the_earlier_time(self):
        tt = np.arange(0, 1000.1, 0.1)
        oo, ss = tt * 0.98, np.full(len(tt), 0.98)
        knots = [[0.0, 1.36], [1000.0, 1.36]]
        o, speed, s = head_rows.shifted_local_map(np.array([0.0, 500.0]), 100.0, knots, tt, oo, ss)
        np.testing.assert_allclose(o, (np.array([100.0, 600.0]) - 1.36) * 0.98)
        np.testing.assert_allclose(s, 1.36)


class NaraVoiceTests(unittest.TestCase):
    def test_interval_overrides_split_the_segment(self):
        voice = dict(voice_late_s=0.683, intervals=[dict(get_from_s=1100.0, get_to_s=1200.0, voice_late_s=0.0)])
        self.assertEqual(voice_pieces(voice, 0.0, 500.0, 1000.0), [(0.0, 100.0, 0.683), (100.0, 200.0, 0.0), (200.0, 500.0, 0.683)])
        self.assertEqual(voice_pieces(dict(voice_late_s=None), 0.0, 10.0, 0.0), [(0.0, 10.0, 0.0)])


class StagedCopyTests(unittest.TestCase):
    def test_copy_verifies_and_keeps_mtime_and_lock_is_exclusive(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / 'a.bin'
            src.write_bytes(b'x' * 100000)
            dst = Path(tmp) / 'out' / 'a.bin'
            staged_io.copy_verified(src, dst)
            self.assertEqual(dst.read_bytes(), src.read_bytes())
            self.assertEqual(dst.stat().st_mtime_ns, src.stat().st_mtime_ns)
            with staged_io.DiskLock(Path(tmp)):
                self.assertTrue((Path(tmp) / '.archive_io.lock').is_file())
            self.assertFalse((Path(tmp) / '.archive_io.lock').exists())


class StageTenRowTests(unittest.TestCase):
    def test_measured_row_penalizes_the_wrong_parity(self):
        lp = channelid_rows.row_logp(dict(row='E', share=1.0, intervals=8, changes=False))
        self.assertEqual(lp[1], 0.0)
        self.assertEqual(lp[3], 0.0)
        self.assertLess(lp[2], -5.0)
        weak = channelid_rows.row_logp(dict(row='L', share=0.6, intervals=8, changes=True))
        self.assertGreater(weak[1], -1.0)


if __name__ == '__main__':
    unittest.main()
