"""Relocation must preserve saved identities and initialize every child worker."""
import hashlib
import json
from pathlib import Path
import shlex
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
import _bootstrap as layout


class ScriptLayoutTests(unittest.TestCase):
    def test_frozen_runtime_preserves_every_bound_source_byte(self):
        manifest = json.loads((layout.RUNTIME/'source_manifest.json').read_text())
        for name, expected in manifest.items():
            with self.subTest(source=name):
                self.assertEqual(hashlib.sha256((layout.RUNTIME/name).read_bytes()).hexdigest(), expected)
        self.assertEqual((layout.SCRIPTS/'05_deflutter/normalization.py').read_bytes(),
                         layout.source_path('normalization.py').read_bytes())


    def test_external_artifacts_stay_in_the_checkout(self):
        import denoise_v2_cluster as cluster
        import denoise_v2_rollout as rollout
        self.assertEqual(rollout.INVENTORY, layout.REPO/'reports/denoise_v2_rollout_20261108/inventory.json')
        self.assertEqual(cluster.INVENTORY, rollout.INVENTORY)
        self.assertEqual(cluster.REVIEW, rollout.REVIEW)

    def test_all_indexed_sources_exist_and_unknown_paths_are_rejected(self):
        for name in layout.LAYOUT:
            self.assertTrue(layout.source_path(name).is_file(), name)
        for name in ('../outside.py', 'unknown.py', '/tmp/anything.py'):
            with self.assertRaises(ValueError):
                layout.source_path(name)

    def test_local_worker_dispatches_through_bootstrap_and_preserves_arguments(self):
        api = SimpleNamespace(Popen=Mock(return_value='worker'))
        processes = layout.ScriptProcesses(api)
        for name in ('denoise_v2_cluster.py', 'denoise_v2_accelerated.py'):
            result = processes.Popen([sys.executable, '-u', str(layout.source_path(name)),
                                      'worker', '--key', 'A15_T510076/CH28'], stdout='same')
            self.assertEqual(result, 'worker')
            self.assertEqual(api.Popen.call_args.args[0],
                layout.command(name, 'worker', '--key', 'A15_T510076/CH28'))
            self.assertEqual(api.Popen.call_args.kwargs, dict(stdout='same'))
        processes.Popen(['ssh', 'host', 'uptime'])
        self.assertEqual(api.Popen.call_args.args[0], ['ssh', 'host', 'uptime'])

    def test_maintenance_ssh_dispatch_keeps_options_and_quoted_arguments(self):
        api = SimpleNamespace(run=Mock(return_value='checked'))
        processes = layout.ScriptProcesses(api)
        for name in ('denoise_v2_cluster.py', 'denoise_v2_accelerated.py'):
            words = ['exec', '/uv path', 'run', '--python', '/python path',
                     '/repo with spaces/scripts/'+name, 'check', '--config', '/config path']
            command = ['ssh', '-o', 'BatchMode=yes', 'host', shlex.join(words)]
            self.assertEqual(processes.run(command, check=True), 'checked')
            actual = api.run.call_args.args[0]
            self.assertEqual(actual[:-1], command[:-1])
            self.assertEqual(shlex.split(actual[-1]), [*words[:5],
                '/repo with spaces/scripts/run.py', name, *words[6:]])

    def test_stage_and_runtime_help_start_in_fresh_processes(self):
        for name in ('pipeline.py', '01_concatenate.py', '01a_zip_originals.py', '03_denoise.py',
                     'denoise_v2_console.py', 'denoise_v2_launcher.py', 'denoise_v2_cluster.py',
                     'denoise_v2_accelerated.py', 'denoise_v2_rollout.py', 'qualify_denoise_numba.py',
                     '05a_plan_deflutter.py', '05b_deflutter.py', 'render_channel_review.py', 'inspect_tone.py',
                     '06_retime.py', '05c_retime.py', '07_normalize.py', 'quindar_survey.py', 'irig_search.py'):
            with self.subTest(script=name):
                result = subprocess.run(layout.command(name, '--help'), capture_output=True,
                                        text=True, timeout=45, cwd=layout.REPO)
                self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
                self.assertIn('usage:', result.stdout.lower())


if __name__ == '__main__':
    unittest.main()
