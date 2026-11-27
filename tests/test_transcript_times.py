"""Stage 07b checks the mission transcripts' GETs against the words heard on retimed audio."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import _bootstrap  # noqa: E402,F401
import transcript_times as tt  # noqa: E402

WORDS = 'roger houston we copy your burn report and thanks a lot'.split()


def event(t, words, source='TEC', event_id='e1', basis='printed'):
    return dict(source=source, event_id=event_id, chapter='p0001', who='CC', basis=basis, t=t, tokens=list(words),
                text=' '.join(words))


def stream(start, words=WORDS, step=0.3):
    return np.array(words), start + step * np.arange(len(words))


class MatchTests(unittest.TestCase):
    def test_heard_onset_minus_printed_get(self):
        tok, get = stream(1000.4)
        rows = tt.match(tok, get, np.full(len(tok), 'timecode'), [event(1000, WORDS)])
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0]['d_s'], 0.4)
        self.assertEqual(rows[0]['timing'], 'timecode')

    def test_a_run_heard_twice_in_the_window_is_not_a_match(self):
        a, ga = stream(1000.0)
        b, gb = stream(1030.0)
        rows = tt.match(np.r_[a, b], np.r_[ga, gb], np.full(2 * len(a), 'timecode'), [event(1000, WORDS)])
        self.assertEqual(rows, [])

    def test_following_words_must_agree(self):
        heard = WORDS[:4] + 'something else entirely different here now'.split()
        tok, get = stream(1000.0, heard)
        self.assertEqual(tt.match(tok, get, np.full(len(tok), 'timecode'), [event(1000, WORDS)]), [])

    def test_words_without_get_are_skipped(self):
        tok, get = stream(1000.0)
        get[:] = np.nan
        self.assertEqual(tt.match(tok, get, np.full(len(tok), 'timecode'), [event(1000, WORDS)]), [])

    def test_summary_keeps_sg_fit_timing_out_of_the_independent_statistics(self):
        rows = [dict(source='TEC', event_id=f'e{k}', basis='printed', timing='timecode' if k % 2 else 'sg_fit',
                     get_s=1000 + 10 * k, get=str(k), d_s=0.3 if k % 2 else 9.0, channel='CH01', text='')
                for k in range(20)]
        s = tt.summarize(rows)
        self.assertEqual(s['sources']['TEC']['n'], 10)
        self.assertAlmostEqual(s['sources']['TEC']['median_s'], 0.3)
        self.assertIn('TEC:printed:sg_fit', s['not_independent'])

    def test_a_shifted_stretch_is_reported(self):
        rows = [dict(source='TEC', event_id=f'e{k}', basis='printed', get_s=10 * k, get=str(k),
                     d_s=5.0 if 20 <= k < 40 else 0.4, chapter=f'p{k // 10}') for k in range(60)]
        runs = tt.stretches(rows)
        self.assertEqual(len(runs), 1)
        self.assertAlmostEqual(runs[0]['median_d_s'], 5.0)

    def test_a_channel_on_the_wrong_head_row_stands_out(self):
        per = {}
        for ch, late in (('CH01', 0.0), ('CH02', 0.0), ('CH03', 1.38)):
            per[ch] = [dict(source='TEC', event_id=f'e{k}', chapter='p1', who='CC', basis='printed', get=str(k),
                            get_s=10 * k, heard_get_s=0, d_s=0.4 + late, timing='timecode', channel=ch, text='',
                            heard='') for k in range(12)]
        events, channels, flagged = tt.combine(per)
        self.assertEqual(len(events), 12)
        self.assertAlmostEqual(events[0]['d_s'], 0.4)
        self.assertEqual(flagged, ['CH03'])


class ChannelCheckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.out = root / 'retimed' / 'A15_T1' / 'CH03'
        self.out.mkdir(parents=True)
        plan = root / 'plan.json'
        plan.write_text(json.dumps(dict(method='irig_timecode+sg_hybrid', version='irig-timing-v2',
                                        support=dict(covered_intervals_s=[[0.0, 50.0]]))))
        (self.out / 'retime.json').write_text(json.dumps(dict(status='complete', shared_plan=dict(path=str(plan)))))
        lines = ['\t'.join(('kind', 'segment', 'word', 'text', 'get_start_s', 'source_start_s', 'probability',
                            'flags')), '\t'.join(('segment', '0', '', ' '.join(WORDS), '', '', '-0.2', 'aligned'))]
        for k, w in enumerate(WORDS):  # canonical origin 10 s: words at canonical 30-33 s (inside the time code)
            lines.append('\t'.join(('word', '0', str(k), w, f'{2000.6 + 0.3 * k:.4f}', f'{20 + 0.3 * k:.4f}', '0.9',
                                    '')))
        (self.out / 'timed_transcript.tsv').write_text('\n'.join(lines) + '\n', encoding='utf-8')
        (self.out / 'timed_transcript.json').write_text(json.dumps(dict(
            tsv_sha256='abc', time_basis=dict(canonical_origin_s=10.0))))
        self.events = [event(2000, WORDS)]

    def tearDown(self):
        self.tmp.cleanup()

    def run_check(self, state):
        with mock.patch.object(tt, 'mission_state', return_value=state), \
                mock.patch.object(tt, 'load_events', return_value=self.events):
            return tt.check_channel(self.out, progress=lambda _: None)

    def test_matches_are_written_beside_the_timed_transcript(self):
        out = self.run_check({'transcript_tec.tsv': 'a'})
        self.assertEqual(out['summary']['sources']['TEC']['n'], 1)
        self.assertAlmostEqual(out['summary']['sources']['TEC']['median_s'], 0.6)
        rows = (self.out / 'transcript_times.tsv').read_text(encoding='utf-8').splitlines()
        self.assertEqual(len(rows), 2)
        self.assertIn('\ttimecode\t', rows[1])

    def test_reused_and_superseded_when_the_journals_change(self):
        self.run_check({'transcript_tec.tsv': 'a'})
        stamp = (self.out / 'transcript_times.json').stat().st_mtime_ns
        self.run_check({'transcript_tec.tsv': 'a'})
        self.assertEqual((self.out / 'transcript_times.json').stat().st_mtime_ns, stamp)
        self.run_check({'transcript_tec.tsv': 'b'})
        self.assertEqual(len(list(self.out.glob('transcript_times.superseded-*.json'))), 1)

    def test_no_mission_data_skips_the_check(self):
        self.assertIsNone(self.run_check(None))
        self.assertFalse((self.out / 'transcript_times.json').exists())


if __name__ == '__main__':
    unittest.main()
