"""Scheduling, platform identity and publication checks without archive writes."""
from copy import deepcopy
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
import denoise_v2_cluster as cluster
import denoise_v2_production as production
from denoise_archive import atomic_json, read_json, sha256
from denoise_v2_gain import configuration


class ClusterTests(unittest.TestCase):
    def job(self, tape='A15_T510083', channel=20):
        return dict(tape=tape, channel=channel, source=dict(
            path=f'D:\\Apollo_15_30-Track\\orig_concatenated\\{tape}\\{tape}_CH{channel}_VOICE.wav'))

    def test_path_translation_preserves_identity_and_rejects_escape(self):
        original = self.job()
        original['source'].update(bytes=128, mtime_ns=42)
        remote = cluster.mapped_job(original, '/mnt/d/Apollo_15_30-Track')
        self.assertEqual(remote['source']['path'],
            '/mnt/d/Apollo_15_30-Track/orig_concatenated/A15_T510083/A15_T510083_CH20_VOICE.wav')
        self.assertEqual(remote['source']['mtime_ns'], 42)
        self.assertTrue(original['source']['path'].startswith('D:'))
        original['source']['path'] = 'D:/elsewhere/audio.wav'
        with self.assertRaises(ValueError):
            cluster.mapped_job(original, '/mnt/d/Apollo_15_30-Track')

    def test_global_tape_limit_and_host_affinity(self):
        first, second, third = self.job('A15_T510071', 1), self.job('A15_T510072', 2), self.job('A15_T510073', 3)
        active = {'first': dict(job=first, host='windows')}
        self.assertIsNone(cluster.select_job([second, third], active, 'genus', 1, {}))
        self.assertEqual(cluster.select_job([second, third], active, 'genus', 2, {}), second)
        owners = {cluster.job_key(second).as_posix(): 'windows'}
        self.assertEqual(cluster.select_job([second, third], active, 'genus', 2, owners), third)
        self.assertEqual(cluster.select_job([third, second], active, 'windows', 2, owners), second)

    def test_probe_rejects_corruption_and_nonfinite(self):
        expected = np.array([0., .1, -.1], dtype='float32')
        self.assertEqual(cluster.compare_probe(expected, expected)['maximum_error'], 0)
        for actual in (expected+.01, np.array([0., np.nan, 0.]), expected[:1]):
            with self.assertRaises(ValueError):
                cluster.compare_probe(expected, actual)

    def test_ssh_paths_are_posix_and_shell_quoted(self):
        config = dict(ssh='bfeist@genus', remote_uv='/home/bfeist/.local/bin/uv',
                      remote_python='/home/bfeist/env/bin/python', remote_repo='/mnt/f/repo with spaces')
        command = cluster.remote_command(config, 'worker', '--key', 'A15_T510083/CH20')
        self.assertIn("'/mnt/f/repo with spaces/scripts/denoise_v2_cluster.py'", command[-1])
        self.assertNotIn('\\', command[-1])
        self.assertIn('--no-project', command[-1])

    def test_workers_only_read_prepared_scratch_marker(self):
        from concurrent.futures import ThreadPoolExecutor
        with tempfile.TemporaryDirectory() as temporary:
            config = dict(scratch=str(Path(temporary).resolve()))
            frozen = dict(run_id='remote-test')
            scratch, root, work = cluster.scratch_paths(config, frozen)
            with self.assertRaisesRegex(ValueError, 'not prepared'):
                cluster.check_scratch(config, frozen)
            root.mkdir(parents=True)
            atomic_json(root/cluster.MARKER, cluster.marker(frozen))
            before = (root/cluster.MARKER).stat().st_mtime_ns
            with ThreadPoolExecutor(max_workers=16) as pool:
                results = list(pool.map(lambda _: cluster.check_scratch(config, frozen), range(64)))
            self.assertTrue(all(row == (scratch, root, work) for row in results))
            self.assertEqual(before, (root/cluster.MARKER).stat().st_mtime_ns)

    @unittest.skipUnless(sys.platform == 'win32', 'Publication is Windows-owned')
    def test_remote_adoption_preserves_audio_and_legacy_cache_compatibility(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            canonical, originals, archive = base/'denoised', base/'orig_concatenated', base/'denoised_v1'
            session = base/'control'/'run'/'cluster'
            session.mkdir(parents=True)
            canonical.mkdir()
            job = self.job()
            source = originals/job['tape']/f'{job["tape"]}_CH20_VOICE.wav'
            source.parent.mkdir(parents=True)
            sf.write(source, np.sin(np.arange(8000)*.2)*.02, 8000, subtype='FLOAT')
            stat = source.stat()
            job['source'].update(path=str(source), bytes=stat.st_size, mtime_ns=stat.st_mtime_ns,
                frames=8000, sample_rate=8000, role='reference_and_target', channel=20, start_seconds=3)
            job.update(action='denoise_v2', source_frame_interval=[0, 8000])
            key = cluster.job_key(job)
            hint = archive/key/'cleanup.jsonl'
            hint.parent.mkdir(parents=True)
            hint.touch()
            job['hints'] = dict(available=True, sha256=sha256(hint))
            frozen = dict(run_id='test-production', implementation_sha256={}, normalization=configuration(),
                          channel_policy='all_numbered', audio_naming='channel_number')
            remote_frozen = dict(frozen, run_id='test-genus', remote_execution=dict(hostname='genus'))
            atomic_json(canonical/cluster.MARKER, cluster.marker(frozen))
            remote_root, work = base/'remote', base/'remote-work'
            remote_root.mkdir()
            atomic_json(remote_root/cluster.MARKER, cluster.marker(remote_frozen))
            result = production.write_channel(job, remote_frozen, remote_root, work, archive, None,
                                               originals=originals, progress=lambda _: None)
            mapped = deepcopy(job)
            mapped['source']['path'] = '/mnt/d/original.wav'
            result.update(source=mapped['source'], signature=production.new_record(mapped, remote_frozen, True)['signature'])
            result['hint_provenance']['relative_path'] = (key/'cleanup.jsonl').as_posix()
            inbox = session/'inbox'/key
            inbox.parent.mkdir(parents=True)
            (remote_root/key).rename(inbox)
            atomic_json(inbox/'denoised.json', result)
            atomic_json(inbox/'genus.execution.json', result)
            atomic_json(session/'qualification.json', {'test': True})
            qualification = dict(frozen=remote_frozen)
            config = dict(frozen=frozen, remote_data='/mnt/d', adapter_sha256='test')
            with (patch.object(cluster, 'session_path', return_value=session),
                  patch.object(cluster, 'CANONICAL', canonical),
                  patch.object(cluster, 'ARCHIVE', archive),
                  patch.object(cluster, 'mapped_job', return_value=mapped),
                  patch.object(cluster, 'validate_source', side_effect=lambda j: production.validate_source(j, originals))):
                log = inbox/'cleanup.jsonl'
                good_log = log.read_bytes()
                log.write_bytes(good_log+b'corruption')
                with self.assertRaisesRegex(ValueError, 'cleanup log changed'):
                    cluster.adopt_remote(config, job, qualification)
                self.assertFalse((canonical/key).exists())
                log.write_bytes(good_log)
                bad_record = deepcopy(result)
                bad_record['frozen']['run_id'] = 'unqualified'
                atomic_json(inbox/'denoised.json', bad_record)
                with self.assertRaisesRegex(ValueError, 'assigned job/environment'):
                    cluster.adopt_remote(config, job, qualification)
                self.assertFalse((canonical/key).exists())
                atomic_json(inbox/'denoised.json', result)
                adopted = cluster.adopt_remote(config, job, qualification)
                self.assertEqual(adopted['audio']['sha256'], result['audio']['sha256'])
                self.assertEqual(adopted['frozen'], frozen)
                self.assertEqual(adopted['execution']['frozen'], remote_frozen)
                production.verify_completed(canonical/key, job, frozen, full=True)
                again = cluster.adopt_remote(config, job, qualification)
                self.assertEqual(again, adopted)
                self.assertEqual(read_json(inbox/'denoised.json')['frozen'], remote_frozen)
                self.assertTrue((session/'acks'/key.with_suffix('.json')).is_file())


if __name__ == '__main__':
    unittest.main()
