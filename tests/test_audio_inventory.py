"""Audio inventory (scripts/audio_inventory): tape keys, missions and Apollo 15 NARA GET ranges."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import _bootstrap  # noqa: E402,F401  (every registered script folder)
import a15_nara  # noqa: E402
from inventory_common import ia_file_key, mission_of_code, mission_of_text, nara_key, nara_queries  # noqa: E402


def nara(local_id, title, scope):
    return {'nara_local_id': local_id, 'nara_title': title, 'nara_scope': scope}


class TapeKeys(unittest.TestCase):
    def test_sources_share_keys(self):
        self.assertEqual(nara_key('255-PAOa-602-AAA'), '602-AAA')
        self.assertEqual(nara_key('255-STa-3304'), '3304')
        self.assertEqual(nara_key('255-AVTa-15-03002'), '15-03002')
        self.assertEqual(ia_file_key('045-AAC_STS-3_Mission_Audio.wav'), '045-AAC')
        self.assertEqual(ia_file_key('540-AAA.wav'), '540-AAA')
        self.assertEqual(a15_nara._file_key('255-paoa-640-aaa-tr1.mp3'), '640-AAA')
        self.assertEqual(a15_nara._file_key('255-pao-602-aaa.mp3'), '602-AAA')


class Missions(unittest.TestCase):
    def test_jsc_codes(self):
        for code, name in (('A/15', 'Apollo 15'), ('G/IXA', 'Gemini 9A'), ('M/III', 'Mercury MR-3'),
                           ('SL/II', 'Skylab 2'), ('51-F', 'STS-51F'), ('STS70', 'STS-70'), ('------', 'General')):
            self.assertEqual(mission_of_code(code), name)

    def test_queries_are_quoted(self):
        self.assertEqual(nara_queries('Apollo 15'), ['"Apollo 15"', '"Apollo XV"'])
        self.assertTrue(all(q.startswith('"') and q.endswith('"') for q in nara_queries('STS-41B')))

    def test_titles(self):
        self.assertEqual(mission_of_text('GT-XII Mission Commentary'), 'Gemini 12')
        self.assertEqual(mission_of_text('045-AAC_STS-3_Mission_Audio.wav'), 'STS-3')
        self.assertEqual(mission_of_text('Historical Apollo 15 Flight Tape Number 1'), 'Apollo 15')


class Apollo15Get(unittest.TestCase):
    def test_flight_tape_from_series_position(self):
        # The title says tape 23; it is the 21st in the series, 1971-07-31 12:00 GMT.
        start, end, _basis, note = a15_nara.get_range(nara(
            '255-PAOa-628-AAA', 'Historical Apollo 15 Flight Tape Number 23',
            'This recording contains audio from GMT Start 1200 to GMT End 1800.'))
        self.assertEqual((start, end), (118 * 3600 + 26 * 60, 124 * 3600 + 26 * 60))
        self.assertIn('title says tape 23', note)

    def test_goss_days_first(self):
        start, end, _basis, _note = a15_nara.get_range(nara(
            '255-AVTa-15-03190', 'Apollo 15 Prime Goss Voice Tape # 15-03190',
            'This recording contains audio from Recorded Start Time GET 012:06:41:45 to '
            'Recorded Stop Time GET 012:07:53:09.'))
        self.assertEqual(start, 294 * 3600 + 41 * 60 + 45)
        self.assertEqual(end, 295 * 3600 + 53 * 60 + 9)

    def test_goss_typo_follows_previous_reel(self):
        scope = 'This recording contains audio from Recorded Start Time GET {} to Recorded Stop Time GET {}.'
        rows = []
        for local, a, b in (('255-AVTa-15-03125', '008:00:48:45', '008:02:21:17'),
                            ('255-AVTa-15-03126', '008:07:20:00', '008:03:56:00')):
            s, e, basis, note = a15_nara.get_range(nara(local, 'Apollo 15 Prime Goss Voice Tape', scope.format(a, b)))
            rows.append({'nara_local_id': local, 'kind': 'goss', 'get_basis': basis, 'note': note,
                         'get_start_s': s, 'get_end_s': e})
        a15_nara.goss_repair(rows)
        self.assertEqual(a15_nara.format_get(rows[1]['get_start_s']), '194:21:00')


if __name__ == '__main__':
    unittest.main()
