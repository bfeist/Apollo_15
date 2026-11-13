"""Unity-gain output must resume frozen checkpoints and preserve older results."""
from contextlib import contextmanager
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
import soundfile as sf

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from _bootstrap import install as _install
_install()
import denoise_archive as archive
import denoise_v2_accelerated as accelerated
import denoise_v2_cluster as cluster
from denoise_v2_gain import configuration
import denoise_v2_output_policy as output
import denoise_v2_production as production
from denoise_v2_rollout import marker


class OutputPolicyTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()
        self.root, self.work, self.originals, self.hints, self.session = (
            self.base/name for name in ('output', 'work', 'originals', 'hints', 'cluster'))
        self.root.mkdir()
        self.session.mkdir()
        self.freeze = dict(run_id='test-policy', implementation_sha256={},
                           normalization=configuration(), channel_policy=archive.ALL_NUMBERED,
                           audio_naming=archive.CHANNEL_WAV)
        self.config = dict(frozen=self.freeze, remote_data='/mnt/d', adapter_sha256='test')
        archive.atomic_json(self.root/archive.MARKER, marker(self.freeze))

    def source(self, samples):
        tape = 'A15_T510083'
        path = self.originals/tape/f'{tape}_HR1U_CH20_VOICE_210-00-00-00_210-00-03-00.wav'
        path.parent.mkdir(parents=True)
        sf.write(path, samples, 8000, subtype='FLOAT')
        stat = path.stat()
        job = dict(tape=tape, channel=20, action='denoise_v2', source_frame_interval=[0,len(samples)],
                   source=dict(channel=20, path=str(path), bytes=stat.st_size, mtime_ns=stat.st_mtime_ns,
                               sample_rate=8000, frames=len(samples), role='reference_and_target', start_seconds=3))
        hint = self.hints/production.job_key(job)/'cleanup.jsonl'
        hint.parent.mkdir(parents=True)
        hint.touch()
        job['hints'] = dict(available=True, sha256=archive.sha256(hint))
        return job

    def write(self, job, writer=None):
        return (writer or production.write_channel)(job, self.freeze, self.root, self.work, self.hints, None,
            originals=self.originals, resume=True, progress=lambda _:None)

    @contextmanager
    def policy(self):
        with (patch.object(cluster, 'check_code'),
              patch.object(cluster, 'session_path', return_value=self.session)):
            with output.unity_outputs(self.config, prepare=True) as policy:
                yield policy

    def test_unity_output_has_identical_samples_without_gain_scan_or_second_write(self):
        samples = np.full(16000, 1.125, dtype='<f4')
        job = self.source(samples)
        with (self.policy(),
              patch.object(production, 'measure_gain', side_effect=AssertionError('scanned gain'))):
            result = self.write(job)
            folder = self.root/production.job_key(job)
            self.assertEqual(result['gain'], 1.)
            self.assertEqual(result['normalization']['method'], output.METHOD)
            self.assertEqual(result['audio']['peak'], 1.125)
            self.assertEqual(result['audio']['sha256'], result['normalization']['unity_audio_sha256'])
            np.testing.assert_array_equal(sf.read(folder/'CH20.wav', dtype='float32')[0], samples)
            self.assertFalse((folder/'unity.wav').exists())
            self.assertEqual(cluster.verify_completed(folder, job, self.freeze, full=True), result)
            with patch.object(production, 'render_float', side_effect=AssertionError('rerendered')):
                self.assertEqual(self.write(job), result)

    def test_accelerated_private_writer_resumes_unchanged_frozen_prefix(self):
        samples = np.full(production.STEP*2+8000, .125, dtype='<f4')
        job = self.source(samples)
        original = production.corrected_chunks

        def interrupted(*args, **kwargs):
            yield next(original(*args, **kwargs))
            raise InterruptedError('test checkpoint')

        with patch.object(production, 'corrected_chunks', side_effect=interrupted):
            with self.assertRaisesRegex(InterruptedError, 'test checkpoint'):
                self.write(job)
        stage = self.work/production.job_key(job)
        checkpoint = (stage/'checkpoint.json').read_bytes()
        committed = archive.read_json(stage/'checkpoint.json')
        prefix = (stage/'unity.wav').read_bytes()
        self.assertEqual(committed['cursor'], production.STEP)
        with self.policy():
            writer = accelerated.private_writer('numba-'+'a'*24, {}, implementation='reference')
            private = sys.modules['_apollo15_accelerated_writer']
            with patch.object(private, 'measure_gain', side_effect=AssertionError('scanned gain')):
                result = self.write(job, writer)
            folder = self.root/production.job_key(job)
            self.assertEqual(result['gain'], 1.)
            self.assertEqual(result['normalization']['method'], output.METHOD)
            self.assertEqual((folder/'CH20.wav').read_bytes()[80:len(prefix)], prefix[80:])
            self.assertEqual(committed['identity']['frozen'], self.freeze)
            self.assertIn(checkpoint, [p.read_bytes() for p in folder.glob('checkpoint.before-*.json')])
            np.testing.assert_array_equal(sf.read(folder/'CH20.wav', dtype='float32')[0], samples)
            cluster.verify_completed(folder, job, self.freeze, full=True)

    def test_old_normalized_output_remains_immutable_and_uses_original_verifier(self):
        job = self.source(np.full(16000, .125, dtype='<f4'))
        old = self.write(job)
        folder = self.root/production.job_key(job)
        before = {p.name:p.read_bytes() for p in folder.iterdir()}
        self.assertNotEqual(old['gain'], 1.)
        with self.policy():
            self.assertEqual(self.write(job), old)
            self.assertEqual(cluster.verify_completed(folder, job, self.freeze, full=True), old)
        self.assertEqual({p.name:p.read_bytes() for p in folder.iterdir()}, before)

    def test_policy_and_manifest_tampering_are_rejected(self):
        job = self.source(np.full(16000, .125, dtype='<f4'))
        with self.policy():
            self.write(job)
            folder = self.root/production.job_key(job)
            record = archive.read_json(folder/'denoised.json')
            record['gain'] = 2.
            archive.atomic_json(folder/'denoised.json', record)
            with self.assertRaisesRegex(production.IdentityError, 'Incompatible unity-gain'):
                cluster.verify_completed(folder, job, self.freeze)
            path = self.session/'output_policy.json'
            descriptor = archive.read_json(path)
            descriptor['implementation_sha256'] = 'wrong'
            archive.atomic_json(path, descriptor)
            with self.assertRaisesRegex(production.IdentityError, 'output policy changed'):
                cluster.check_code(self.config)

    @unittest.skipUnless(sys.platform == 'win32', 'Publication is Windows-owned')
    def test_remote_unity_output_is_verified_adopted_and_acknowledged(self):
        job = self.source(np.full(16000, .125, dtype='<f4'))
        remote = dict(self.freeze, run_id='test-remote')
        archive.atomic_json(self.root/archive.MARKER, marker(remote))
        canonical = self.base/'canonical'
        canonical.mkdir()
        archive.atomic_json(canonical/archive.MARKER, marker(self.freeze))
        with self.policy():
            result = production.write_channel(job, remote, self.root, self.work, self.hints, None,
                originals=self.originals, resume=True, progress=lambda _:None)
            mapped = dict(job, source=dict(job['source'], path='/mnt/d/original.wav'))
            result.update(source=mapped['source'],
                signature=production.new_record(mapped, remote, True)['signature'])
            key = production.job_key(job)
            result['hint_provenance']['relative_path'] = (key/'cleanup.jsonl').as_posix()
            inbox = self.session/'inbox'/key
            inbox.parent.mkdir(parents=True)
            (self.root/key).rename(inbox)
            archive.atomic_json(inbox/'denoised.json', result)
            archive.atomic_json(inbox/'genus.execution.json', result)
            archive.atomic_json(self.session/'qualification.json', {'test':True})
            with (patch.object(cluster, 'CANONICAL', canonical),
                  patch.object(cluster, 'ARCHIVE', self.hints),
                  patch.object(cluster, 'mapped_job', return_value=mapped),
                  patch.object(cluster, 'validate_source',
                      side_effect=lambda j: production.validate_source(j, self.originals))):
                adopted = cluster.adopt_remote(self.config, job, dict(frozen=remote))
                self.assertEqual(adopted['gain'], 1.)
                self.assertEqual(adopted['normalization']['method'], output.METHOD)
                self.assertEqual(adopted['audio']['sha256'], result['audio']['sha256'])
                cluster.verify_completed(canonical/key, job, self.freeze, full=True)
                self.assertEqual(cluster.adopt_remote(self.config, job, dict(frozen=remote)), adopted)
            self.assertTrue((self.session/'acks'/key.with_suffix('.json')).exists())


if __name__ == '__main__':
    unittest.main()
