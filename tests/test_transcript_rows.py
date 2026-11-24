from pathlib import Path
from types import SimpleNamespace
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts' / 'sg_transcript'))
from md_transcripts import DEAD_AIR, drop_end_of_tape, extract_page, strip_end_of_tape  # noqa: E402


def line(y, x, *words):
    out = []
    for w in words:
        out.append((x, y, x + 6 * len(w), y + 9, w, 0, 0, 0))
        x += 6 * len(w) + 8
    return out


def stamped(y, stamp, speaker, text):
    d, h, m, s = stamp.split()
    return [(56, y, 66, y + 9, d), (73, y, 83, y + 9, h), (92, y, 102, y + 9, m), (110, y, 120, y + 9, s),
            (134, y, 150, y + 9, speaker)] + line(y, 188, *text.split())


class FakePage:
    def __init__(self, words, number=12):
        self.words, self.number, self.rect = words, number, SimpleNamespace(width=612)

    def get_text(self, kind='text'):
        return [w[:5] + (0, 0, 0) for w in self.words] if kind == 'words' else 'x'


class TopOfPageTests(unittest.TestCase):
    """AS15_TEC page 13: the first row is printed above y 109, its wrap just below."""

    def page(self):
        return FakePage(line(66, 55, 'Tape', '1/4') + line(78, 55, 'Page', '4')
                        + stamped(103, '00 00 11 53', 'CDR', 'Okay, Houston. GIMBAL MOTORS are OFF, and the')
                        + line(115, 188, 'S-IVB', 'oxidizer', 'is', '40.')
                        + stamped(139, '00 00 11 59', 'CC', 'Roger; 40 and 31.')
                        + stamped(163, '00 00 12 06', 'CMP', 'Okay, Gordo.'))

    def test_the_top_row_and_its_wrap_are_kept_without_renumbering(self):
        rows, _ = extract_page(self.page(), 'single', 'tec', 'u')
        self.assertEqual([r['event_id'][-3:] for r in rows], ['r00', 'r01', 'r02'])
        self.assertEqual(rows[0]['get'], '000:11:53')
        self.assertTrue(rows[0]['text_ocr'].endswith('and the S-IVB oxidizer is 40.'))
        self.assertEqual(rows[1]['text_ocr'], 'Roger; 40 and 31.')

    def test_the_header_is_not_a_row(self):
        rows, _ = extract_page(self.page(), 'single', 'tec', 'u')
        self.assertFalse(any('Tape' in r['text_ocr'] or 'Page' in r['text_ocr'] for r in rows))

    def test_a_wrap_from_the_previous_page_is_carried(self):
        page = FakePage(line(66, 55, 'Tape', '1/4') + line(94, 188, 'to', 'copy.')
                        + stamped(139, '00 00 11 59', 'CC', 'Roger.'))
        rows, _ = extract_page(page, 'single', 'tec', 'u')
        self.assertEqual([(r['event_id'][-4:], r['text_ocr']) for r in rows], [('u094', 'to copy.'), ('-r01', 'Roger.')])


class EndOfTapeTests(unittest.TestCase):
    def words(self, text):
        return [w[4] for w in strip_end_of_tape(line(300, 170, *text.split()))]

    def test_garbled_end_of_tape_markers_are_not_communication(self):
        for text in ('I think we ENI, OF TAPE', 'I think we END OF TAPE.', 'I think we _ND OF TAPE',
                     'I think we EXD OF TAPE', 'I think we END OF TAP E', 'I think we END OF TA?E',
                     'I think we _1_ OF TAP1!'):
            self.assertEqual(self.words(text), ['I', 'think', 'we'], text)
        self.assertEqual(self.words('end of tape'), [])

    def test_markers_inside_an_ocr_page_stream_are_dropped(self):
        stream = 'Okay, Gordo. END OF TAP E PAO This is Apollo Control. _ND OF TAPE.'.split()
        self.assertEqual(drop_end_of_tape(stream), 'Okay, Gordo. PAO This is Apollo Control.'.split())
        self.assertEqual(drop_end_of_tape('a piece of tape'.split()), 'a piece of tape'.split())

    def test_dead_air_notes_are_not_communication(self):
        for text in ('DEAD AIR.', '(All Dead Air)'):
            self.assertTrue(DEAD_AIR.fullmatch(text), text)
        self.assertFalse(DEAD_AIR.fullmatch('we had dead air there'))

    def test_spoken_words_are_kept(self):
        for text in ('Okay, take care of that.', 'a piece of tape', 'that is the end of tape'):
            self.assertEqual(self.words(text), text.split(), text)


if __name__ == '__main__':
    unittest.main()
