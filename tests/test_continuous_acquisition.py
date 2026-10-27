"""Sparse-event acquisition grows without reusing failed validation as training."""
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from continuous_acquisition import AcquisitionEvidence, local_roles


def events(starts):
    return [dict(start=float(value), end=float(value)+.25) for value in starts]


class AcquisitionEvidenceTests(unittest.TestCase):
    def test_sparse_events_bootstrap_over_multiple_monitors(self):
        ledger = AcquisitionEvidence()
        ledger.ingest(15, events([10, 50]))
        self.assertEqual(ledger.propose(15, 60)['status'], 'waiting_for_independent_evidence')
        ledger.ingest(15, events([70, 110]))
        proposal = ledger.propose(15, 120)
        self.assertEqual(proposal['status'], 'ready_for_search')
        self.assertEqual(proposal['horizon_seconds'], 120.)
        self.assertEqual([e['start_seconds'] for e in proposal['roles']['calibration']], [10, 50])
        self.assertEqual([e['start_seconds'] for e in proposal['roles']['validation']], [70, 110])
        self.assertFalse(proposal['correction_approved'])
        self.assertEqual((proposal['required_support_start'], proposal['required_support_end']), (10, 110.25))

    def test_overlapping_reads_and_checkpoint_do_not_change_event_roles(self):
        one, split = AcquisitionEvidence(), AcquisitionEvidence()
        all_events = events([10, 20, 40, 70, 90, 110, 140, 170, 200, 230])
        one.ingest(15, all_events)
        split.ingest(15, all_events[:4])
        split = AcquisitionEvidence.restore(json.loads(json.dumps(split.state())))
        split.ingest(15, all_events[2:])
        left = one.propose(15, 240)['roles']
        right = split.propose(15, 240)['roles']
        self.assertEqual(left, right)
        self.assertTrue(all(e['role'] == 'final' for e in right['final']))
        for name, values in right.items():
            self.assertTrue(all(e['role'] == name for e in values))

    def test_failed_passage_is_consumed_before_any_retry(self):
        ledger = AcquisitionEvidence()
        ledger.ingest(15, events([10, 20, 30, 40, 50]))
        first = ledger.propose(15, 60)
        self.assertEqual(first['status'], 'ready_for_search')
        final = first['roles']['final'][0]
        ledger.mark_attempt(15, 60)
        ledger.ingest(15, events([70, 80]))
        self.assertEqual(ledger.propose(15, 120)['status'], 'waiting_for_independent_evidence')
        ledger.ingest(15, events([130, 140]))
        later = ledger.propose(15, 180)
        self.assertEqual(later['status'], 'ready_for_search')
        self.assertTrue(all(e['start_seconds'] >= 60 for group in later['roles'].values() for e in group))
        self.assertEqual(final['role'], 'final')

    def test_channel_copies_never_create_independent_target_evidence(self):
        ledger = AcquisitionEvidence()
        for channel in (12, 13, 14, 15):
            ledger.ingest(channel, events([10]))
        for channel in (12, 13, 14, 15):
            self.assertEqual(ledger.propose(channel, 60)['status'], 'waiting_for_independent_evidence')

    def test_one_beep_margins_or_close_repeats_are_not_multiple_events(self):
        ledger = AcquisitionEvidence()
        accepted = ledger.ingest(15, events([10, 10.02, 10.04, 11, 13.1]))
        self.assertEqual(len(accepted), 2)
        self.assertEqual(ledger.propose(15, 60)['status'], 'waiting_for_independent_evidence')

    def test_evidence_older_than_bounded_horizon_does_not_qualify(self):
        ledger = AcquisitionEvidence()
        ledger.ingest(15, events([10, 20]))
        ledger.ingest(15, events([370, 380]))
        self.assertEqual(ledger.propose(15, 400)['status'], 'waiting_for_independent_evidence')

    def test_local_coordinates_and_context_preserve_source_offsets(self):
        ledger = AcquisitionEvidence(context_seconds=4)
        ledger.ingest(15, events([10, 20, 30, 40, 50]), origin=100)
        proposal = ledger.propose(15, 160, available_start=100, available_end=170)
        roles = local_roles(proposal, 96)
        self.assertEqual(roles[0][0]['start'], 14.)
        self.assertEqual(roles[2][0]['start_seconds'], 150.)
        self.assertEqual(proposal['analysis_start'], 100.)
        self.assertEqual(proposal['analysis_end'], 164.)


if __name__ == '__main__':
    unittest.main()
