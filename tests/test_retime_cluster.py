"""07b two-host tape queue (retime_cluster.py)."""
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'scripts'), str(ROOT / 'scripts/30-track/07_retime')]
import _bootstrap  # noqa: E402
_bootstrap.install()
import retime_cluster as rc  # noqa: E402


class QueueTests(unittest.TestCase):
    def test_claim_is_exclusive_and_stale_claims_are_taken_over(self):
        with tempfile.TemporaryDirectory() as tmp:
            a, b = rc.TapeQueue(Path(tmp)), rc.TapeQueue(Path(tmp))
            self.assertTrue(a.claim('T1'))
            self.assertFalse(b.claim('T1'))
            claim = Path(tmp) / '.queue' / 'T1.claim'
            old = time.time() - rc.STALE_S - 10
            os.utime(claim, (old, old))
            self.assertTrue(b.claim('T1'))
            b.finish('T1', [])
            self.assertEqual(a.state('T1'), 'done')
            self.assertFalse(a.claim('T1'))

    def test_failed_tapes_are_reported_and_cleared_for_a_new_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            q = rc.TapeQueue(Path(tmp))
            q.claim('T2')
            q.finish('T2', ['no plan'])
            self.assertEqual(q.summary(['T2'])['T2'][1]['failures'], ['no plan'])
            q.reset_failed()
            self.assertEqual(q.state('T2'), 'open')

    def test_workers_split_the_tapes_between_them(self):
        with tempfile.TemporaryDirectory() as tmp:
            seen = []
            args = SimpleNamespace(retimed_root=Path(tmp), scratch=Path(tmp), tapes='T1,T2,T3', defluttered_root=Path(tmp), workers=1)

            def render(tape, args, log):
                seen.append(tape)
                rc.worker(args, lambda t, a, log: seen.append(t) or [], 'second', log=lambda m: None) if tape == 'T1' else None
                return []
            n = rc.worker(args, render, 'first', log=lambda m: None)
            self.assertEqual(sorted(seen), ['T1', 'T2', 'T3'])
            self.assertEqual(n, 1)


class CommandTests(unittest.TestCase):
    def test_genus_command_translates_archive_paths(self):
        self.assertEqual(rc.linux_path('D:\\Apollo_15_30-Track\\retimed'), '/mnt/d/Apollo_15_30-Track/retimed')
        args = SimpleNamespace(genus_scratch='/ssd/x', genus_workers=12, tapes='A15_T510075', no_row_shift=False,
                               allow_quindar_plan=False, no_irig=False, retimed_root=Path('D:/Apollo_15_30-Track/retimed'),
                               defluttered_root=Path('D:/Apollo_15_30-Track/defluttered_shared'),
                               quindar_analysis_root=Path('D:/x'), sg_fit=None)
        cmd = rc.genus_command(args)
        self.assertIn('--queue-worker', cmd)
        self.assertIn('/mnt/d/Apollo_15_30-Track/retimed', cmd)
        self.assertIn('--tapes A15_T510075', cmd)
        self.assertNotIn('D:', cmd)


if __name__ == '__main__':
    unittest.main()
