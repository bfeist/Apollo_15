"""Flight-plan ingestion (docs/17_2026-11-23_flight_plan_ingestion.md): parsers, fits, validators and release rules."""
from pathlib import Path
import sys
import unittest

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import _bootstrap  # noqa: E402,F401  (every registered script folder)
import md_common  # noqa: E402
import md_flightplan as fp  # noqa: E402
import md_fp_actual as actual  # noqa: E402
import md_fp_geometry as geometry  # noqa: E402
import md_fp_ocr as ocr  # noqa: E402
import md_fp_pages as pages  # noqa: E402
import md_fp_structure as structure  # noqa: E402
import md_toc as toc  # noqa: E402

RAW_GIF = md_common.RAW_ROOT / 'original_docs/flight_plan/afj_gif'


class PageIdentityTests(unittest.TestCase):
    def test_page_ids_normalise_to_printed_numbers(self):
        self.assertEqual(fp.normalise_page_id('5-9'), '5-009')
        self.assertEqual(fp.normalise_page_id('3-126'), '3-126')
        self.assertEqual(fp.normalise_page_id('XI'), 'xi')
        self.assertIsNone(fp.normalise_page_id('page'))
        order = sorted(['3-010', 'xxv', '1-001', 'cover', '5-009', 'ii'], key=fp.page_sort_key)
        self.assertEqual(order, ['cover', 'ii', 'xxv', '1-001', '3-010', '5-009'])

    def test_index_and_excerpt_parsers(self):
        html = b'''<H4>Chapter 2 - Charts and Tables</H4><ul><li>2-012 <A HREF="fplan/2-012.gif">CSM Burn/Event Schedule</A>
        </ul><H3>Chapter 3</H3><H4>Lunar Orbit/Surface Phase</H4><ul><li>3-126 <A HREF="fplan/3-126.gif">104 hours - PDI</A></ul>
        <H4>Chapter 4 - Consumables Analysis</H4>'''
        rows = fp.parse_afj_index(html)
        self.assertEqual([r['page_id'] for r in rows], ['2-012', '3-126'])
        self.assertEqual(rows[1]['phase'], 'Lunar Orbit/Surface Phase')
        self.assertEqual(fp.unlinked_chapters(html), ['Chapter 4 - Consumables Analysis'])
        excerpt = fp.parse_alsj_excerpt(b'Page 5-9, 96:00-108:00, Summary Flight Plan <a href="a15fp001.jpg" '
                                        b'title="image">(137k)</a><p> Page 3-126, 104:00-105:00, PDI <a\n'
                                        b'href="a15fp019.jpg">x</a>')
        self.assertEqual([(e['page_id'], e['jpg']) for e in excerpt], [('5-009', 'a15fp001.jpg'),
                                                                        ('3-126', 'a15fp019.jpg')])

    def test_pdf_images_stored_bottom_up_still_match_by_pixels(self):
        gif = (np.arange(60, dtype=np.uint8).reshape(6, 10) * 3)
        stored = gif[::-1]  # how many AFJ PDF XObjects are stored
        found = {k: pages.pixel_hash(v) for k, v in pages.rotations(stored).items()}
        self.assertIn(pages.pixel_hash(gif), found.values())
        self.assertEqual([k for k, v in found.items() if v == pages.pixel_hash(gif)], ['flip0'])

    def test_lep_reading_and_structure(self):
        listed = dict(pages.parse_lep('3-310 . . . 6/21/71 3-348 . . . 6/21/71 5-1 . . . . 6/21/71 xii . . 6/21/71'))
        self.assertEqual(listed, {'3-310': '6/21/71', '3-348': '6/21/71', '5-001': '6/21/71', 'xii': '6/21/71'})
        expected = pages.expected_lep_pages()
        self.assertIn('3-003', expected)
        self.assertIn('4-001', expected)
        self.assertIn('6-001', expected)
        self.assertEqual(len([p for p in expected if p.startswith('5-')]), 25)


class RulerTests(unittest.TestCase):
    def test_labels_take_hours_and_skip_dap_codes(self):
        items = [((0, y, 0, t), geometry.label_value(t)) for y, t in
                 ((120, '104:00'), (220, ':10'), (240, '(22112)'), (320, ':20'), (420, '104:30'), (720, '105:00'))]
        items = [i for i in items if i[1]]
        labels = geometry.ruler_labels(items, None)
        self.assertEqual([s for _, _, s in labels], [374400, 375000, 375600, 376200, 378000])

    def test_minute_labels_roll_over_the_hour(self):
        items = [((0, y, 0, t), geometry.label_value(t)) for y, t in ((100, '69:00'), (300, ':40'), (500, ':20'))]
        self.assertEqual([s for _, _, s in geometry.ruler_labels(items, None)], [248400, 250800, 253200])

    def test_robust_fit_rejects_a_misread_label(self):
        points = [(120, 374400), (220, 375000), (320, 375600), (420, 376200), (520, 386800), (620, 377400)]
        fit = geometry.fit_ruler(points)
        self.assertAlmostEqual(fit['slope'], 6.0, places=3)
        self.assertEqual(fit['inliers'], [True, True, True, True, False, True])

    def test_footer_range_and_cdt_check(self):
        self.assertEqual(geometry.footer_range('6/21/71 104:0D 105:00 5/14 3-126'), (374400, 378000))
        self.assertEqual(geometry.footer_range('69:00 - 71:00'), (248400, 255600))
        self.assertIsNone(geometry.footer_range('no time here'))
        self.assertEqual(geometry.cdt_expected(104 * 3600), '1634')
        self.assertEqual(geometry.cdt_expected(177 * 3600), '1734')
        self.assertEqual(geometry.cdt_expected(7 * 3600), '1534')

    def test_footer_fields(self):
        f = ocr.parse_footer('MISSION EDITION DATE TIME DAY/REV PAGE APOLLO 15 FINAL (7/26) 6/21/71 104:00 - 105:00 '
                             '5/14 3-126')
        self.assertEqual((f['edition'], f['page_date'], f['get_start'], f['get_end'], f['flight_day'], f['rev'],
                          f['printed_page']), ('FINAL (7/26)', '6/21/71', '104:00', '105:00', '5', '14', '3-126'))
        self.assertEqual(ocr.parse_footer('APOLLO 15 FINAL (7/26) 6/21/71 69:00 - 71:00 3-4/TLC 3-70')['flight_day'],
                         '3-4')

    @unittest.skipUnless((RAW_GIF / '3-126.gif').is_file(), 'raw flight-plan scans not available')
    def test_golden_page_3_126_geometry(self):
        g = geometry.analyse(pages.load_gray(RAW_GIF / '3-126.gif'))
        tokens = [(250, 120, 297, '104:00'), (277, 220, 298, ':10'), (277, 320, 298, ':20'), (250, 420, 298, '104:30'),
                  (276, 520, 298, ':40'), (276, 620, 298, ':50'), (250, 718, 298, '105:00'), (250, 470, 290, '(22112)')]
        strips = geometry.find_strips(g, tokens, (374400, 378000))
        self.assertEqual(len(strips), 1)
        fit = strips[0]['fit']
        self.assertLess(abs(fit['intercept'] + fit['slope'] * 120 - 374400), 30)
        self.assertLess(fit['rms'], 30)
        self.assertEqual(strips[0]['rule_x'], 224)  # ticks drawn to the right of the rule
        page = {'page_id': '3-126', 'chapter': '3', 'status': 'present', 'index_title': '104 hours - PDI'}
        self.assertEqual(geometry.classify(page, {**g, 'strips': strips}, 'LM FLIGHT PLAN MCC-H CDR LMP NOTES')[0],
                         'lm_timeline')


class StructureValidationTests(unittest.TestCase):
    items = [{'block_id': 'p:b000', 'column': 'LM', 'text': 'PDI'},
             {'block_id': 'p:b001', 'column': 'LM', 'text': 'THROTTLE UP'},
             {'block_id': 'p:b002', 'column': 'NOTES', 'text': 'TIG: 104:28:54.8'},
             {'block_id': 'p:b003', 'column': 'LM', 'text': 'LM LUNAR TOUCHDOWN'}]
    glossary = {'PDI': ['POWERED DESCENT INITIATION']}

    def row(self, bid, **kw):
        base = {'block_id': bid, 'kind': 'activity', 'category': 'burn', 'activity_group': bid,
                'continues_from_previous_page': False, 'expansion': '', 'confidence': 0.9}
        return {**base, **kw}

    def test_valid_answer_is_accepted(self):
        rows = [self.row('p:b000', expansion='POWERED DESCENT INITIATION'), self.row('p:b001', activity_group='p:b000'),
                self.row('p:b002', kind='pad_field'), self.row('p:b003', kind='event_marker')]
        ok, errors = structure.validate_rows(rows, self.items, self.glossary)
        self.assertEqual(errors, [])
        self.assertEqual(len(ok), 4)

    def test_bad_ids_duplicates_and_missing_rows_are_rejected(self):
        rows = [self.row('p:b000'), self.row('p:b000'), self.row('p:b999'), self.row('p:b002')]
        ok, errors = structure.validate_rows(rows, self.items, self.glossary)
        self.assertNotIn('p:b000', ok)
        self.assertTrue(any('unknown block_id' in e for e in errors))
        self.assertTrue(any('appears 2 times' in e for e in errors))
        self.assertTrue(any('p:b003 missing' in e for e in errors))

    def test_invented_text_and_supplied_times_are_rejected(self):
        rows = [self.row('p:b000', expansion='Powered descent at 104:30'),
                self.row('p:b001', expansion='Throttle to full thrust'),
                self.row('p:b002', kind='pad_field', title='TIG 104:28:55'),
                self.row('p:b003', kind='event_marker')]
        ok, errors = structure.validate_rows(rows, self.items, self.glossary)
        self.assertEqual(set(ok), {'p:b003'})
        self.assertTrue(any('supplies a time' in e for e in errors))
        self.assertTrue(any('not from the glossary' in e for e in errors))
        self.assertTrue(any('unexpected field title' in e for e in errors))

    def test_groups_must_be_contiguous_within_one_column(self):
        rows = [self.row('p:b000'), self.row('p:b001'), self.row('p:b002', kind='pad_field', activity_group='p:b000'),
                self.row('p:b003', activity_group='p:b000')]
        ok, errors = structure.validate_rows(rows, self.items, self.glossary)
        self.assertNotIn('p:b002', ok)
        self.assertNotIn('p:b003', ok)
        self.assertTrue(any('another column' in e for e in errors))
        self.assertTrue(any('not contiguous' in e for e in errors))

    def test_rules_label_codes_ruler_labels_and_pads(self):
        geo = {'height': 850, 'footer_y': 728}

        def label(text, column='LM', y=300):
            return structure.rule_label({'text': text, 'column': column, 'bbox_px': f'400,{y},500,{y + 12}'}, {}, geo, [])
        self.assertEqual(label('(22112)', 'ruler')[0], 'code')
        self.assertEqual(label(':10', 'ruler')[0], 'noise')
        self.assertEqual(label('TIG: 104:28:54.8', 'NOTES')[0], 'pad_field')
        self.assertEqual(label('CSM SYSTEMS CHECKLIST')[0], 'checklist_ref')
        self.assertEqual(label('APOLLO 15 FINAL (7/26)', y=740)[0], 'noise')
        self.assertIsNone(label('DPS THROTTLE CHECK'))

    def test_burn_fields_and_printed_times(self):
        f = structure.burn_fields('TIG: 104:28:54.8 BT: 12 MIN, 2.1 SEC ΔVR: 6700 FT/SEC ULLAGE: 4 JET 7.5 SEC')
        self.assertEqual(f['tig'], '104:28:54.8')
        self.assertEqual(f['dv'], '6700 FT/SEC')
        self.assertEqual(f['ullage'], '4 JET 7.5 SEC')
        self.assertEqual(structure.printed_get('LM LUNAR TOUCHDOWN GET: 104:40:57'), 104 * 3600 + 40 * 60 + 57)
        self.assertIsNone(structure.printed_get('V48 (11102)'))

    def test_3_288_annotation_is_layer_2_evidence(self):
        """The LM JETTISON PAD value 177:20:33 is written in by hand; the ALSJ scan shows the box empty."""
        blocks = [{'block_id': '3-288:b040', 'kind': 'annotation', 'origin': 'annotation', 'activity_group': '',
                   'page_id': '3-288', 'get_planned': '', 'text': '177 : 20 : 33'}]
        self.assertTrue(all(b['origin'] != 'printed' for b in blocks if b['kind'] == 'annotation'))
        events = structure.build_layer1(blocks, {}, {}, {})[1]
        self.assertEqual(events, [])  # an annotation never becomes a planned event


class AsFlownTests(unittest.TestCase):
    def test_slip_map_interpolates_and_holds_at_the_ends(self):
        at = actual.slip_map([(1000, 60), (3000, 260)])
        self.assertEqual(at(2000)[0], 160.0)
        self.assertEqual(at(500), (60.0, 500))
        self.assertEqual(at(5000), (260.0, 2000))
        self.assertGreater(actual.uncertainty(7200), actual.uncertainty(60))

    def test_reversed_anchors_are_flagged(self):
        self.assertEqual(actual.check_monotonic([(1000, 0), (2000, 10)]), [])
        self.assertEqual(len(actual.check_monotonic([(1000, 2000), (2000, 0)])), 1)

    def test_page_references_resolve_by_time(self):
        hours = {'3-084': 60 * 3600, '3-384': 270 * 3600}
        self.assertEqual(actual.resolve_page('384', 59 * 3600, hours), '3-084')
        self.assertEqual(actual.resolve_page('384', 269 * 3600, hours), '3-384')
        self.assertEqual(actual.resolve_page('3-126', None, hours), '3-126')
        self.assertEqual(actual.change_kind('delete the P52 at 104:10'), 'deleted')
        self.assertEqual(actual.change_kind('move the eat period to 105:00 instead of 104:30'), 'time_change')


class TocTests(unittest.TestCase):
    def row(self, toc_id, seconds, level=1, basis='mission_report', key='x', label='Item'):
        return {'toc_id': toc_id, 'key': key, 'seconds': seconds, 'level': level, 'label': label, 'description': '',
                'source': toc_id, 'get_basis': basis, 'evidence': 'e'}

    def test_only_actual_times_reach_the_toc(self):
        kept, dropped = toc.build([self.row('a', 10), self.row('b', 100, basis='slip_interpolated'),
                                   self.row('c', 200, basis='planned'), self.row('d', 300, basis='utterance')])
        self.assertEqual([r['toc_id'] for r in kept], ['a', 'd'])
        self.assertEqual({d['toc_id'] for d in dropped}, {'b', 'c'})

    def test_no_duplicates_within_30_s_at_one_level(self):
        kept, dropped = toc.build([self.row('mr', 1000), self.row('afj', 1020, basis='afj_index'),
                                   self.row('l2', 1010, level=2)])
        self.assertEqual([r['toc_id'] for r in kept], ['mr', 'l2'])
        self.assertEqual(kept[0]['alternatives'], ['afj@000:17:00'])

    def test_recorded_edits_are_reapplied(self):
        rows = [self.row('a', 10, label='Lift-off'), self.row('b', 20, label='Tower clear', level=2)]
        out = toc.apply_decisions(rows, {'a': {'new_label': 'Launch'}, 'b': {'decision': 'drop'}})
        self.assertEqual([(r['toc_id'], r['label']) for r in out], [('a', 'Launch')])

    def test_time_ids_follow_airt2(self):
        self.assertEqual(toc.time_id(0), '0000000')
        self.assertEqual(toc.time_id(104 * 3600 + 42 * 60 + 29), '1044229')
        self.assertEqual(toc.time_id(-(35 * 3600 + 17 * 60 + 28)), '-351728')
        self.assertEqual(toc.time_id(-5400), '-013000')
        self.assertEqual(toc.plain('A <b>bold</b> | label'), 'A bold / label')


if __name__ == '__main__':
    unittest.main()
