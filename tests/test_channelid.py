"""Stage 10 channel identification: solver, readout detection and filename parsing."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts' / '30-track' / '10_channelid'))

import channelid_evidence as ce  # noqa: E402
import channelid_report as cr  # noqa: E402
import channelid_shared as csh  # noqa: E402
import channelid_solve as cv  # noqa: E402
from channelid_sheets import sheet  # noqa: E402


def glm(true, p=0.8):
    return dict(candidates=[dict(channel=true, p=p)], summary='', callsigns=[], evidence=[])


def evidence(words, anchor=None, ranking=None):
    ranking = ranking if ranking is not None else ([[anchor, 50]] if anchor else [])
    return dict(channels={str(d): dict(words=w) for d, w in words.items()},
                anchors=dict(channel=anchor, share=0.9 if anchor else 0.0, ranking=ranking))


class SolveTests(unittest.TestCase):
    def test_rigid_reversal_is_recovered(self):
        a = {d: glm(33 - d if 1 <= 33 - d <= 30 else 0) for d in range(1, 31)}
        sol = cv.solve(a)
        self.assertEqual(sol['direction'], 'opposite')
        self.assertEqual(sol['rule'], 'true = 33 - delivered')
        self.assertEqual(sol['channels'][13]['true'], 20)
        self.assertIsNone(sol['channels'][1]['true'])

    def test_identity_is_recovered(self):
        sol = cv.solve({d: glm(d) for d in range(1, 31)})
        self.assertEqual(sol['rule'], 'true = delivered')
        self.assertEqual(sol['direction'], 'same')

    def test_one_step_drift(self):
        truth = {d: (34 - d if d <= 8 else 33 - d) for d in range(1, 31)}
        sol = cv.solve({d: glm(t if 1 <= t <= 30 else 0, 0.9) for d, t in truth.items()})
        self.assertEqual([s['offset'] for s in sol['segments']], [34, 33])
        self.assertEqual(sol['channels'][4]['true'], 30)
        self.assertEqual(sol['channels'][13]['true'], 20)

    def test_isolated_wrong_guess_does_not_bend_the_map(self):
        a = {d: glm(33 - d if 1 <= 33 - d <= 30 else 0) for d in range(1, 31)}
        a[15] = glm(5, 0.9)
        sol = cv.solve(a)
        self.assertEqual(sol['rule'], 'true = 33 - delivered')
        self.assertEqual(sol['channels'][15]['true'], 18)
        self.assertEqual(sol['channels'][15]['agreement'], 'conflict')

    def test_anchor_and_weak_bleed_break_a_tie(self):
        # GLM names 30 on both CH03 (weak bleed) and CH04; everything else is uninformative.
        a = {d: dict(candidates=[dict(channel=0, p=0.01)], summary='', callsigns=[], evidence=[])
             for d in range(1, 31)}
        a[3] = glm(30, 0.95)
        a[4] = glm(30, 0.9)
        words = {d: 20000 for d in range(1, 31)}
        words[3] = 1000
        sol = cv.solve(a, evidence(words, anchor=4))
        self.assertEqual(sol['channels'][4]['true'], 30)
        self.assertTrue(sol['anchor_consistent'])


    def test_anchor_rules(self):
        self.assertEqual(cv.anchor_of(dict(ranking=[[4, 155], [3, 29]], share=0.84)), (4, 'clear'))
        self.assertEqual(cv.anchor_of(dict(ranking=[[23, 1]], share=1.0))[0], None)
        self.assertTrue(cv.anchor_of(dict(ranking=[[3, 102], [2, 82]], share=0.54))[1].startswith('ambiguous'))

    def test_offset_steps_prefer_skips(self):
        truth = {d: (34 - d if d <= 8 else 33 - d) for d in range(1, 31)}
        a = {d: glm(t if 1 <= t <= 30 else 0, 0.9) for d, t in truth.items()}
        a[29] = glm(34 - 29, 0.6)           # one late channel suggesting a step back up
        sol = cv.solve(a)
        self.assertEqual([s['offset'] for s in sol['segments']], [34, 33])


class SharedTests(unittest.TestCase):
    def test_mapping_counts_down_and_skips(self):
        m = csh.mapping(4, (15,), range(1, 31))
        self.assertEqual([m[d] for d in (1, 3, 4, 5)], [0, 0, 30, 29])
        self.assertEqual(m[18], 16)
        self.assertEqual(m[19], 14)          # true 15 skipped
        self.assertEqual(m[30], 3)

    def test_irig_anchors_true_ch01(self):
        m = csh.mapping(2, (15,), range(1, 31))
        self.assertEqual((m[29], m[30]), (2, 1))
        a = {d: glm(t or 0, 0.6) for d, t in m.items()}
        a[30] = dict(candidates=[dict(channel=0, p=0.9)], summary='', callsigns=[], evidence=[])
        irig = dict(searched=[28, 29, 30], channels={'30': dict(strict=50000)})
        lp, notes = cv.emissions(a, None, irig)
        self.assertGreater(lp[30][1], lp[30][0])
        self.assertLess(lp[29][1], -6)
        self.assertIn('IRIG', notes[30])

    def test_group_pattern_recovered(self):
        msets = csh.missing_sets()
        tapes = []
        for d0 in (2, 3, 4):
            truth = csh.mapping(d0, (15,), range(1, 31))
            lp = {d: cv.channel_logp_array(glm(t, 0.7)['candidates']) for d, t in truth.items()}
            t = dict(lp=lp)
            t['S'] = csh.tape_table(t, msets)
            tapes.append(t)
        fit = csh.group_fit(tapes, msets)
        self.assertEqual(msets[fit['best']], (15,))


class EvidenceTests(unittest.TestCase):
    def test_readout_lines(self):
        self.assertTrue(ce.annotation_score('Channel 14, Capcom Left, Position 3010.'))
        self.assertTrue(ce.annotation_score('On HL1, Channel 1, Timing IRIG-B.'))
        self.assertTrue(ce.annotation_score('Position 30, 09.'))
        self.assertFalse(ce.annotation_score('Flight, FIDO, we are go for the burn.'))
        self.assertFalse(ce.annotation_score('The key note is that the Capcom will call up channel 17 for the crew'))

    def test_excerpt_skips_broadcast(self):
        rows = [dict(t=10.0 * i, text=f'own line {i} trajectory', shared=[]) for i in range(5)]
        rows += [dict(t=10.0 * i + 1, text='air to ground everyone hears', shared=[1, 2, 3, 4, 5]) for i in range(5)]
        out = ce.excerpt(rows)
        self.assertTrue(out)
        self.assertTrue(all('everyone' not in text for _, text, _ in out))


class ReportTests(unittest.TestCase):
    def test_filename(self):
        f = cr.parse_filename('A15_T510093_HR1U_CH30_VOICE_TECH_213-04-57-00_213-22-12-00.wav')
        self.assertEqual((f['channel'], f['label'], f['head']), (30, 'VOICE_TECH', 'HR1U'))

    def test_true_view_lists_every_track_and_reasons(self):
        sh = sheet('HR1')
        mp = csh.mapping(4, (15,), range(1, 31))
        chans = {str(d): dict(delivered_channel=d, delivered_label=f'L{d}', true_channel=t or None,
                              confidence='high', posterior=1.0) for d, t in mp.items()}
        rows = cr.true_view(sh, chans, [15], dict(searched=[28, 29, 30], channels={}))
        self.assertEqual(len(rows), 30)
        by = {r['true_channel']: r for r in rows}
        self.assertEqual(by[30]['delivered_channel'], 4)
        self.assertEqual(by[1]['status'], 'missing')
        self.assertIn('IRIG time code not found on delivered CH28, CH29, CH30', by[1]['reason'])
        self.assertIn('gap', by[15]['reason'])
        self.assertEqual(by[2]['status'], 'missing')         # 4 + 27 runs past CH30
        self.assertIn('beyond', by[2]['reason'])

    def test_sheets_match_filename_labels(self):
        self.assertEqual(sheet('HR1')[13]['label'], 'SURGEON_RT')
        self.assertEqual(sheet('HR2')[23]['label'], 'INCO')
        self.assertEqual(len(sheet('HR1')), 30)
        self.assertEqual(len(sheet('HR2')), 30)


if __name__ == '__main__':
    unittest.main()
