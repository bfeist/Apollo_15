"""SG reel matching (scripts/sg-audio/02_sg-transcript/md_sg.py): segment parsing, offsets, anchors and judging."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import _bootstrap  # noqa: E402,F401  (every registered script folder)
import md_pao as pao  # noqa: E402
import md_sg as sg  # noqa: E402


def segment(index, start, words, flags=()):
    out, t = [], start
    for w in words.split():
        out.append({'tok': w, 's': t, 'e': t + 0.3, 'p': 0.9, 'flags': [], 'text': w})
        t += 0.4
    return {'index': index, 's': start, 'e': t, 'text': words, 'flags': list(flags), 'words': out}


def event(eid, t, words):
    return {'event_id': eid, 't': t, 'get': '', 'get_basis': 'journal', 'printed_get': '', 'journal_get': '',
            'journal_basis': 'anchor', 'who': 'CC', 'tokens': words.split(), 'text': words}


def anchor(t, offset, flags=()):
    return {'reel': 'R', 'source_s': t, 'offset_s': offset, 'event_id': f'e{t}', 'flags': list(flags)}


class SegmentNumberTests(unittest.TestCase):
    def test_pao_tape_labels(self):
        self.assertEqual(sg.pao_segment('41/2'), 41)
        self.assertEqual(sg.pao_segment('MC8/1'), 8)
        self.assertIsNone(sg.pao_segment(''))


class AnchorTests(unittest.TestCase):
    def test_utterance_start_inside_a_segment_is_anchored_at_its_first_word(self):
        seg = segment(0, 100.0, 'roger we copy and the shadow is four degrees left')
        cands = [event('a', 500, 'roger we copy'), event('b', 503, 'and the shadow is four degrees left')]
        found = {ev['event_id']: (i, run) for i, ev, run, _ in sg.anchors_for_segment(seg, cands)}
        self.assertEqual(found['a'], (0, 3))
        self.assertEqual(found['b'], (3, 7))

    def test_short_or_unmatched_starts_are_not_anchors(self):
        seg = segment(0, 0.0, 'okay houston go ahead')
        found = sg.anchors_for_segment(seg, [event('a', 0, 'okay'), event('b', 2, 'something else entirely')])
        self.assertEqual(found, [])

    def test_coarse_offset_votes_find_the_reel_offset(self):
        events = [event(f'e{k}', 50000 + 30 * k, f'distinct w{k} x{k} y{k} z{k} words') for k in range(20)]
        segs = [segment(k, 1000.0 + 30 * k, f'distinct w{k} x{k} y{k} z{k} words') for k in range(20)]
        grid = sg.coarse_offsets('R', segs, events)
        self.assertTrue(grid)
        self.assertTrue(all(abs(g['offset_s'] - 49000) < 1 for g in grid if g['rank'] == 1))


class JudgeTests(unittest.TestCase):
    def test_local_line_follows_drift_and_flags_outlier_and_collapsed(self):
        rows = [anchor(100.0 * k, 5000 + 0.005 * 100 * k) for k in range(10)]
        rows.append(anchor(450.0, 5012.0))                                 # 10 s off its neighbours
        rows.append(anchor(550.0, 5002.7, flags=['collapsed_timing']))     # unusable timing
        rows.append(anchor(650.0, 5003.3, flags=['zero_duration']))        # start still usable
        rows.sort(key=lambda r: r['source_s'])
        sg.judge(rows)
        status = {r['source_s']: r['status'] for r in rows if r['source_s'] in (450.0, 550.0, 650.0)}
        self.assertEqual(status, {450.0: 'outlier', 550.0: 'rejected', 650.0: 'flagged'})
        self.assertTrue(all(r['status'] == 'ok' for r in rows if not r['flags'] and r['source_s'] != 450.0))
        self.assertLess(max(abs(r['residual_s']) for r in rows if r['status'] == 'ok'), 0.01)

    def test_sparse_anchor_is_isolated(self):
        rows = [anchor(0.0, 10.0), anchor(5000.0, 10.0)]
        sg.judge(rows)
        self.assertEqual({r['status'] for r in rows}, {'isolated'})



def pao_row(text):
    spans = pao.word_spans(text)
    return {'text': text, 'spans': spans, 'tokens': [t for _, _, t in spans]}


class PaoReadingTests(unittest.TestCase):
    VOCAB = {'all', 'still', 'going', 'well', 'with', 'apollo', 'the', 'crater', 'falls', 'fails', 'us'}

    def test_page_slots_skip_ia_speaker_labels_and_find_word_readings(self):
        rows = [pao_row('Ail still going well.'), pao_row('the bright crater there.')]
        slots = pao.page_slots(rows, {'ia': 'PAO All still going well. CAPCOM the bright era te r there.'.split(),
                                      'oxr': 'PAO All still going well. CAPCOM the bright crater there.'.split()})
        found = [(s['row'], s['tl'], {n: v and v[0] for n, v in s['readings'].items()}) for s in slots]
        self.assertEqual(found, [(0, ['ail'], {'ia': ['all'], 'oxr': ['all']}),
                                 (1, ['crater'], {'ia': ['era', 'te', 'r'], 'oxr': ['crater']})])

    def test_decisions(self):
        def decide(tl, ia, asr=None, oxr=None):
            readings = {'ia': (ia.split(), ia)}
            if oxr is not None:
                readings['oxr'] = (oxr.split(), oxr)
            return pao.decide({'tl': tl.split(), 'readings': readings}, asr and asr.split(), self.VOCAB)[1:]
        self.assertEqual(decide('crater', 'era te r'), ('tl', 'vocab'))        # IA split a word into unknown pieces
        self.assertEqual(decide('ye s', 'yes'), ('ia', 'joined'))
        self.assertEqual(decide('ail', 'all'), ('ia', 'vocab'))
        self.assertEqual(decide('fails', 'falls'), ('tl', 'typed'))            # a known typed word stands
        self.assertEqual(decide('fails', 'falls', 'falls'), ('tl', 'typed'))   # the audio never overrules a typed word
        self.assertEqual(decide('fails', 'falls', 'fails'), ('tl', 'asr'))     # it may confirm the text layer
        self.assertEqual(decide('ail', 'all', 'all'), ('ia', 'asr'))           # or side against an OCR misread
        self.assertEqual(decide('fails', 'falls', 'something'), ('tl', 'typed'))  # never the ASR's own words
        self.assertEqual(decide('place at this', 'Pj _f', oxr='place at this'), ('tl', 'agree'))  # garbled IA dropped
        self.assertEqual(decide('fials', 'fells'), ('tl', 'no_better'))
        self.assertEqual(decide('fials', 'falls'), ('ia', 'vocab'))
        self.assertEqual(decide('fials', 'fails', oxr='falls'), ('', 'review'))     # two known alternatives
        self.assertEqual(decide('with a comm check do you', 'Ove r'), ('tl', 'default'))
        self.assertEqual(decide('fails', 'falls', oxr='falls'), ('oxr', 'ocr_majority'))  # two OCRs outvote one
        self.assertEqual(decide('falls', 'fails', oxr='falls'), ('tl', 'agree'))
        self.assertEqual(decide('fails', 'fells', 'falls', oxr='falls'), ('tl', 'typed'))
        self.assertEqual(decide('fials', 'fells', 'falls', oxr='falls'), ('oxr', 'asr'))

    def test_correction_keeps_the_rest_of_the_text(self):
        row = pao_row('Ail still going well, ye s.')
        slots = [{'i0': 0, 'i1': 1, 'words': 'All'}, {'i0': 4, 'i1': 6, 'words': 'yes'}]
        self.assertEqual(pao.corrected_text(row, slots), 'All still going well, yes.')


if __name__ == '__main__':
    unittest.main()
