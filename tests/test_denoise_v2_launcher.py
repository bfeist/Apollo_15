"""Verify affinity dispatch without starting production workers."""
import argparse
from pathlib import Path
import shlex
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from _bootstrap import install as _install
_install()
import denoise_v2_launcher as launcher


class LauncherTests(unittest.TestCase):
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
             patch.object(launcher, 'check_affinity'), \
             patch.object(launcher.cluster, 'coordinate', side_effect=run):
            with self.assertRaisesRegex(RuntimeError, 'coordinator stopped'):
                launcher.coordinate(args)
            self.assertIs(launcher.cluster.remote_command, original)

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
