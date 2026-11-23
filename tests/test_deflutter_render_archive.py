import importlib
import tempfile
from pathlib import Path
import threading
import unittest
from unittest.mock import patch

from tests.test_shared_tape import fixture, write
from crosstalk_stage import render_channel
from shared_tape import build_tape_plan
from shared_render import render_shared_channel

archive = importlib.import_module('05b_render_archive')


class RenderArchiveTests(unittest.TestCase):
    def test_publish_resume_and_detect_corrupt_archive(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            evidence, assessment, inventory = fixture(root)
            tape = inventory['tape']
            plan_root = root/'plans'
            plan = build_tape_plan(evidence, assessment, plan_root/tape)
            # This fixture supplies a saved map in place of the real qualified census.
            plan['qualified'] = True
            write(plan_root/tape/'plan.json', plan)
            sources, work, final = root/'release', root/'work', root/'dest'
            catalog = root/'echo'
            catalog.mkdir()
            write(catalog/'directions.json', {})
            rows = {}
            for row in inventory['channels']:
                ch = row['channel']
                product = render_channel(inventory, ch, root/'denoised', sources/tape/f'CH{ch:02d}',
                                         catalog, progress=lambda _: None)
                rows[ch] = dict(frames=row['frames'], sha256=product['audio']['sha256'])
            sizes = {(tape,ch): r['frames'] for ch,r in rows.items()}
            with patch.object(archive, 'PLANS', plan_root), patch.object(archive, 'SSD', root/'ssd'):
                monitor = archive.RenderMonitor(sizes, 2, 0)
                jobs = archive.prepare(tape, rows, sources, work, final, monitor)
                for ch, _, request, _ in jobs:
                    archive.render_job(('Proprius', 1), tape, (ch, request), monitor)
                # A stop between rendering on F and publishing on D reuses verified results.
                restart = archive.RenderMonitor(sizes, 2, 0)
                jobs = archive.prepare(tape, rows, sources, work, final, restart)
                self.assertEqual(restart.reused, set(sizes))
                locked = work/tape/'results/CH20/deflutter.wav'
                unlink = Path.unlink

                def unlink_with_sharing_violation(path, *args, **kwargs):
                    if path == locked:
                        error = PermissionError('Open WAV')
                        error.winerror = 32
                        raise error
                    return unlink(path, *args, **kwargs)

                with patch.object(Path, 'unlink', unlink_with_sharing_violation):
                    archive.publish(tape, jobs, work, restart)
                self.assertEqual(restart.published, set(sizes))
                self.assertEqual(list(work.rglob('*.wav')), [locked])
                direct = render_shared_channel(plan_root/tape, 20, sources/tape/'CH20',
                                               root/'direct', progress=lambda _: None)
                report = archive.read(final/tape/'CH20/review.json')
                self.assertEqual(report['audio']['deflutter']['decoded_float32_sha256'],
                                 direct['audio']['deflutter']['decoded_float32_sha256'])
                self.assertEqual(report['start_seconds'], 1)
                self.assertEqual(report['shared_map'], direct['shared_map'])
                after = archive.RenderMonitor(sizes, 2, 0)
                archive.coordinate(sources, {tape:rows}, work, final, after, threading.Event())
                self.assertEqual(after.reused, set(sizes))
                self.assertFalse(list(work.rglob('*.wav')))
                with (final/tape/'CH20/deflutter.wav').open('ab') as altered:
                    altered.write(b'changed')
                with self.assertRaisesRegex(ValueError, 'Invalid completed WAV'):
                    archive.prepare(tape, rows, sources, work, final, after)
