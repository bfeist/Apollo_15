"""Mocked console/coordinator lifecycle checks; never launch workers or SSH."""
from contextlib import ExitStack, nullcontext
from pathlib import Path
import signal
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from _bootstrap import install as _install
_install()
import denoise_v2_console as console


class ScheduleLifecycleTests(unittest.TestCase):
    def session(self, *, exit_code=1, worker_code=75, worker_status='interrupted',
                stop_reason='Stopping at checkpoints', updated=101,
                ctrl_c_during_wait=False, ctrl_c_at_restart_message=False,
                auto=True):
        with tempfile.TemporaryDirectory() as temp, ExitStack() as stack:
            folder = Path(temp)
            job = dict(tape='T', channel=1, source=dict(frames=8000, sample_rate=8000))
            status = dict(updated=updated, stop_reason=stop_reason, jobs={
                'T/CH01':dict(status=worker_status, host='windows', cursor=4000,
                             exit_code=worker_code)})
            args = SimpleNamespace(auto_workers=auto, local_workers=12 if auto else 8,
                                   genus_workers=6, genus_cpus='0,2,4,8,10,12',
                                   backend='numba', tapes=3, run_seconds=None,
                                   plain=True, all_tapes=False, user_cancelled=False)
            child = SimpleNamespace(returncode=None, pid=123)
            polls = iter([None, exit_code])
            def poll():
                child.returncode = next(polls, exit_code)
                return child.returncode
            child.poll = poll
            handlers = {}
            def handler(sig, action):
                previous = handlers.get(sig, signal.default_int_handler)
                handlers[sig] = action
                return previous
            display = Mock()
            def printed(message, **kwargs):
                if ctrl_c_at_restart_message and 'Checkpoints saved.' in str(message):
                    handlers[signal.SIGINT](signal.SIGINT, None)
            display.print.side_effect = printed
            def read(path):
                name = Path(path).name
                if name == 'config.json':
                    return dict(ssh='unused')
                if name == 'jobs.json':
                    return [job]
                if name == 'status.json':
                    return status
                return dict(run_id='test')
            patches = [patch.object(console.cluster, 'read_json', side_effect=read),
                       patch.object(console.cluster, 'session_path', return_value=folder),
                       patch.object(console.cluster, 'planning_lock', return_value=nullcontext()),
                       patch.object(console.cluster, 'atomic_json'),
                       patch.object(console, 'release_stopped_connections', return_value=0),
                       patch.object(console, 'Console', return_value=display),
                       patch.object(console.subprocess, 'Popen', return_value=child),
                       patch.object(console.signal, 'signal', side_effect=handler),
                       patch.object(console.time, 'time', return_value=100),
                       patch.object(console.time, 'monotonic', return_value=100),
                       patch.object(console.time, 'sleep', side_effect=lambda _: (
                           handlers[signal.SIGINT](signal.SIGINT, None) if ctrl_c_during_wait else None)),
                       patch.object(console, 'scheduled_windows_workers', return_value=6)]
            for mocked in patches:
                stack.enter_context(mocked)
            result = console.run_session(args)
            return result, (folder/'stop_requested').exists()

    def test_schedule_boundary_stops_at_checkpoints_then_requests_restart(self):
        self.assertEqual(self.session(), ((0, True), True))

    def test_ctrl_c_while_workers_checkpoint_prevents_restart(self):
        self.assertEqual(self.session(ctrl_c_during_wait=True), ((1, False), True))

    def test_ctrl_c_at_restart_announcement_prevents_restart(self):
        self.assertEqual(self.session(ctrl_c_at_restart_message=True), ((130, False), True))

    def test_real_worker_failure_during_schedule_stop_prevents_restart(self):
        self.assertEqual(self.session(worker_code=2), ((1, False), True))

    def test_failed_job_or_coordinator_failure_prevents_restart(self):
        for options in (dict(worker_status='failed'), dict(exit_code=2), dict(updated=99)):
            with self.subTest(options=options):
                self.assertEqual(self.session(**options)[0], (options.get('exit_code', 1), False))

    def test_successful_completion_and_fixed_limit_do_not_restart(self):
        self.assertEqual(self.session(exit_code=0, worker_status='complete')[0], (0, False))
        self.assertEqual(self.session(auto=False)[0], (1, False))

    def test_user_cancellation_latch_prevents_starting_another_session(self):
        calls = []
        def session(args):
            calls.append(args.local_workers)
            if len(calls) > 1:
                self.fail('A user stop must survive the session restart boundary')
            args.user_cancelled = True
            return 0, True
        with patch.object(sys, 'argv', ['console']), \
             patch.object(sys, 'platform', 'win32'), \
             patch.object(console, 'scheduled_windows_workers', return_value=6), \
             patch.object(console, 'run_session', side_effect=session):
            self.assertIn(console.main(), (0, 130))
        self.assertEqual(calls, [6])

    def test_keyboard_interrupt_between_sessions_does_not_start_another(self):
        with patch.object(sys, 'argv', ['console']), \
             patch.object(sys, 'platform', 'win32'), \
             patch.object(console, 'scheduled_windows_workers', side_effect=[6, KeyboardInterrupt]), \
             patch.object(console, 'run_session', return_value=(0, True)) as session:
            self.assertEqual(console.main(), 130)
        self.assertEqual(session.call_count, 1)


if __name__ == '__main__':
    unittest.main()
