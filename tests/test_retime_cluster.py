"""07b two-host tape queue (retime_cluster.py)."""
import os
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'scripts'), str(ROOT / 'scripts/30-track/07_retime')]
import _bootstrap  # noqa: E402
_bootstrap.install()
import retime_cluster as rc  # noqa: E402
import staged_io  # noqa: E402


def channels(root, tape, n):
    """``n`` fake 05b channels of ``tape`` and their retimed destinations."""
    jobs = []
    for c in range(1, n + 1):
        src = root / 'in' / tape / f'CH{c:02d}' / 'deflutter.wav'
        src.parent.mkdir(parents=True)
        src.write_bytes(bytes([c]) * 1000)
        jobs.append((src, root / 'retimed' / tape / f'CH{c:02d}', None))
    return jobs


def fake_render(args):
    source, staged, out, plan, knots = args
    if 'CH99' in source:
        raise RuntimeError('render failed')
    Path(out).mkdir(parents=True)
    (Path(out) / 'retimed.wav').write_bytes(Path(staged).read_bytes())
    return ['rendered']


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
            root = Path(tmp)
            jobs = {t: channels(root, t, 2) for t in ('T1', 'T2', 'T3')}
            args = SimpleNamespace(retimed_root=root / 'retimed', scratch=root / 'scratch', tapes='T1,T2,T3',
                                   defluttered_root=root, workers=1)
            seen = []

            def prepare(tape, args, log):
                seen.append(tape)
                if tape == 'T1':  # a second host takes the remaining tapes while this one prepares T1
                    second = SimpleNamespace(**vars(args))
                    rc.worker(second, lambda t, a, log: (seen.append(t), ('plan', jobs[t]))[1], 'second', root,
                              log=lambda m: None, render=fake_render, pool_factory=ThreadPoolExecutor)
                return 'plan', jobs[tape]
            n = rc.worker(args, prepare, 'first', root, log=lambda m: None, render=fake_render,
                          pool_factory=ThreadPoolExecutor)
            self.assertEqual(sorted(seen), ['T1', 'T2', 'T3'])
            self.assertEqual(n, 1)
            self.assertTrue(all(rc.TapeQueue(root / 'retimed').state(t) == 'done' for t in ('T1', 'T2', 'T3')))

    def test_a_tape_that_cannot_be_prepared_fails_and_the_queue_continues(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            jobs = channels(root, 'T2', 1)
            args = SimpleNamespace(retimed_root=root / 'retimed', scratch=root / 'scratch', tapes='T1,T2',
                                   defluttered_root=root, workers=2)

            def prepare(tape, args, log):
                if tape == 'T1':
                    raise ValueError('no plan')
                return 'plan', jobs
            self.assertEqual(rc.worker(args, prepare, 'w', root, log=lambda m: None, render=fake_render,
                                       pool_factory=ThreadPoolExecutor), 2)
            status = rc.TapeQueue(root / 'retimed').summary(['T1', 'T2'])
            self.assertEqual(status['T1'][1]['failures'], ['no plan'])
            self.assertEqual(status['T2'][0], 'done')


class StreamTests(unittest.TestCase):
    def test_next_tape_starts_while_the_last_channel_of_the_previous_one_renders(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            t1, t2 = channels(root, 'T1', 3), channels(root, 'T2', 3)
            release, order, done = threading.Event(), [], {}

            def render(args):
                if 'T1' in args[0] and 'CH03' in args[0]:  # the last channel of T1 is slow
                    order.append('T1 CH03 start')
                    self.assertTrue(release.wait(10))
                elif 'T2' in args[0]:
                    order.append('T2 start')
                    release.set()
                return fake_render(args)
            batches = iter([('T1', t1, 'plan', root / 'scratch' / 'T1'), ('T2', t2, 'plan', root / 'scratch' / 'T2')])
            staged_io.run_stream(lambda: next(batches, None), 2, root, lambda k, f: done.setdefault(k, f),
                                 log=lambda m: None, render=render, pool_factory=ThreadPoolExecutor)
            self.assertEqual(done, {'T1': [], 'T2': []})
            self.assertLess(order.index('T1 CH03 start'), order.index('T2 start'))
            for _, destination, _ in t1 + t2:
                self.assertTrue((destination / 'retimed.wav').is_file())
            self.assertFalse((root / 'scratch' / 'T1').exists())

    def test_a_failed_channel_is_reported_with_its_own_tape_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            t1, t2 = channels(root, 'T1', 2), channels(root, 'T2', 1)
            bad = root / 'in' / 'T1' / 'CH99' / 'deflutter.wav'
            bad.parent.mkdir(parents=True)
            bad.write_bytes(b'x')
            t1.append((bad, root / 'retimed' / 'T1' / 'CH99', None))
            batches = iter([('T1', t1, 'p', root / 's' / 'T1'), ('T2', t2, 'p', root / 's' / 'T2'), ('T3', [], 'p', root / 's' / 'T3')])
            done = {}
            staged_io.run_stream(lambda: next(batches, None), 2, root, lambda k, f: done.setdefault(k, f),
                                 log=lambda m: None, render=fake_render, pool_factory=ThreadPoolExecutor)
            self.assertEqual(done, {'T1': ['CH99: render failed'], 'T2': [], 'T3': []})


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
