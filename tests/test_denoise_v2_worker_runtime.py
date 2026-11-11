"""Acknowledged SSD cleanup must preserve evidence and allow worker resume."""
from contextlib import redirect_stderr
import io
from pathlib import Path
import shlex
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from _bootstrap import install as _install
_install()
import denoise_v2_worker_runtime as runtime


class WorkerRuntimeTests(unittest.TestCase):
    def result(self, base, channel, extra=False, digest='accepted'):
        key = Path('A15_T510076')/f'CH{channel:02d}'
        completed = base/'ssd'/'output'/key
        completed.mkdir(parents=True)
        filename = f'CH{channel:02d}.wav'
        runtime.cluster.atomic_json(completed/'denoised.json',
            dict(audio=dict(filename=filename, sha256=digest)))
        (completed/filename).write_bytes(b'preserved audio')
        (completed/'cleanup.jsonl').write_bytes(b'preserved cleanup')
        if extra:
            (completed/'denoised.before-numba.json').write_bytes(b'preserved provenance')
        ack = base/'session'/'acks'/key.with_suffix('.json')
        ack.parent.mkdir(parents=True, exist_ok=True)
        runtime.cluster.atomic_json(ack, dict(audio_sha256='accepted'))
        return completed

    def test_extra_files_preserve_entire_result_without_blocking_other_cleanup(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp).resolve()
            retained = self.result(base, 27, extra=True)
            expected = {p.name:p.read_bytes() for p in retained.iterdir()}
            removable = self.result(base, 28)
            log = io.StringIO()
            with patch.object(runtime.cluster, 'session_path', return_value=base/'session'), redirect_stderr(log):
                runtime.release_acknowledged({}, base/'ssd'/'output')
            self.assertEqual({p.name:p.read_bytes() for p in retained.iterdir()}, expected)
            self.assertFalse(removable.exists())
            self.assertIn('denoised.before-numba.json', log.getvalue())
            self.assertIn('continuing worker', log.getvalue())

    def test_missing_files_are_retained(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp).resolve()
            retained = self.result(base, 27)
            (retained/'cleanup.jsonl').unlink()
            with patch.object(runtime.cluster, 'session_path', return_value=base/'session'), redirect_stderr(io.StringIO()):
                runtime.release_acknowledged({}, base/'ssd'/'output')
            self.assertEqual((retained/'CH27.wav').read_bytes(), b'preserved audio')

    def test_acknowledgement_mismatch_still_refuses_cleanup(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp).resolve()
            retained = self.result(base, 27, digest='different')
            with patch.object(runtime.cluster, 'session_path', return_value=base/'session'):
                with self.assertRaisesRegex(ValueError, 'Acknowledgement differs'):
                    runtime.release_acknowledged({}, base/'ssd'/'output')
            self.assertEqual(len(list(retained.iterdir())), 3)

    def test_invalid_audio_filename_cannot_remove_files_outside_channel(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp).resolve()
            retained = self.result(base, 27)
            runtime.cluster.atomic_json(retained/'denoised.json',
                dict(audio=dict(filename='../outside.wav', sha256='accepted')))
            with patch.object(runtime.cluster, 'session_path', return_value=base/'session'):
                with self.assertRaisesRegex(ValueError, 'Invalid SSD audio filename'):
                    runtime.release_acknowledged({}, base/'ssd'/'output')
            self.assertTrue((retained/'CH27.wav').exists())

    def test_locked_result_is_left_for_later_cleanup(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp).resolve()
            retained = self.result(base, 27)
            with patch.object(runtime.cluster, 'session_path', return_value=base/'session'), \
                 patch.object(runtime.cluster, 'planning_lock', side_effect=ValueError('Another planner is already using result')):
                runtime.release_acknowledged({}, base/'ssd'/'output')
            self.assertEqual(len(list(retained.iterdir())), 3)

    def test_dispatch_preserves_quoted_arguments_for_both_backends(self):
        config = dict(remote_repo='/repo with spaces')
        for name in runtime.WORKER_SCRIPTS:
            argv = ['exec', '/uv path', 'run', '/repo with spaces/scripts/'+name,
                    'worker', '--key', 'A15_T510076/CH28']
            command = ['ssh', '-o', 'BatchMode=yes', 'host', shlex.join(argv)]
            result = runtime.remote_worker_command(command, config)
            self.assertEqual(result[:-1], command[:-1])
            self.assertEqual(shlex.split(result[-1]), [*argv[:3],
                '/repo with spaces/scripts/03_denoise/denoise_v2_worker_runtime.py', name, *argv[4:]])

    def test_reference_worker_uses_patched_module_and_restores_it(self):
        original_cleanup = runtime.cluster.release_acknowledged
        arguments = ['runtime', 'denoise_v2_cluster.py', 'worker', '--config', 'config']
        def run():
            self.assertIs(runtime.cluster.release_acknowledged, runtime.release_acknowledged)
            self.assertEqual(sys.argv[1:], arguments[2:])
            return 75
        with patch.object(sys, 'argv', arguments), patch.object(runtime.cluster, 'main', side_effect=run):
            self.assertEqual(runtime.main(), 75)
            self.assertIs(sys.argv, arguments)
        self.assertIs(runtime.cluster.release_acknowledged, original_cleanup)

    def test_accelerated_worker_keeps_exit_code_and_restores_patch(self):
        original_cleanup = runtime.cluster.release_acknowledged
        arguments = ['runtime', 'denoise_v2_accelerated.py', 'worker', '--config', 'config']
        def run(path, run_name):
            self.assertIs(runtime.cluster.release_acknowledged, runtime.release_acknowledged)
            self.assertEqual(run_name, '__main__')
            self.assertEqual(Path(path).name, arguments[1])
            raise SystemExit(75)
        with patch.object(sys, 'argv', arguments), patch.object(runtime.runpy, 'run_path', side_effect=run):
            with self.assertRaises(SystemExit) as exit_status:
                runtime.main()
            self.assertEqual(exit_status.exception.code, 75)
            self.assertIs(sys.argv, arguments)
        self.assertIs(runtime.cluster.release_acknowledged, original_cleanup)


if __name__ == '__main__':
    unittest.main()
