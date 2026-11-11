"""Execution provenance, resume, dispatch and qualification failure checks."""
from pathlib import Path
import shlex
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

import numpy as np
import soundfile as sf

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from _bootstrap import install as _install
_install()
import denoise_archive as archive
import denoise_v2_accelerated as accelerated
import denoise_v2_production as production
from denoise_v2_rollout import marker

EXECUTION = 'numba-'+'a'*24


class AcceleratedTests(unittest.TestCase):
    def test_segments_retain_reference_prefix_and_coalesce_resumes(self):
        meta = accelerated.extend_segments(dict(cursor=100), 120, EXECUTION)
        self.assertEqual(meta['segments'], [dict(execution_id='reference', start_frame=0, end_frame=100),
                         dict(execution_id=EXECUTION, start_frame=100, end_frame=120)])
        resumed = accelerated.extend_segments(dict(cursor=120, acceleration=meta), 150, EXECUTION)
        self.assertEqual(len(resumed['segments']), 2)
        self.assertEqual(resumed['segments'][-1]['end_frame'], 150)
        resumed['segments'][0]['end_frame'] = 99
        with self.assertRaises(ValueError):
            accelerated.extend_segments(dict(cursor=150, acceleration=resumed), 160, EXECUTION)

    def test_worker_dispatch_preserves_unrelated_processes_and_remote_quoting(self):
        process_api = SimpleNamespace(Popen=Mock(return_value='process'))
        proxy = accelerated.WorkerProcesses(process_api, EXECUTION)
        proxy.Popen([sys.executable, '-u', str(accelerated.cluster.SCRIPT), 'worker', '--key', 't/CH20'])
        command = process_api.Popen.call_args.args[0]
        self.assertIn('denoise_v2_accelerated.py', command[2])
        self.assertEqual(command[-2:], ['--execution-id', EXECUTION])
        proxy.Popen(['ssh', 'host', 'uptime'])
        self.assertEqual(process_api.Popen.call_args.args[0], ['ssh', 'host', 'uptime'])
        config = dict(remote_repo='/repo with spaces', remote_python='/python old')
        execution = dict(identity=dict(remote_python='/python new'))
        original = ['ssh', 'host', shlex.join(['exec', 'uv', 'run', '--python', '/python old',
            '/repo with spaces/scripts/denoise_v2_cluster.py', 'worker', '--key', 't/CH20'])]
        command = accelerated.remote_worker_command(original, config, EXECUTION, execution)
        argv = shlex.split(command[-1])
        self.assertIn('/python new', argv)
        self.assertIn('/repo with spaces/scripts/denoise_v2_accelerated.py', argv)
        self.assertEqual(argv[-2:], ['--execution-id', EXECUTION])

    def test_incomplete_qualification_and_untrusted_execution_id_rejected(self):
        with self.assertRaises(ValueError):
            accelerated.validate_report(dict(passed=True, cases=[]), {})
        with self.assertRaises(ValueError):
            accelerated.execution_folder({}, '../escape')

    def test_resume_preserves_committed_audio_and_old_records_then_publishes_provenance(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp).resolve()
            originals, hints, root, work = [base/name for name in ('orig_concatenated', 'denoised_v1', 'output', 'work')]
            tape = 'A15_T510083'
            (originals/tape).mkdir(parents=True)
            source = originals/tape/f'{tape}_HR1U_CH20_VOICE_210-00-00-00_210-00-03-00.wav'
            frames = production.STEP*2+8000
            sf.write(source, np.zeros(frames), 8000, subtype='FLOAT')
            stat = source.stat()
            job = dict(tape=tape, channel=20, action='denoise_v2', source_frame_interval=[0,frames],
                       source=dict(channel=20, path=str(source), bytes=stat.st_size, mtime_ns=stat.st_mtime_ns,
                                   sample_rate=8000, frames=frames, role='reference_and_target', start_seconds=0))
            hint = hints/production.job_key(job)/'cleanup.jsonl'
            hint.parent.mkdir(parents=True)
            hint.touch()
            job['hints'] = dict(available=True, sha256=archive.sha256(hint))
            freeze = dict(run_id='test-base-contract', implementation_sha256={})
            root.mkdir()
            archive.atomic_json(root/archive.MARKER, marker(freeze))

            class StopAfterOne:
                def __init__(self):
                    self.checks = 0
                def exists(self):
                    self.checks += 1
                    return self.checks > 1

            arguments = (job, freeze, root, work, hints, None)
            options = dict(originals=originals, progress=lambda _:None, resume=True)
            with self.assertRaises(InterruptedError):
                production.write_channel(*arguments, **options, stop=StopAfterOne())
            stage = work/production.job_key(job)
            old_checkpoint = (stage/'checkpoint.json').read_bytes()
            old_manifest = (stage/'denoised.json').read_bytes()
            prefix = (stage/'denoised.wav').read_bytes()
            writer = accelerated.private_writer(EXECUTION, dict(identity='test'))
            with self.assertRaises(InterruptedError):
                writer(*arguments, **options, stop=StopAfterOne())
            self.assertEqual((stage/'denoised.wav').read_bytes()[:len(prefix)], prefix)
            self.assertEqual((stage/f'checkpoint.before-{EXECUTION}.json').read_bytes(), old_checkpoint)
            self.assertEqual((stage/f'denoised.before-{EXECUTION}.json').read_bytes(), old_manifest)
            record = writer(*arguments, **options)
            self.assertEqual(record['run_id'], freeze['run_id'])
            self.assertEqual(record['acceleration']['segments'], [
                dict(execution_id='reference', start_frame=0, end_frame=production.STEP),
                dict(execution_id=EXECUTION, start_frame=production.STEP, end_frame=frames)])
            final = root/production.job_key(job)
            samples, rate = sf.read(final/'denoised.wav', dtype='float32')
            np.testing.assert_array_equal(samples, np.zeros(frames))
            self.assertEqual(rate, 8000)
            production.verify_completed(final, job, freeze, full=True)
            before = (final/'denoised.json').read_bytes()
            writer(*arguments, **options)
            self.assertEqual((final/'denoised.json').read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
