from pathlib import Path
import sys
import tempfile
import unittest

import numpy as np
import soundfile as sf

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import _bootstrap  # noqa: E402,F401  (every registered script folder)
import md_common  # noqa: E402
import md_reconcile  # noqa: E402
import md_review_ui as ui  # noqa: E402


class ReviewUiTests(unittest.TestCase):
    def test_get_seconds(self):
        self.assertEqual(ui.get_seconds('145:24:51'), 145 * 3600 + 24 * 60 + 51)
        self.assertEqual(ui.get_seconds('-001:30:00'), -5400)
        self.assertIsNone(ui.get_seconds(''))

    def test_clip_is_mono_pcm_of_the_requested_span(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'reel.wav'
            sf.write(path, np.zeros((48000 * 4, 2), dtype='float32'), 48000)
            data, rate = sf.read(__import__('io').BytesIO(ui.clip_wav(path, 1.0, 2.0)))
            self.assertEqual((rate, data.ndim, len(data)), (48000, 1, 96000))

    def test_save_records_the_choice_in_mission_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            work, review = Path(tmp) / 'work', Path(tmp) / 'review'
            work.mkdir()
            review.mkdir()
            row = {f: '' for f in md_reconcile.WORDING_FIELDS}
            row.update({'wording_key': 'E1:abc', 'event_id': 'E1', 'get': '000:01:00', 'kind': 'substantive',
                        'status': 'review', 'proposed': 'afj', 'printed': 'a', 'afj': 'b'})
            md_common.write_tsv(work / 'wording.tsv', md_reconcile.WORDING_FIELDS, [row])
            saved = (ui.WORK, md_reconcile.WORK, md_reconcile.REVIEW)
            ui.WORK = md_reconcile.WORK = work
            md_reconcile.REVIEW = review
            try:
                with self.assertRaises(ValueError):
                    ui.Store().save('wording', 'E1:abc', {'choice': 'asr', 'reviewer': 'me'})
                out = ui.Store().save('wording', 'E1:abc', {'choice': 'afj', 'reviewer': 'me', 'note': 'heard it'})
            finally:
                ui.WORK, md_reconcile.WORK, md_reconcile.REVIEW = saved
            self.assertEqual(out['recorded'], 1)
            choices = md_common.read_tsv(review / 'wording_choices.tsv')
            self.assertEqual([(c['wording_key'], c['choice'], c['reviewer'], c['note']) for c in choices],
                             [('E1:abc', 'afj', 'me', 'heard it')])
            self.assertEqual(md_common.read_tsv(work / 'wording.tsv')[0]['status'], 'human')


if __name__ == '__main__':
    unittest.main()
