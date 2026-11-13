"""Acknowledged SSD cleanup must preserve evidence and allow worker resume."""
from contextlib import redirect_stderr
import io
import errno
import importlib.util
from pathlib import Path
import shlex
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from _bootstrap import install as _install
_install()
import denoise_v2_worker_runtime as runtime


class WorkerRuntimeTests(unittest.TestCase):
    def test_late_flush_error_reuses_only_a_fully_matching_private_upload(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp).resolve()
            source, pending = base/'completed.wav', base/'CH27.upload'
            pending.mkdir()
            source.write_bytes(b'finished WAV data'*100)
            destination = pending/'CH27.wav'
            copies = runtime.UploadCopies(runtime.cluster.shutil, pending)
            original_copy = runtime.cluster.shutil.copyfileobj
            with patch.object(runtime.os, 'fsync', side_effect=[OSError(errno.EIO, 'late CIFS flush error'), None]), \
                 patch.object(runtime.cluster.shutil, 'copyfileobj', wraps=original_copy) as copy:
                with self.assertRaises(OSError):
                    copies.copyfile(source, destination)
                self.assertEqual(destination.read_bytes(), source.read_bytes())
                before = destination.stat().st_mtime_ns
                copies.copyfile(source, destination)
            copy.assert_called_once()
            self.assertEqual(destination.stat().st_mtime_ns, before)
            # A same-sized but corrupt private upload must be copied again.
            destination.write_bytes(b'x'*source.stat().st_size)
            with patch.object(runtime.cluster.shutil, 'copyfileobj', wraps=original_copy) as copy:
                copies.copyfile(source, destination)
            copy.assert_called_once()
            self.assertEqual(destination.read_bytes(), source.read_bytes())

    def test_unreadable_stop_flag_does_not_abort_recovery_or_reset_upload_progress(self):
        cluster = runtime.cluster
        emit = Mock()
        attempts = []
        def worker(*_):
            attempts.append(None)
            if len(attempts) == 1:
                cluster.emit(event='progress', host='genus', key='t/CH27',
                             cursor=0, frames=100, phase='uploading')
                raise OSError(errno.EIO, 'network outage')
            return 'complete'
        with patch.object(cluster, 'session_path', return_value=Path('session')), \
             patch.object(cluster, 'emit', emit), \
             patch.object(runtime, 'STORAGE_DELAYS', (2,)), \
             patch.object(Path, 'exists', side_effect=OSError(errno.EIO, 'share unreachable')), \
             patch.object(runtime.time, 'sleep'), redirect_stderr(io.StringIO()):
            self.assertEqual(runtime.retry_storage_worker(worker, {}, 't/CH27', 'genus'), 'complete')
            self.assertIs(cluster.emit, emit)
        self.assertEqual(len(attempts), 2)
        for call in emit.call_args_list:
            self.assertEqual(call.kwargs['cursor'], 100)
            self.assertEqual(call.kwargs['frames'], 100)

    def test_upload_io_failure_reuses_completed_result_and_keeps_inbox_private(self):
        self.assert_upload_recovery(late_flush=False)

    def test_upload_eagain_on_final_flush_reuses_completed_audio_and_matching_upload(self):
        self.assert_upload_recovery(late_flush=True)

    def assert_upload_recovery(self, *, late_flush):
        cluster = runtime.cluster
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp).resolve()
            folder, root, work = base/'session', base/'ssd'/'output', base/'ssd'/'work'
            key = 'A15_T510079/CH27'
            job = dict(tape='A15_T510079', channel=27, source=dict(frames=32), expected_audio_bytes=128)
            folder.mkdir()
            cluster.atomic_json(folder/'jobs.json', [job])
            source = root/key
            source.mkdir(parents=True)
            result = dict(audio=dict(filename='CH27.wav', sha256='preserved'), worker_seconds=123)
            (source/'CH27.wav').write_bytes(b'completed audio'*20)
            (source/'cleanup.jsonl').write_bytes(b'completed cleanup')
            cluster.atomic_json(source/'denoised.json', result)
            before = {p.name:p.read_bytes() for p in source.iterdir()}
            inbox = folder/'inbox'/key
            pending = inbox.with_name('CH27.upload')
            original_copy = cluster.shutil.copyfileobj
            attempts = []
            def copy(reader, writer, length):
                attempts.append(Path(writer.name).name)
                self.assertFalse(inbox.exists())
                if len(attempts) == 1 and not late_flush:
                    writer.write(reader.read(17))
                    raise OSError(errno.EIO, 'CIFS temporarily unavailable')
                return original_copy(reader, writer, length)
            def wait(_):
                self.assertTrue(pending.exists())
                self.assertFalse(inbox.exists())
                self.assertEqual({p.name:p.read_bytes() for p in source.iterdir()}, before)
            flushes = []
            def fsync(_):
                flushes.append(None)
                if late_flush and len(flushes) == 1:
                    raise BlockingIOError(errno.EAGAIN, 'CIFS flush temporarily unavailable')
            with patch.object(cluster, 'os', SimpleNamespace(name='posix')), \
                 patch.object(cluster, 'session_path', return_value=folder), \
                 patch.object(cluster, 'check_code'), \
                 patch.object(cluster, 'verify_qualification', return_value=dict(frozen={})), \
                 patch.object(cluster, 'remote_freeze', return_value={}), \
                 patch.object(cluster, 'mapped_job', side_effect=lambda job, _:job), \
                 patch.object(cluster, 'check_scratch', return_value=(base/'ssd', root, work)), \
                 patch.object(cluster, 'release_acknowledged'), \
                 patch.object(cluster, 'write_channel', side_effect=lambda *args, **kwargs:cluster.read_json(source/'denoised.json')) as writer, \
                 patch.object(cluster.shutil, 'copyfileobj', side_effect=copy), \
                 patch.object(cluster.shutil, 'copyfile', side_effect=AssertionError('fast copy must not run')), \
                 patch.object(runtime.os, 'fsync', side_effect=fsync), \
                 patch.object(cluster, 'emit') as emit, \
                 patch.object(runtime.time, 'sleep', side_effect=wait), redirect_stderr(io.StringIO()):
                runtime.retry_storage_worker(cluster.worker, dict(remote_data='/data'), key, 'genus')
            self.assertEqual(writer.call_count, 2)
            for call in writer.call_args_list:
                self.assertTrue(call.kwargs['resume'])
            expected = ['CH27.wav', 'cleanup.jsonl', 'denoised.json', 'genus.execution.json']
            if not late_flush:
                expected.insert(0, 'CH27.wav')
            self.assertEqual(attempts, expected)
            if late_flush:
                self.assertGreaterEqual(len(flushes), 5)  # Matching upload must be flushed again.
            self.assertFalse(pending.exists())
            self.assertEqual((inbox/'CH27.wav').read_bytes(), before['CH27.wav'])
            self.assertEqual((inbox/'cleanup.jsonl').read_bytes(), before['cleanup.jsonl'])
            self.assertEqual((inbox/'genus.execution.json').read_bytes(), before['denoised.json'])
            self.assertEqual(cluster.read_json(inbox/'denoised.json')['audio']['mtime_ns'], (inbox/'CH27.wav').stat().st_mtime_ns)
            self.assertEqual({p.name:p.read_bytes() for p in source.iterdir()}, before)
            self.assertTrue(any(call.kwargs.get('phase') == 'storage retry' for call in emit.call_args_list))

    def test_storage_retries_are_bounded_and_non_io_failures_are_not_retried(self):
        for error, expected in ((OSError(errno.EIO, 'share unavailable'), len(runtime.STORAGE_DELAYS)+1),
                                (BlockingIOError(errno.EAGAIN, 'flush temporarily unavailable'), len(runtime.STORAGE_DELAYS)+1),
                                (OSError(errno.ENOSPC, 'disk full'), 1),
                                (PermissionError(errno.EACCES, 'denied'), 1),
                                (ValueError('source identity changed'), 1)):
            worker = Mock(side_effect=error)
            copies = runtime.cluster.shutil
            with patch.object(runtime.cluster, 'session_path', return_value=Path('session')), \
                 patch.object(Path, 'exists', return_value=False), \
                 patch.object(runtime.cluster, 'emit'), patch.object(runtime.time, 'sleep'), \
                 redirect_stderr(io.StringIO()):
                with self.assertRaises(type(error)) as raised:
                    runtime.retry_storage_worker(worker, {}, 't/CH27', 'genus')
            self.assertIs(raised.exception, error)
            self.assertEqual(worker.call_count, expected)
            self.assertIs(runtime.cluster.shutil, copies)

    def test_stop_during_storage_backoff_returns_checkpoint_exit_without_relaunch(self):
        worker = Mock(side_effect=OSError(errno.EIO, 'share unavailable'))
        with patch.object(runtime.cluster, 'session_path', return_value=Path('session')), \
             patch.object(Path, 'exists', return_value=True), \
             patch.object(runtime.cluster, 'emit'), patch.object(runtime.time, 'sleep') as sleep, \
             redirect_stderr(io.StringIO()):
            self.assertEqual(runtime.retry_storage_worker(worker, {}, 't/CH27', 'genus'), 75)
        worker.assert_called_once()
        sleep.assert_not_called()

    def test_windows_workers_and_unrelated_copies_keep_original_behavior(self):
        worker = Mock(side_effect=OSError(errno.EIO, 'local failure'))
        with self.assertRaises(OSError):
            runtime.retry_storage_worker(worker, {}, 't/CH27', 'windows')
        worker.assert_called_once()
        original = SimpleNamespace(copyfile=Mock(return_value='unchanged'))
        copies = runtime.UploadCopies(original, Path('session/inbox/t/CH27.upload'))
        self.assertEqual(copies.copyfile('source', 'elsewhere', follow_symlinks=False), 'unchanged')
        original.copyfile.assert_called_once_with('source', Path('elsewhere'), follow_symlinks=False)

    def windows_error(self, code):
        error = PermissionError('Windows file is locked')
        error.winerror = code
        return error

    def test_transient_windows_errors_retry_without_changing_payload(self):
        value = dict(cursor=512000, acceleration=dict(executor='retained'))
        write = Mock(side_effect=[self.windows_error(code) for code in (5, 32, 33)]+[None])
        with patch.object(runtime.time, 'sleep') as sleep, redirect_stderr(io.StringIO()):
            runtime.retry_atomic_json(write, Path('checkpoint.json'), value)
        self.assertEqual(write.call_count, 4)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], list(runtime.REPLACE_DELAYS[:3]))
        for call in write.call_args_list:
            self.assertIs(call.args[1], value)

    def test_permanent_windows_lock_is_bounded_and_preserves_checkpoint(self):
        with tempfile.TemporaryDirectory() as temp:
            checkpoint = Path(temp)/'checkpoint.json'
            runtime.archive.atomic_json(checkpoint, dict(cursor=100))
            before = checkpoint.read_bytes()
            error = self.windows_error(5)
            with patch.object(Path, 'replace', side_effect=error) as replace, \
                 patch.object(runtime.time, 'sleep') as sleep, redirect_stderr(io.StringIO()):
                with self.assertRaises(PermissionError) as raised:
                    runtime.retry_atomic_json(runtime.archive.atomic_json, checkpoint, dict(cursor=200))
            self.assertIs(raised.exception, error)
            self.assertEqual(replace.call_count, len(runtime.REPLACE_DELAYS)+1)
            self.assertEqual(sleep.call_count, len(runtime.REPLACE_DELAYS))
            self.assertEqual(checkpoint.read_bytes(), before)
            self.assertEqual(runtime.archive.read_json(checkpoint.with_suffix('.json.partial')), dict(cursor=200))

    def test_other_errors_fail_immediately(self):
        for error in (PermissionError('POSIX denied'), self.windows_error(2), OSError('disk full'), ValueError('bad JSON')):
            write = Mock(side_effect=error)
            with patch.object(runtime.time, 'sleep') as sleep:
                with self.assertRaises(type(error)):
                    runtime.retry_atomic_json(write, Path('checkpoint.json'), {})
            write.assert_called_once()
            sleep.assert_not_called()

    @unittest.skipUnless(sys.platform == 'win32', 'Windows sharing semantics')
    def test_real_windows_reader_lock_retries_atomic_replacement(self):
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.CreateFileW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                      wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
        kernel.CreateFileW.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        with tempfile.TemporaryDirectory() as temp:
            checkpoint = Path(temp)/'checkpoint.json'
            runtime.archive.atomic_json(checkpoint, dict(cursor=100))
            before = checkpoint.read_bytes()
            handle = kernel.CreateFileW(str(checkpoint), 0x80000000, 1, None, 3, 0x80, None)
            self.assertNotEqual(handle, ctypes.c_void_p(-1).value)
            def unlock(_):
                nonlocal handle
                self.assertEqual(checkpoint.read_bytes(), before)
                self.assertTrue(kernel.CloseHandle(handle))
                handle = None
            try:
                with patch.object(runtime.time, 'sleep', side_effect=unlock) as sleep, \
                     redirect_stderr(io.StringIO()):
                    runtime.retry_atomic_json(runtime.archive.atomic_json, checkpoint, dict(cursor=200))
                sleep.assert_called_once()
                self.assertEqual(runtime.archive.read_json(checkpoint), dict(cursor=200))
                self.assertFalse(checkpoint.with_suffix('.json.partial').exists())
            finally:
                if handle is not None:
                    kernel.CloseHandle(handle)

    def test_patch_covers_imported_aliases_and_private_writer_then_restores(self):
        originals = {module:module.atomic_json for module in (runtime.archive, runtime.cluster, runtime.production)}
        with self.assertRaisesRegex(RuntimeError, 'stopped'):
            with runtime.resilient_json_writes():
                for module, original in originals.items():
                    self.assertIsNot(module.atomic_json, original)
                spec = importlib.util.spec_from_file_location('_test_private_writer', runtime.production.__file__)
                private = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(private)
                self.assertIs(private.atomic_json, runtime.archive.atomic_json)
                raise RuntimeError('stopped')
        for module, original in originals.items():
            self.assertIs(module.atomic_json, original)

    def test_local_dispatch_wraps_both_backends_and_preserves_other_commands(self):
        processes = SimpleNamespace(Popen=Mock(return_value='process'), PIPE=-1)
        proxy = runtime.WorkerProcesses(processes)
        for name in runtime.WORKER_SCRIPTS:
            command = [sys.executable, '-u', str(runtime.source_path(name)), 'worker', '--key', 't/CH19']
            self.assertEqual(proxy.Popen(command, stdout=-1), 'process')
            self.assertEqual(processes.Popen.call_args.args[0], [*command[:2], str(Path(runtime.__file__).resolve()), name, *command[3:]])
            self.assertEqual(processes.Popen.call_args.kwargs, dict(stdout=-1))
        for command in (['ssh', 'host', 'uptime'], [sys.executable, '-u', str(runtime.source_path(runtime.WORKER_SCRIPTS[0])), 'prepare']):
            proxy.Popen(command)
            self.assertEqual(processes.Popen.call_args.args[0], command)
        self.assertEqual(proxy.PIPE, -1)

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
        original_worker = runtime.cluster.worker
        arguments = ['runtime', 'denoise_v2_cluster.py', 'worker', '--config', 'config']
        def run():
            self.assertIs(runtime.cluster.release_acknowledged, runtime.release_acknowledged)
            self.assertIsNot(runtime.archive.atomic_json, original_atomic)
            self.assertIsNot(runtime.cluster.worker, original_worker)
            self.assertEqual(sys.argv[1:], arguments[2:])
            return 75
        original_atomic = runtime.archive.atomic_json
        from contextlib import nullcontext
        with (patch.object(sys, 'argv', arguments), patch.object(runtime.cluster, 'main', side_effect=run),
              patch.object(runtime.cluster, 'read_json', return_value={}),
              patch.object(runtime, 'unity_outputs', return_value=nullcontext())):
            self.assertEqual(runtime.main(), 75)
            self.assertIs(sys.argv, arguments)
        self.assertIs(runtime.cluster.release_acknowledged, original_cleanup)
        self.assertIs(runtime.cluster.worker, original_worker)
        self.assertIs(runtime.archive.atomic_json, original_atomic)

    def test_accelerated_worker_keeps_exit_code_and_restores_patch(self):
        original_cleanup = runtime.cluster.release_acknowledged
        original_worker = runtime.cluster.worker
        arguments = ['runtime', 'denoise_v2_accelerated.py', 'worker', '--config', 'config']
        def run():
            self.assertIs(runtime.cluster.release_acknowledged, runtime.release_acknowledged)
            self.assertIsNot(runtime.archive.atomic_json, original_atomic)
            self.assertIsNot(runtime.cluster.worker, original_worker)
            self.assertEqual(Path(sys.argv[0]).name, arguments[1])
            raise SystemExit(75)
        original_atomic = runtime.archive.atomic_json
        from contextlib import nullcontext
        import denoise_v2_accelerated as accelerated
        with (patch.object(sys, 'argv', arguments), patch.object(accelerated, 'main', side_effect=run),
              patch.object(runtime.cluster, 'read_json', return_value={}),
              patch.object(runtime, 'unity_outputs', return_value=nullcontext())):
            with self.assertRaises(SystemExit) as exit_status:
                runtime.main()
            self.assertEqual(exit_status.exception.code, 75)
            self.assertIs(sys.argv, arguments)
        self.assertIs(runtime.cluster.release_acknowledged, original_cleanup)
        self.assertIs(runtime.cluster.worker, original_worker)
        self.assertIs(runtime.archive.atomic_json, original_atomic)


if __name__ == '__main__':
    unittest.main()
