import hashlib
import json
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
from denoise_v2 import corrected_chunks
from denoise_v2_batch import prepare_output
from denoise_v2_gain import Settings as GainSettings, configuration as gain_configuration, measure as measure_gain
from denoise_v2_production import (STEP, IdentityError, job_key, validate_source, wav_header,
                                   write_channel)
from denoise_v2_quality import compare_review
from denoise_v2_rollout import archive_snapshot, marker, rollback, rotate, supersede_timing_copy_root
from channel_review import completed_denoised, ensure_denoised
from review_sampling import verify_bindings
from v5_resume import resume_root

RATE = 8000
TAPE = 'A15_T510083'


class ProductionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()
        self.originals = self.base/'orig_concatenated'
        (self.originals/TAPE).mkdir(parents=True)
        self.hints = self.base/'denoised_v1'
        self.output, self.work = self.base/'denoised', self.base/'staging'
        self.output.mkdir()
        self.freeze = {'run_id':'test-frozen', 'implementation_sha256':{}}
        archive.atomic_json(self.output/archive.MARKER, marker(self.freeze))

    def source(self, samples, timing=False):
        channel = 1 if timing else 20
        role = 'IRIG' if timing else 'VOICE'
        path = self.originals/TAPE/f'{TAPE}_HR1U_CH{channel}_{role}_210-00-00-00_210-00-01-00.wav'
        sf.write(path, samples, RATE, subtype='PCM_16' if timing else 'FLOAT')
        stat = path.stat()
        row = dict(channel=channel, path=str(path), bytes=stat.st_size, mtime_ns=stat.st_mtime_ns,
                   sample_rate=RATE, frames=len(samples), role='timing_only' if timing else 'reference_and_target',
                   start_seconds=3)
        job = dict(tape=TAPE, channel=channel, source=row, source_frame_interval=[0,len(samples)],
                   action='copy_timing_original' if timing else 'denoise_v2')
        if not timing:
            hint = self.hints/job_key(job)/'cleanup.jsonl'
            hint.parent.mkdir(parents=True)
            hint.touch()
            job['hints'] = dict(available=True, sha256=archive.sha256(hint))
        return job

    def write(self, job, resume=False):
        return write_channel(job, self.freeze, self.output, self.work, self.hints, None,
                             originals=self.originals, resume=resume, progress=lambda _: None)

    def test_float_preserves_over_full_scale_and_rf64_decodes(self):
        job = self.source(np.full(2*RATE, 1.125))
        record = self.write(job)
        self.assertEqual(record['canonical_start_offset_frames'], 3*RATE)
        self.assertEqual(record['audio']['peak'], 1.125)
        self.assertEqual(record['audio']['samples_above_full_scale'], 2*RATE)
        self.assertEqual(record['audio']['format'], 'WAV')
        rf64 = self.base/'test_rf64.wav'
        samples = np.array([1.125, -.25, 0], dtype='<f4')
        rf64.write_bytes(wav_header(len(samples), RATE, force_rf64=True)+samples.tobytes())
        np.testing.assert_array_equal(sf.read(rf64, dtype='float32')[0], samples)
        self.assertEqual(sf.info(rf64).format, 'RF64')
        self.assertEqual(wav_header(2**30, RATE)[:4], b'RF64')

    def test_original_timing_copy_and_version_refusal(self):
        self.freeze['normalization'] = gain_configuration()
        archive.atomic_json(self.output/archive.MARKER, marker(self.freeze))
        job = self.source(np.arange(2*RATE)/RATE-.5, timing=True)
        row = job['source']
        record = self.write(job)
        folder = self.output/job_key(job)
        self.assertEqual(record['action'], 'copy_original_timing')
        self.assertEqual(record['gain'], 1.)
        self.assertNotIn('normalization', record)
        self.assertEqual((folder/'denoised.wav').read_bytes(), Path(row['path']).read_bytes())
        self.assertEqual(archive.read_json(folder/'cleanup.jsonl')['kind'], 'bypass')
        self.assertTrue(completed_denoised(row, folder, 0, row['frames'], version='v2'))
        self.assertFalse(completed_denoised(row, folder, 0, row['frames'], version='v1'))
        with self.assertRaisesRegex(ValueError, 'V2 denoise cache'):
            ensure_denoised(row, folder, 0, row['frames'], version='v1')
        with self.assertRaisesRegex(ValueError, 'V2 denoise cache'):
            ensure_denoised(row, folder.with_name('CH02'), 0, row['frames'])
        self.assertEqual(ensure_denoised(row, folder, 0, row['frames'])['action'], 'copy_original_timing')
        with self.assertRaisesRegex(ValueError, 'Legacy cleanup'):
            with archive.legacy_write(self.hints/'tape'/'new'):
                self.fail('Archive must stay read-only')
        with self.assertRaisesRegex(ValueError, 'separate run-owned'):
            write_channel(job, self.freeze, self.output, self.hints/'scratch', self.hints, None,
                          originals=self.originals)
        with self.assertRaisesRegex(IdentityError, 'Original source identity'):
            validate_source(job, self.output)

    def test_timing_label_runs_the_normalized_denoise_writer(self):
        self.freeze.update(channel_policy=archive.ALL_NUMBERED, normalization=gain_configuration(),
                           audio_naming=archive.CHANNEL_WAV)
        archive.atomic_json(self.output/archive.MARKER, marker(self.freeze))
        job = self.source(np.full(12*RATE, .25, dtype='<f4'), timing=True)
        hint = self.hints/job_key(job)/'cleanup.jsonl'
        hint.parent.mkdir(parents=True)
        hint.touch()
        job.update(action='denoise_v2', hints=dict(available=True, sha256=archive.sha256(hint)))
        record = self.write(job)
        folder = self.output/job_key(job)
        self.assertEqual(record['action'], 'denoise_v2')
        self.assertEqual(record['source']['role'], 'timing_only')
        self.assertEqual(record['audio']['subtype'], 'FLOAT')
        self.assertEqual(record['audio']['filename'], 'CH01.wav')
        self.assertAlmostEqual(record['normalization']['program_output_peak'], 10**(-3/20))
        row = job['source']
        self.assertTrue(completed_denoised(row, folder, 0, row['frames'], version='v2'))
        self.assertEqual(archive.denoised_audio_path(folder), folder/'CH01.wav')
        self.assertEqual(ensure_denoised(row, folder, 0, row['frames'])['file'], str(folder/'CH01.wav'))
        self.assertFalse((folder/'denoised.wav').exists())
        self.assertNotEqual(archive.sha256(folder/'CH01.wav'), archive.sha256(row['path']))

    def test_publish_revised_channel_in_existing_folder(self):
        prior = self.freeze['run_id']
        job = self.source(np.full(12*RATE, .25, dtype='<f4'), timing=True)
        before = self.write(job)
        folder = self.output/job_key(job)
        hint = self.hints/job_key(job)/'cleanup.jsonl'
        hint.parent.mkdir(parents=True)
        hint.touch()
        job.update(action='denoise_v2', hints=dict(available=True, sha256=archive.sha256(hint)))
        self.freeze = dict(self.freeze, run_id='current', channel_policy=archive.ALL_NUMBERED,
                           normalization=gain_configuration(), audio_naming=archive.CHANNEL_WAV)
        archive.atomic_json(self.output/archive.MARKER, marker(self.freeze))
        record = write_channel(job, self.freeze, self.output, self.work, self.hints, None,
                               originals=self.originals, previous_run_id=prior, progress=lambda _: None)
        self.assertEqual(record['audio']['filename'], 'CH01.wav')
        self.assertEqual(archive.sha256(folder/'denoised.wav'), before['audio']['sha256'])
        self.assertEqual(archive.read_json(folder/f'denoised.{prior}.json'), before)
        self.assertTrue(completed_denoised(job['source'], folder, 0, record['audio']['frames']))

    def test_resume_rebuilds_compact_neighbor_and_rejects_incompatible_checkpoint(self):
        t = np.arange(STEP+RATE*8)/RATE
        samples = (.02*np.sin(2*np.pi*180*t) + .01*np.sin(2*np.pi*480*t)).astype('<f4')
        job = self.source(samples)
        stage = self.work/job_key(job)

        def fit(block, seeds, settings, defer_compact):
            self.assertTrue(defer_compact)
            bank = dict(kind='compact_low_residual', harmonic=3, hz=180, true_absence_intervals_seconds=[])
            # The real compact projection runs inside corrected_chunks.
            return np.zeros_like(block), dict(components=[bank])

        def interrupt(*args, **kwargs):
            yield next(corrected_chunks(*args, **kwargs))
            raise RuntimeError('simulated interruption')

        with patch('denoise_v2.model_window', side_effect=fit):
            with patch('denoise_v2_production.corrected_chunks', side_effect=interrupt):
                with self.assertRaisesRegex(RuntimeError, 'interruption'):
                    self.write(job)
            checkpoint = stage/'checkpoint.json'
            self.assertEqual(archive.read_json(checkpoint)['cursor'], STEP)
            self.assertEqual(archive.read_json(stage/'denoised.json')['status'], 'failed')
            with self.assertRaisesRegex(IdentityError, 'Use resume'):
                self.write(job)
            saved = checkpoint.read_bytes()
            changed = archive.read_json(checkpoint)
            changed['identity']['gain'] = .5
            archive.atomic_json(checkpoint, changed)
            with self.assertRaisesRegex(IdentityError, 'Incompatible checkpoint'):
                self.write(job, resume=True)
            checkpoint.write_bytes(saved)
            for name in ('denoised.wav', 'cleanup.jsonl'):
                with (stage/name).open('ab') as f:
                    f.write(b'uncommitted bytes')
            def read(a, n):
                return samples[a:a+n].astype('float64')
            expected = np.concatenate([y for _, _, y, _, _ in corrected_chunks(read, len(t), 0, len(t))])
            self.write(job, resume=True)
        folder = self.output/job_key(job)
        written, _ = sf.read(folder/'denoised.wav', dtype='float32')
        np.testing.assert_array_equal(written, expected.astype('<f4'))
        self.assertGreater(np.std(samples-written), .01)
        rows = [json.loads(line) for line in (folder/'cleanup.jsonl').read_text().splitlines()]
        self.assertEqual([n['anchor_frame'] for n in rows if n['kind'] == 'anchor'], [0,STEP,2*STEP])

    def test_normalization_resume_uses_program_peak_and_retains_endpoint_samples(self):
        self.freeze['normalization'] = gain_configuration()
        archive.atomic_json(self.output/archive.MARKER, marker(self.freeze))
        samples = np.full(12*RATE, .015625, dtype='<f4')
        samples[3] = -.98
        samples[-2] = .90
        job = self.source(samples)
        with patch('denoise_v2_production.normalize_audio', side_effect=RuntimeError('during normalization')):
            with self.assertRaisesRegex(RuntimeError, 'during normalization'):
                self.write(job)
        stage = self.work/job_key(job)
        self.assertTrue((stage/'unity.wav').exists())
        self.assertFalse((self.output/job_key(job)).exists())
        self.assertEqual(archive.read_json(stage/'checkpoint.json')['cursor'], len(samples))
        with patch('denoise_v2_production.corrected_chunks', side_effect=AssertionError('rerendered source')):
            # The finished checkpoint must bypass construction of the DSP iterator too.
            record = self.write(job, resume=True)
        target = self.output/job_key(job)
        result, rate = sf.read(target/'denoised.wav', dtype='float32')
        gain = record['normalization']
        self.assertEqual(rate, RATE)
        self.assertEqual(gain['input_peak'], .015625)
        self.assertAlmostEqual(gain['gain_db'], 12.)
        self.assertEqual(gain['max_boost_db'], 12.)
        self.assertAlmostEqual(gain['program_output_peak'], .015625*10**(12/20))
        self.assertEqual(gain['samples_above_full_scale'], 2)
        self.assertEqual([event['source_frames'] for event in gain['ignored_endpoint_impulses']],
                         [[3,4],[len(samples)-2,len(samples)-1]])
        np.testing.assert_array_equal(result, (samples.astype('float64')*record['gain']).astype('<f4'))
        self.assertFalse((target/'unity.wav').exists())
        self.assertEqual(self.write(job, resume=True), record)
        reference = self.base/'reference.wav'
        sf.write(reference, samples[RATE:2*RATE]*1.5, RATE, subtype='PCM_24')
        reviewed = dict(source_frames=[RATE,2*RATE], common_gain=1.5,
                        files={reference.name:dict(sha256=archive.sha256(reference))})
        compare_review(target/'denoised.wav', reviewed, reference, RATE, record['gain'])

    def test_reuse_completed_unity_canary_without_denoising_again(self):
        job = self.source(np.full(12*RATE, .25, dtype='<f4'))
        prior = self.write(job)
        source = self.output/job_key(job)
        original_manifest = (source/'denoised.json').read_bytes()
        normalized_freeze = dict(self.freeze, run_id='normalized', normalization=gain_configuration())
        root = self.base/'normalized'
        root.mkdir()
        archive.atomic_json(root/archive.MARKER, marker(normalized_freeze))
        with patch('denoise_v2_production.render_float', side_effect=AssertionError('rerendered source')):
            result = write_channel(job, normalized_freeze, root, self.base/'normalized_work', self.hints, None,
                                   originals=self.originals, reuse_unity=(source, self.freeze),
                                   progress=lambda _: None)
        self.assertEqual(result['reused_unity_run_id'], self.freeze['run_id'])
        self.assertAlmostEqual(result['normalization']['program_output_peak'], 10**(-3/20))
        self.assertEqual(result['normalization']['unity_audio_sha256'], prior['audio']['sha256'])
        self.assertEqual(result['cleanup_log_sha256'], prior['cleanup_log_sha256'])
        self.assertEqual((source/'denoised.json').read_bytes(), original_manifest)
        self.assertEqual(archive.sha256(source/'denoised.wav'), prior['audio']['sha256'])

    def test_normalization_counts_sustained_edge_audio_and_interior_peaks(self):
        path = self.base/'measurement.wav'
        samples = np.full(12*RATE, .125, dtype='<f4')
        samples[RATE//2:RATE] = .75
        samples[-2] = 2.
        for interior, expected in ((.125,.75), (.875,.875)):
            samples[6*RATE] = interior
            sf.write(path, samples, RATE, subtype='FLOAT')
            with sf.SoundFile(path) as audio:
                result = measure_gain(audio, GainSettings())
            self.assertEqual(result['input_peak'], expected)
            self.assertEqual([event['source_frames'] for event in result['ignored_endpoint_impulses']],
                             [[len(samples)-2,len(samples)-1]])

    def test_timing_policy_transition_recovers_after_quarantine(self):
        prior_id, next_id = 'v2-'+'a'*24, 'v2-'+'b'*24
        self.freeze['run_id'] = prior_id
        archive.atomic_json(self.output/archive.MARKER, marker(self.freeze))
        timing = self.source(np.full(2*RATE, .125), timing=True)
        legacy = self.write(timing)
        self.hints.mkdir()
        (self.hints/'preserved.txt').write_text('v1 data')
        control = self.base/'control'
        run, prior_run = control/next_id, control/prior_id
        run.mkdir(parents=True)
        prior_run.mkdir()
        archive.atomic_json(prior_run/'frozen.json', self.freeze)
        archive.atomic_json(run/'supersede.json', dict(prior_run_id=prior_id))
        snapshot = control/f'legacy_{prior_id}.json'
        archive.atomic_json(snapshot, archive_snapshot(self.hints))
        transaction = control/archive.TRANSACTION
        archive.atomic_json(transaction, dict(version='v1', run_id=prior_id, status='complete',
            snapshot_sha256=archive.sha256(snapshot), **{'from':str(self.output), 'to':str(self.hints)}))
        successor = dict(run_id=next_id, channel_policy=archive.ALL_NUMBERED)
        jobs = [dict(timing, action='denoise_v2'), dict(tape=TAPE, channel=20)]

        def interrupt(path, value):
            if path == transaction:
                raise RuntimeError('simulated interruption after quarantine')
            archive.atomic_json(path, value)

        with (patch('denoise_v2_rollout.CONTROL', control), patch('denoise_v2_rollout.CANONICAL', self.output),
              patch('denoise_v2_rollout.ARCHIVE', self.hints)):
            with patch('denoise_v2_rollout.atomic_json', side_effect=interrupt):
                with self.assertRaisesRegex(RuntimeError, 'simulated interruption'):
                    supersede_timing_copy_root(jobs, successor, run)
            self.assertFalse(self.output.exists())
            supersede_timing_copy_root(jobs, successor, run)
        rotate(successor, self.output, self.hints, control)
        quarantine = self.output.with_name(f'denoised_quarantine_{prior_id}')
        self.assertEqual(archive.sha256(quarantine/job_key(timing)/'denoised.wav'), legacy['audio']['sha256'])
        self.assertEqual((self.hints/'preserved.txt').read_text(), 'v1 data')
        self.assertEqual(archive.read_json(self.output/archive.MARKER), marker(successor))


class MigrationTests(unittest.TestCase):
    def test_simple_batch_resumes_in_same_output_directory(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp).resolve()
            root, old, control = (base/name for name in ('denoised', 'denoised_v1', 'control'))
            prior_id, new_id = 'v2-'+'a'*24, 'v2-'+'b'*24
            run = control/new_id
            run.mkdir(parents=True)
            root.mkdir()
            old.mkdir()
            (old/'generated.pkf').write_bytes(b'not in the historical inventory')
            (root/'preserved.wav').write_bytes(b'old output')
            archive.atomic_json(root/archive.MARKER, marker(dict(run_id=prior_id)))
            archive.atomic_json(run/'supersede.json', dict(prior_run_id=prior_id))
            freeze = dict(run_id=new_id)
            self.assertEqual(prepare_output(root, run, freeze), prior_id)
            (root/'new.wav').write_bytes(b'new output')
            self.assertEqual(prepare_output(root, run, freeze), prior_id)
            self.assertEqual((root/'new.wav').read_bytes(), b'new output')
            self.assertEqual((root/'preserved.wav').read_bytes(), b'old output')
            self.assertEqual((old/'generated.pkf').read_bytes(), b'not in the historical inventory')
            self.assertEqual(archive.read_json(root/archive.MARKER)['run_id'], new_id)

    def test_recovery_relocation_hash_and_rollback(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp).resolve()
            canonical, archived, control = (base/name for name in ('denoised','denoised_v1','control'))
            channel = canonical/TAPE/'CH20'
            channel.mkdir(parents=True)
            control.mkdir()
            original = channel/'denoised.wav'
            original.write_bytes(b'original v1')
            archive.atomic_json(channel/'denoised.json', {'status':'complete', 'audio':{'decoded_float32_sha256':'stored'}})
            binding = dict(path=str(original), sha256=archive.sha256(original))
            freeze = {'run_id':'test-freeze'}

            def fail_marker(path, value):
                if path.name == archive.MARKER:
                    raise RuntimeError('simulated crash after rename')
                archive.atomic_json(path, value)

            with patch('denoise_v2_rollout.atomic_json', side_effect=fail_marker):
                with self.assertRaisesRegex(RuntimeError, 'simulated crash'):
                    rotate(freeze, canonical, archived, control)
            self.assertTrue(archived.is_dir())
            rotate(freeze, canonical, archived, control)
            channel.mkdir(parents=True)
            original.write_bytes(b'unrelated new v2')
            with (patch.object(archive, 'CANONICAL', canonical), patch.object(archive, 'ARCHIVE', archived),
                  patch.object(archive, 'CONTROL', control), patch('v5_resume.CANONICAL', canonical)):
                self.assertEqual(archive.bound_path(binding), archived/original.relative_to(canonical))
                verify_bindings([binding])
                old_root = str(canonical/TAPE)
                clean = dict(status='analyzed')
                windows = [dict(channels={'20':dict(discovery_sources={'denoised':clean})})]
                self.assertEqual(resume_root(old_root, old_root, windows), str(archived/TAPE))
                clean['cleanup_version'] = 'v2'
                self.assertEqual(resume_root(old_root, old_root, windows), old_root)
                with self.assertRaisesRegex(ValueError, 'Stale evidence'):
                    verify_bindings([dict(binding, cleanup_version='v2')])
                rollback(control, canonical, archived)
                verify_bindings([binding])
            self.assertFalse(archived.exists())
            self.assertEqual(len(list(base.glob('denoised_quarantine_*'))), 1)


if __name__ == '__main__':
    unittest.main()
