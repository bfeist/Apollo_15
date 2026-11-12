"""Exercise persistent reuse and invalidation with real, tiny WAV artifacts."""
import os
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import numpy as np
import soundfile as sf

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from _bootstrap import install as _install
_install()
import denoise_v2_verification as verification

cluster, production = verification.cluster, verification.production


class VerificationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        base = Path(temporary.name).resolve()
        self.session = base/'control'
        self.session.mkdir()
        canonical = base/'denoised'
        self.output = canonical/'TAPE'/'CH01'
        self.output.mkdir(parents=True)
        self.source = base/'source.wav'
        audio = self.output/'CH01.wav'
        sf.write(self.source, np.zeros(800), 8000, subtype='FLOAT')
        sf.write(audio, np.zeros(800), 8000, subtype='FLOAT')
        info = self.source.stat()
        self.job = dict(tape='TAPE', channel=1, action='denoise_v2',
            source_frame_interval=[0, 800], hints=dict(sha256='test-hint'),
            source=dict(path=str(self.source), bytes=info.st_size, mtime_ns=info.st_mtime_ns,
                        channel=1, frames=800, sample_rate=8000, start_seconds=0,
                        role='reference_and_target'))
        self.freeze = dict(run_id='test', audio_naming='channel_number', channel_policy='all_numbered')
        self.config = dict(frozen=self.freeze, jobs_sha256='test')
        cluster.atomic_json(canonical/cluster.MARKER, cluster.marker(self.freeze))
        log = self.output/'cleanup.jsonl'
        log.write_text('{}\n', encoding='utf-8')
        record = production.new_record(self.job, self.freeze, True)
        info = audio.stat()
        record.update(status='complete', cleanup_log_sha256=cluster.sha256(log),
            audio=dict(filename=audio.name, bytes=info.st_size, mtime_ns=info.st_mtime_ns,
                       frames=800, sample_rate=8000, format='WAV', subtype='FLOAT',
                       sha256=cluster.sha256(audio)))
        cluster.atomic_json(self.output/'denoised.json', record)
        self.record = record
        for attribute, value in (('CANONICAL', canonical), ('session_path', lambda _: self.session)):
            patcher = patch.object(cluster, attribute, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def cache(self, verify=None, config=None):
        return verification.CompletedVerification(config or self.config,
            verify if verify is not None else production.verify_completed)

    def warm(self):
        cache = self.cache()
        self.assertEqual(cache.verify(self.output, self.job, self.freeze), self.record)
        cache.close()

    def test_second_launch_reuses_success_and_full_check_still_verifies(self):
        self.warm()
        verify = Mock(wraps=production.verify_completed)
        cache = self.cache(verify)
        self.assertEqual(cache.verify(self.output, self.job, self.freeze), self.record)
        verify.assert_not_called()
        self.assertEqual((cache.checked, cache.cached, cache.verified), (1, 1, 0))
        cache.verify(self.output, self.job, self.freeze, full=True)
        self.assertTrue(verify.call_args.kwargs['full'])
        cache.close()

    def test_changed_cleanup_is_rechecked_and_rejected(self):
        self.warm()
        (self.output/'cleanup.jsonl').write_text('changed evidence\n', encoding='utf-8')
        cache = self.cache()
        with self.assertRaisesRegex(ValueError, 'Cleanup evidence changed'):
            cache.verify(self.output, self.job, self.freeze)
        cache.close()
        self.assertNotIn('TAPE/CH01', cluster.read_json(cache.path)['entries'])

    def test_explicit_full_check_detects_audio_content_with_preserved_size_and_mtime(self):
        self.warm()
        path = self.output/'CH01.wav'
        info = path.stat()
        with path.open('r+b') as stream:
            stream.seek(-4, 2)
            stream.write(struct.pack('<f', .1))
        os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns))
        with self.assertRaisesRegex(ValueError, 'Completed audio bytes changed'):
            self.cache().verify(self.output, self.job, self.freeze, full=True)

    def test_changed_wav_metadata_is_rechecked_and_rejected(self):
        self.warm()
        with (self.output/'CH01.wav').open('ab') as stream:
            stream.write(b'changed')
        with self.assertRaisesRegex(ValueError, 'output identity mismatch'):
            self.cache().verify(self.output, self.job, self.freeze)

    def test_changed_source_is_rechecked_and_rejected(self):
        self.warm()
        with self.source.open('ab') as stream:
            stream.write(b'changed')
        with self.assertRaisesRegex(ValueError, 'output identity mismatch'):
            self.cache().verify(self.output, self.job, self.freeze)

    def test_changed_manifest_or_marker_is_rejected(self):
        for target in ('manifest', 'marker'):
            with self.subTest(target=target):
                self.warm()
                path = (self.output/'denoised.json' if target == 'manifest' else
                        cluster.CANONICAL/cluster.MARKER)
                original = cluster.read_json(path)
                cluster.atomic_json(path, dict(original, run_id='different'))
                with self.assertRaises(ValueError):
                    self.cache().verify(self.output, self.job, self.freeze)
                cluster.atomic_json(path, original)

    def test_changed_job_or_freeze_cannot_reuse_cache(self):
        self.warm()
        for job, freeze in ((dict(self.job, source_frame_interval=[1, 800]), self.freeze),
                            (self.job, dict(self.freeze, run_id='different'))):
            with self.assertRaisesRegex(ValueError, 'Incompatible completed'):
                self.cache().verify(self.output, job, freeze)

    def test_missing_corrupt_or_old_cache_falls_back_to_verification(self):
        for contents in (None, '{broken', '[]', '{"identity":"old","entries":{}}'):
            with self.subTest(contents=contents):
                path = self.session/'verification_cache.json'
                if contents is None:
                    path.unlink(missing_ok=True)
                else:
                    path.write_text(contents, encoding='utf-8')
                verify = Mock(wraps=production.verify_completed)
                cache = self.cache(verify)
                cache.verify(self.output, self.job, self.freeze)
                verify.assert_called_once()
                cache.close()

    def test_config_change_invalidates_cache(self):
        self.warm()
        verify = Mock(wraps=production.verify_completed)
        cache = self.cache(verify, dict(self.config, jobs_sha256='new'))
        cache.verify(self.output, self.job, self.freeze)
        verify.assert_called_once()

    def test_file_change_during_verification_does_not_create_cache_entry(self):
        def verify(*args, **kwargs):
            result = production.verify_completed(*args, **kwargs)
            (self.output/'cleanup.jsonl').write_text('changed after verification', encoding='utf-8')
            return result
        cache = self.cache(verify)
        cache.verify(self.output, self.job, self.freeze)
        self.assertEqual(cache.entries, {})

    def test_adapter_restores_hooks_and_publication_always_rechecks(self):
        self.warm()
        verify = Mock(wraps=production.verify_completed)
        select = Mock(return_value=None)
        with patch.object(cluster, 'verify_completed', verify), patch.object(cluster, 'select_job', select):
            with self.assertRaisesRegex(RuntimeError, 'stopped'):
                with verification.cached_verification(self.config):
                    cluster.verify_completed(self.output, self.job, self.freeze)
                    verify.assert_not_called()
                    cluster.select_job([], {}, 'windows', 3, {})
                    cluster.verify_completed(self.output, self.job, self.freeze)
                    verify.assert_called_once()
                    raise RuntimeError('stopped')
            self.assertIs(cluster.verify_completed, verify)
            self.assertIs(cluster.select_job, select)
        state = cluster.read_json(self.session/'startup.json')
        self.assertEqual((state['checked'], state['cached']), (1, 1))

    def test_cache_write_failure_does_not_fail_verification(self):
        cache = self.cache()
        with patch.object(cluster, 'atomic_json', side_effect=OSError('read only')):
            self.assertEqual(cache.verify(self.output, self.job, self.freeze), self.record)
            cache.close()


if __name__ == '__main__':
    unittest.main()
