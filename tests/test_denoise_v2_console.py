from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from _bootstrap import install as _install
_install()
import denoise_v2_console as console_module
from denoise_v2_console import Rates, duration, metrics, render, worker_ssh_descendants, release_stopped_connections


class ConsoleTests(unittest.TestCase):
    def test_worker_failure_shows_actual_log_error_and_ignores_clean_checkpoints(self):
        from rich.console import Console
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            (folder/'logs').mkdir()
            (folder/'logs'/'tape_CH28.log').write_text(
                'progress\nAccelerated worker stopped: Unexpected SSD output files: CH27\n', encoding='utf-8')
            console = Console(record=True, width=140)
            states = dict(jobs={
                'tape/CH28':dict(status='failed', host='genus', exit_code=2),
                'tape/CH29':dict(status='interrupted', host='genus', exit_code=75)})
            self.assertTrue(console_module.report_worker_failures(console, folder, states))
            output = console.export_text()
            self.assertIn('Unexpected SSD output files: CH27', output)
            self.assertIn('tape/CH28', output)
            self.assertNotIn('tape/CH29', output)
            self.assertFalse(console_module.report_worker_failures(console, folder,
                dict(jobs={'tape/CH29':states['jobs']['tape/CH29']})))

    def test_eastern_day_night_boundaries_and_dst(self):
        eastern = ZoneInfo('America/New_York')
        for hour, minute, expected in ((0,59,6), (1,0,12), (8,59,12), (9,0,6), (23,59,6)):
            value = datetime(2026, 11, 10, hour, minute, tzinfo=eastern)
            self.assertEqual(console_module.scheduled_windows_workers(value), expected)
        for fold in (0, 1):
            self.assertEqual(console_module.scheduled_windows_workers(
                datetime(2026,11,1,1,30,tzinfo=eastern,fold=fold)), 12)
        self.assertEqual(console_module.scheduled_windows_workers(
            datetime(2026,11,10,14,0,tzinfo=timezone.utc)), 6)

    def test_scheduled_restart_recalculates_limit_and_fixed_override_does_not(self):
        limits = []
        def session(args):
            limits.append(args.local_workers)
            return 0, len(limits) == 1
        with patch.object(sys, 'argv', ['console']), \
             patch.object(console_module, 'scheduled_windows_workers', side_effect=[6,12]), \
             patch.object(console_module, 'run_session', side_effect=session):
            self.assertEqual(console_module.main(), 0)
        self.assertEqual(limits, [6,12])
        with patch.object(sys, 'argv', ['console', '--local-workers', '8']), \
             patch.object(console_module, 'scheduled_windows_workers') as schedule, \
             patch.object(console_module, 'run_session', return_value=(0,False)) as run:
            self.assertEqual(console_module.main(), 0)
            schedule.assert_not_called()
            self.assertEqual(run.call_args.args[0].local_workers, 8)

    def test_shutdown_targets_only_this_coordinators_worker_connections(self):
        def process(pid, parent, name, command=''):
            return dict(ProcessId=pid, ParentProcessId=parent, Name=name, CommandLine=command)
        rows = [process(10, 1, 'python.exe'), process(11, 10, 'python.exe'),
                process(12, 11, 'ssh.exe', 'ssh host denoise_v2_cluster.py worker --key tape/CH01'),
                process(13, 11, 'ssh.exe', 'ssh host uptime'),
                process(15, 11, 'ssh.exe', 'ssh host denoise_v2_accelerated.py worker --key tape/CH03'),
                process(14, 99, 'ssh.exe', 'ssh host denoise_v2_cluster.py worker --key tape/CH02')]
        self.assertEqual([p['ProcessId'] for p in worker_ssh_descendants(rows, 10)], [12, 15])

    def test_shutdown_leaves_connections_alone_when_remote_active_or_unreachable(self):
        for code, stdout, stderr in ((0, '123 python worker', ''), (255, '', 'SSH failed'),
                                     (1, '', 'Unexpected error')):
            with patch('denoise_v2_console.subprocess.run', return_value=SimpleNamespace(
                    returncode=code, stdout=stdout, stderr=stderr)) as run:
                self.assertEqual(release_stopped_connections(dict(ssh='genus'), 10), 0)
                self.assertEqual(run.call_count, 1)

    def test_resumed_hours_are_not_counted_as_new_work(self):
        rates = Rates(warmup=30)
        states = {'t/CH01': dict(host='windows', cursor=8000*36000)}
        rates.observe(states, 100)
        self.assertIsNone(rates.rate('t/CH01', 8000))
        states['t/CH01']['cursor'] += 8000*120
        rates.observe(states, 160)
        self.assertEqual(rates.rate('t/CH01', 8000), 2.)

    def test_reset_rewarms_rate_and_old_samples_expire(self):
        rates = Rates(window=60, warmup=30)
        state = {'a': dict(host='genus', cursor=1000)}
        rates.observe(state, 0)
        state['a']['cursor'] = 2000
        rates.observe(state, 60)
        state['a']['cursor'] = 0
        rates.observe(state, 90)
        self.assertIsNone(rates.rate('a', 1))
        state['a']['cursor'] = 60
        rates.observe(state, 150)
        self.assertEqual(rates.rate('a', 1), 1.)

    def test_tape_and_archive_rates_use_both_hosts_and_pending_audio(self):
        jobs = {f'T{t}/CH{c}': dict(tape=f'T{t}', source=dict(frames=1000, sample_rate=1))
                for t,c in ((1,1),(1,2),(1,3),(2,1))}
        states = {'T1/CH1': dict(status='running',host='windows',cursor=100),
                  'T1/CH2': dict(status='running',host='genus',cursor=200),
                  'T1/CH3': dict(status='pending'), 'T2/CH1': dict(status='complete')}
        rates = Rates(warmup=30)
        rates.observe(states, 0)
        states['T1/CH1']['cursor'] = 220
        states['T1/CH2']['cursor'] = 440
        rates.observe(states, 60)
        result = metrics(jobs, states, rates)
        self.assertEqual(result['rate'], 6.)
        self.assertEqual(result['remaining_seconds'], 2340)
        self.assertEqual(result['eta_seconds'], 390)
        self.assertEqual(result['tapes']['T1']['eta_seconds'], 390)
        self.assertEqual(result['workers']['T1/CH1']['eta_seconds'], 390)
        self.assertEqual(result['hosts']['genus']['rate'], 4.)
        from rich.console import Console
        console = Console(record=True, width=140)
        console.print(render(dict(jobs=states), result, dict(windows=6, genus=16)))
        output = console.export_text()
        self.assertIn('Archive ETA', output)
        self.assertIn('Tape ETA', output)
        self.assertIn('Channel ETA', output)

    def test_overall_eta_waits_for_new_workers(self):
        jobs = {k:dict(tape='t',source=dict(frames=1000,sample_rate=1)) for k in ('a','b')}
        states = {'a':dict(status='running',host='windows',cursor=0)}
        rates = Rates(warmup=30)
        rates.observe(states, 0)
        states['a']['cursor'] = 120
        states['b'] = dict(status='running',host='genus',cursor=0)
        rates.observe(states, 60)
        self.assertIsNone(metrics(jobs, states, rates)['eta_seconds'])
        rates.observe(states, 120)
        self.assertIsNone(rates.rate('b', 1))
        self.assertIsNone(metrics(jobs, states, rates)['eta_seconds'])
        self.assertEqual(duration(None), 'warming up')
        self.assertEqual(duration(90000), '~1d 1h')


if __name__ == '__main__':
    unittest.main()
