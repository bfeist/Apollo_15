from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts' / 'sg_transcript'))
from md_editorial import split_spans  # noqa: E402
from md_reconcile import (fills_garble, grow_link, journal_garble_tokens, journal_reading,  # noqa: E402
                          printed_garble_tokens, proposed_reading)


class ProposedReadingTests(unittest.TestCase):
    """The journals curate the 1971 PDFs, so they are proposed over the printed row."""

    def test_a_journal_is_proposed_over_the_printed_row(self):
        self.assertEqual(proposed_reading({'printed': 'roger the fails'.split(), 'afj': 'roger the falls'.split()}), 'afj')
        self.assertEqual(proposed_reading({'printed': 'roger'.split(), 'alsj': 'roger houston'.split()}), 'alsj')

    def test_agreeing_journals_are_proposed(self):
        readings = {'printed': 'a'.split(), 'afj': 'yeah b'.split(), 'alsj': 'yes b'.split()}
        self.assertEqual(proposed_reading(readings), 'afj')

    def test_differing_journals_take_the_one_the_printed_row_supports(self):
        readings = {'printed': 'stand by'.split(), 'afj': 'stand by'.split(), 'alsj': 'standing by'.split()}
        self.assertEqual(proposed_reading(readings), 'afj')
        readings = {'printed': 'stand'.split(), 'afj': 'stand by'.split(), 'alsj': 'standing by'.split()}
        self.assertEqual(proposed_reading(readings), '')


class GarbleFillTests(unittest.TestCase):
    """A journal reading is accepted where it only supplies words for printed garble."""

    def fill(self, printed, journal_spans):
        spans = [{'kind': k, 'text': t} for k, t in journal_spans]
        return fills_garble(printed_garble_tokens(printed), journal_garble_tokens(spans))

    def test_journal_words_for_printed_dots(self):
        self.assertTrue(self.fill('Roger, ... the burn.', [('spoken', 'Roger, copy the burn.')]))
        self.assertTrue(self.fill('Okay .... lift-off ...', [('spoken', 'Okay. We have lift-off.')]))
        self.assertTrue(self.fill('SC (garble)', [('spoken', 'SC Stand by')]))

    def test_journal_keeping_some_garble(self):
        spans = [('spoken', 'Roger, copy '), ('editorial', 'garble'), ('spoken', ' burn.')]
        self.assertTrue(self.fill('Roger, ... burn.', spans))

    def test_any_other_difference_is_not_a_fill(self):
        self.assertFalse(self.fill('Roger, ... the burn.', [('spoken', 'Roger, copy that burn.')]))
        self.assertFalse(self.fill('Roger, the burn.', [('spoken', 'Roger, copy the burn.')]))  # no garble
        self.assertFalse(self.fill('Roger, *** the burn.', [('spoken', 'Roger, copy the burn.')]))  # VOX clip
        self.assertFalse(self.fill('Roger, ... the burn.', [('spoken', 'Roger, the burn.')]))  # nothing added


class UtteranceBreakTests(unittest.TestCase):
    """The journals and the PDFs break utterances differently; a link grows over the other rows."""

    @staticmethod
    def neighbours(rows):
        def nxt(end, added):
            n = sum(e == end for e, _ in added)
            i = n if end == 'after' else None
            return (f'r{i}', rows[i].split()) if i is not None and i < len(rows) else None
        return nxt

    def test_one_journal_row_covers_two_printed_rows(self):
        journal = 'okay houston gimbal motors are off and the oxidizer is 40'.split()
        added_j, added_p, score = grow_link([journal], ['okay houston gimbal motors are off'.split()],
                                            self.neighbours([]), self.neighbours(['and the oxidizer is 40']))
        self.assertEqual(added_p, [('after', 'r0')])
        self.assertEqual(added_j, [])
        self.assertEqual(score, 1.0)

    def test_a_printed_row_split_in_the_journal(self):
        added_j, added_p, _ = grow_link(['roger copy'.split()], ['roger copy the burn report'.split()],
                                        self.neighbours(['the burn report']), self.neighbours([]))
        self.assertEqual(added_j, [('after', 'r0')])

    def test_an_unrelated_neighbour_stays_out(self):
        added_j, added_p, _ = grow_link(['roger copy'.split()], ['roger copy'.split()],
                                        self.neighbours(['stand by']), self.neighbours(['go ahead']))
        self.assertEqual((added_j, added_p), ([], []))


class JournalReadingTests(unittest.TestCase):
    def reading(self, text):
        spans = [dict(s, index=i) for i, s in enumerate(split_spans(text, 'afj'))]
        return journal_reading(spans, {})

    def test_unit_conversions_are_always_removed(self):
        self.assertEqual(self.reading("the S-IVB oxidizer is 40 [psi, 276 kPa], and the fuel's about 31 [psi, 214 kPa]."),
                         "the S-IVB oxidizer is 40, and the fuel's about 31.")
        self.assertEqual(self.reading('a 93.7 [nautical miles, 173.5 km] by 88.9 [nautical miles, 164.6 km, orbit];'),
                         'a 93.7 by 88.9;')

    def test_every_square_bracket_is_removed(self):
        self.assertEqual(self.reading('Okay, Houston. [SPS engine] Gimbal Motors are Off. [Long pause.]'),
                         'Okay, Houston. Gimbal Motors are Off.')
        self.assertEqual(self.reading('[Do] you guys have enough [time]?'), 'you guys have enough?')

    def test_garble_and_nonspeech_keep_their_markers(self):
        self.assertEqual(self.reading('Roger, copy [garble] burn.'), 'Roger, copy... burn.')
        self.assertEqual(self.reading('Okay. [Laughter]'), 'Okay. (Laughter)')

    def test_parentheses_stay_except_units(self):
        self.assertEqual(self.reading('Okay. (Pause) 40 (psi, 276 kPa) now.'), 'Okay. (Pause) 40 now.')



class SquareBracketDecisionTests(unittest.TestCase):
    """Square brackets are the editors' additions: decided without review, never kept as words."""

    def test_decisions(self):
        from md_spans import square_bracket
        self.assertEqual(square_bracket('restoration', 'keep', [])[:3], ('inserted', 'remove', 'auto'))
        self.assertEqual(square_bracket('other', '', [])[:3], ('inserted', 'remove', 'auto'))
        self.assertEqual(square_bracket('gloss', 'remove', [])[:3], ('gloss', 'remove', 'auto'))
        self.assertEqual(square_bracket('garble', 'garble', [])[:3], ('garble', 'garble', 'auto'))
        self.assertEqual(square_bracket('pause', 'annotate', [])[:3], ('pause', 'annotate', 'auto'))
        self.assertEqual(square_bracket('nonspeech', 'keep', [])[:3], ('nonspeech', 'keep', 'auto'))



class ComparedWordsTests(unittest.TestCase):
    """Only spoken words are compared: no non-speech markers or editors' marks on either side."""

    def test_printed_markers_are_not_words(self):
        from md_reconcile import printed_tokens
        self.assertEqual(printed_tokens('Okay. Going through the air. (Laughter)'), 'okay going through the air'.split())
        self.assertEqual(printed_tokens("It's broke loost [sic]."), 'its broke loost'.split())
        self.assertEqual(printed_tokens('CB(16) open'), 'cb 16 open'.split())

    def test_journal_literal_notation_is_spoken(self):
        from md_reconcile import spoken_text
        self.assertIn('CB(16)', spoken_text(split_spans('"CB(16), Comm, TV, closed." (Pause)', 'alsj')))


    def test_line_end_hyphens_join(self):
        from md_reconcile import printed_tokens
        self.assertEqual(printed_tokens('The measure- ment. Okay - okay.'), 'the measurement okay okay'.split())

    def test_pao_tape_overlap_is_dropped_once(self):
        from md_reconcile import join_printed, printed_tokens
        a, b = 'place it in trajectory toward the moon.', 'trajectory toward the moon. It will increase'
        text, words = join_printed([(a, printed_tokens(a)), (b, printed_tokens(b))])
        self.assertEqual(text, 'place it in trajectory toward the moon. It will increase')
        self.assertEqual(words, printed_tokens(text))


if __name__ == '__main__':
    unittest.main()
