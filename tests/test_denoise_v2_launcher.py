"""Verify affinity dispatch without starting production workers."""
import argparse
from contextlib import contextmanager, nullcontext, redirect_stderr
import io
from pathlib import Path
import shlex
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from _bootstrap import install as _install
_install()
import denoise_v2_launcher as launcher


class LauncherTests(unittest.TestCase):
    def setUp(self):
        policy = patch.object(launcher, 'unity_outputs', return_value=nullcontext())
        self.policy = policy.start()
        self.addCleanup(policy.stop)

    def test_status_and_verification_writes_retry_through_shutdown(self):
        args = SimpleNamespace(genus_workers=0, genus_cpus='none', config='config')
        cluster = launcher.cluster
        original_json = cluster.atomic_json
        original_replace = Path.replace
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)/'cluster'
            folder.mkdir()
            paths = [folder/'startup.json', folder/'status.json', folder.parent/'status.json',
                     folder/'verification_cache.json']
            attempts = {}
            def replace(pending, target):
                attempts[target] = attempts.get(target, 0)+1
                if attempts[target] == 1:
                    error = PermissionError('Windows reader holds status open')
                    error.winerror = 5
                    raise error
                return original_replace(pending, target)
            @contextmanager
            def verification(_):
                cluster.atomic_json(paths[0], dict(phase='startup'))
                try:
                    yield
                finally:
                    cluster.atomic_json(paths[3], dict(phase='cache closed'))
            def run(_):
                for path in paths[1:3]:
                    cluster.atomic_json(path, dict(complete=115))
                raise RuntimeError('shutdown')
            with patch.object(cluster, 'read_json', return_value={}), \
                 patch.object(launcher, 'cached_verification', side_effect=verification), \
                 patch.object(cluster, 'coordinate', side_effect=run), \
                 patch.object(Path, 'replace', replace), \
                 patch.object(launcher.worker_runtime.time, 'sleep'), redirect_stderr(io.StringIO()):
                with self.assertRaisesRegex(RuntimeError, 'shutdown'):
                    launcher.coordinate(args)
            self.assertIs(cluster.atomic_json, original_json)
            self.assertEqual(attempts, dict.fromkeys(paths, 2))
            self.assertEqual(cluster.read_json(paths[1]), dict(complete=115))
            self.assertEqual(cluster.read_json(paths[3]), dict(phase='cache closed'))

    def test_local_workers_reach_runtime_wrapper_after_backend_selection(self):
        import denoise_v2_accelerated as accelerated
        original_processes = launcher.cluster.subprocess
        def run(_):
            launcher.cluster.subprocess.Popen([sys.executable, '-u', str(launcher.cluster.SCRIPT),
                                               'worker', '--key', 't/CH19'])
        for backend in (None, 'numba'):
            args = SimpleNamespace(genus_workers=0, genus_cpus='none', config='config', backend=backend)
            with patch.object(launcher.cluster, 'read_json', return_value={}), \
                 patch.object(launcher, 'cached_verification', return_value=nullcontext()), \
                 patch.object(accelerated, 'active_execution', return_value=('numba-test', {})), \
                 patch.object(accelerated, 'load_execution'), \
                 patch.object(original_processes, 'Popen') as popen, \
                 patch.object(launcher.cluster, 'coordinate', side_effect=run):
                launcher.coordinate(args)
            command = popen.call_args.args[0]
            self.assertEqual(Path(command[2]).name, 'run.py')
            self.assertEqual(command[3], 'denoise_v2_worker_runtime.py')
            self.assertEqual(command[4], 'denoise_v2_accelerated.py' if backend else 'denoise_v2_cluster.py')
            self.assertEqual(command[5:8], ['worker', '--key', 't/CH19'])
            if backend:
                self.assertEqual(command[-2:], ['--execution-id', 'numba-test'])
            self.assertIs(launcher.cluster.subprocess, original_processes)

    def test_affinity_preserves_ssh_options_and_quoted_worker_arguments(self):
        argv = ['/path with spaces/uv', 'run', 'worker.py', '--key', 'tape/CH03']
        command = ['ssh', '-o', 'BatchMode=yes', 'host', 'exec '+shlex.join(argv)]
        result = launcher.with_affinity(command, launcher.GENUS_CPUS)
        self.assertEqual(result[:-1], command[:-1])
        self.assertEqual(shlex.split(result[-1]), ['exec', 'taskset', '-c', launcher.GENUS_CPUS, *argv])
        self.assertEqual(shlex.split(command[-1]), ['exec', *argv])
        self.assertEqual(launcher.with_affinity(command, 'none'), command)

    def test_mask_validation(self):
        for value in ('', '1,1', '-1', '0-5', '0;true', '1,'):
            with self.assertRaises(argparse.ArgumentTypeError):
                launcher.cpu_list(value)
        self.assertEqual(launcher.cpu_list('00,2,4'), '0,2,4')
        self.assertEqual(launcher.cpu_list('none'), 'none')

    def test_every_replacement_worker_gets_affinity_and_dispatch_is_restored(self):
        args = SimpleNamespace(genus_workers=6, genus_cpus=launcher.GENUS_CPUS, config='config')
        config = dict(remote_repo='/repo with spaces')
        def original(config, *arguments):
            return ['ssh', 'host', 'exec '+shlex.join(['uv',
                config['remote_repo']+'/scripts/denoise_v2_cluster.py', *arguments])]
        def run(_):
            for key in ('first', 'replacement'):
                command = launcher.cluster.remote_command(config, 'worker', '--key', key)
                self.assertEqual(shlex.split(command[-1])[:4], ['exec', 'taskset', '-c', launcher.GENUS_CPUS])
                self.assertIn('/repo with spaces/scripts/03_denoise/denoise_v2_worker_runtime.py', shlex.split(command[-1]))
            self.assertEqual(launcher.cluster.remote_command(config, 'qualify'), original(config, 'qualify'))
            raise RuntimeError('coordinator stopped')
        with patch.object(launcher.cluster, 'remote_command', original), \
             patch.object(launcher.cluster, 'read_json', return_value=config), \
             patch.object(launcher, 'cached_verification', return_value=nullcontext()) as cache, \
             patch.object(launcher, 'check_affinity'), \
             patch.object(launcher.cluster, 'coordinate', side_effect=run):
            with self.assertRaisesRegex(RuntimeError, 'coordinator stopped'):
                launcher.coordinate(args)
            self.assertIs(launcher.cluster.remote_command, original)
            cache.assert_called_once_with(config)

    def test_invalid_capacity_or_failed_preflight_never_starts_coordinator(self):
        with patch.object(launcher.cluster, 'coordinate') as run:
            with self.assertRaises(ValueError):
                launcher.coordinate(SimpleNamespace(genus_workers=12, genus_cpus=launcher.GENUS_CPUS))
            with patch.object(launcher.cluster, 'read_json', return_value={}), \
                 patch.object(launcher, 'check_affinity', side_effect=RuntimeError('unavailable')):
                with self.assertRaisesRegex(RuntimeError, 'unavailable'):
                    launcher.coordinate(SimpleNamespace(genus_workers=6, genus_cpus=launcher.GENUS_CPUS, config='config'))
            run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
