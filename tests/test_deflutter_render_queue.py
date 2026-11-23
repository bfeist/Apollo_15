import sys
from pathlib import Path
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts/05_deflutter'))
from render_queue import run_queue


class RenderQueueTests(unittest.TestCase):
    def test_free_worker_crosses_tapes_while_other_worker_is_still_rendering(self):
        next_tape_started = threading.Event()
        disk = threading.Lock()
        resident = set()
        completed = set()
        published = []
        rows = {'A': ['slow', 'fast'], 'B': ['second tape'], 'C': []}

        def stage(tape):
            self.assertTrue(disk.acquire(blocking=False))
            try:
                resident.add(tape)
                self.assertLessEqual(len(resident), 2)
                return rows[tape], rows[tape]
            finally:
                disk.release()

        def render(slot, tape, channel):
            if channel == 'slow':
                self.assertTrue(next_tape_started.wait(timeout=5))
            if tape == 'B':
                self.assertNotIn(('A', 'slow'), completed)
                next_tape_started.set()
            completed.add((tape, channel))

        def publish(tape, jobs):
            self.assertTrue(disk.acquire(blocking=False))
            try:
                self.assertTrue(all((tape, job) in completed for job in jobs))
                resident.remove(tape)
                published.append(tape)
            finally:
                disk.release()

        run_queue(rows, ['Proprius', 'Genus'], stage, render, publish, threading.Event())
        self.assertCountEqual(published, rows)
        self.assertEqual(len(completed), 3)
        self.assertFalse(resident)

    def test_drain_leaves_partial_tape_for_resume(self):
        stop = threading.Event()
        rendered, published = [], []

        def render(slot, tape, channel):
            rendered.append(channel)
            stop.set()

        run_queue(['A'], ['Genus'], lambda _: ([1, 2], [1, 2]), render,
                  lambda tape, _: published.append(tape), stop)
        self.assertEqual(rendered, [1])
        self.assertFalse(published)

    def test_failed_worker_prevents_tape_publication(self):
        stop = threading.Event()
        def render(*_):
            raise ValueError('unverified channel')
        with self.assertRaisesRegex(ValueError, 'unverified channel'):
            run_queue(['A'], ['Genus'], lambda _: ([1], [1]), render,
                      lambda *_: self.fail('published a failed tape'), stop)
        self.assertTrue(stop.is_set())
