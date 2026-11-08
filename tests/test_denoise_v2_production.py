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
import denoise_archive as archive
from denoise_v2 import corrected_chunks
from denoise_v2_production import (STEP, IdentityError, job_key, validate_source, wav_header,
                                   write_channel)
from denoise_v2_rollout import marker, rollback, rotate
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
        job = self.source(np.arange(2*RATE)/RATE-.5, timing=True)
        row = job['source']
        record = self.write(job)
        folder = self.output/job_key(job)
        self.assertEqual(record['action'], 'copy_original_timing')
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


class MigrationTests(unittest.TestCase):
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
