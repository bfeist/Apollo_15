from contextlib import ExitStack
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
from scipy import signal
import soundfile as sf

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts/04_crosstalk'))
import repeat_archive
import repeat_cluster
import repeat_qualification
from repeat_periodicity import DelayWitnesses, RATE, in_domain, witness
from tests.test_shared_tape import fixture


class QualificationTests(unittest.TestCase):
    def test_line_rejected_speech_survives_and_periodic_alias_bypasses(self):
        rng = np.random.default_rng(17)
        ref = signal.sosfilt(signal.butter(3,[350,2700],fs=RATE,btype='band',output='sos'),
                             rng.normal(size=RATE//2))
        raw = np.zeros(10*RATE)
        donor = raw.copy()
        first, gap, delta = 4*RATE, 10951, 39
        raw[first:first+len(ref)] = -.22*ref
        raw[first+gap:first+gap+len(ref)] = ref
        donor[first+delta:first+delta+len(ref)] = ref
        hit = dict(early=first/RATE,later=(first+gap)/RATE,early_path=delta/RATE,
                   late_path=(delta-gap)/RATE,later_db=0.)
        with tempfile.TemporaryDirectory() as tmp:
            a,b = [Path(tmp)/n for n in ['a.wav','b.wav']]
            sf.write(a,raw, RATE,subtype='FLOAT')
            sf.write(b,donor,RATE,subtype='FLOAT')
            with sf.SoundFile(a) as y, sf.SoundFile(b) as x:
                result, reason = witness(hit,y,x,0,0)
            self.assertIsNone(reason)
            self.assertAlmostEqual(result['later_db'],20*np.log10(1/.22),places=2)
            # Identical repeating clock pulses carry energy in many speech bands.
            periodic = np.tile(rng.normal(size=80),1000)
            sf.write(a,periodic,RATE,subtype='FLOAT')
            with sf.SoundFile(a) as y, sf.SoundFile(a) as x:
                result, _ = witness(dict(hit,later=5.,early_path=0.,late_path=-1.),y,x,0,0)
            self.assertIsNone(result)

    def test_renderer_domain_and_independent_timing_control(self):
        rows = {ch:dict(row=dict(role='timing_only' if ch==1 else 'reference_and_target'))
                for ch in (1,2,3,4)}
        def event(t,gap):
            return dict(early=t,later=t+gap,early_path=0.,late_path=-gap,donor=4)
        test = event(600.,1.37)
        self.assertFalse(in_domain(event(600.,.8)))
        self.assertTrue(in_domain(test))
        only_clock = DelayWitnesses({1:[event(100.,1.37),event(350.,1.37)]},rows)
        self.assertFalse(only_clock.lookup(test,True))
        one_voice = DelayWitnesses({2:[event(100.,1.368),event(350.,1.372)]},rows)
        self.assertTrue(one_voice.lookup(test,False))
        self.assertFalse(one_voice.lookup(test,True))
        voices = DelayWitnesses({2:[event(100.,1.368)],3:[event(350.,1.372)]},rows)
        self.assertTrue(voices.lookup(test,True))
        self.assertFalse(voices.lookup(event(600.,.9167),True))

    def test_empty_qualified_catalog_reopens_with_the_official_renderer(self):
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            root=Path(tmp)
            _,_,inv=fixture(root)
            stack.enter_context(patch.object(repeat_archive, 'capture_coordinates', return_value={}))
            tape=inv['tape']
            for key,folder in [('CANONICAL','denoised'),('ORIGINALS','originals')]:
                stack.enter_context(patch.object(repeat_archive,key,root/folder))
            stack.enter_context(patch.object(repeat_cluster,'SSD',root/'ssd'))
            stack.enter_context(patch.object(repeat_cluster,'render_archive'))
            stack.enter_context(patch.object(repeat_qualification,'HOLD',root/'held'))
            raw,dest=root/'raw'/'trial',root/'qual'/'trial'
            repeat_cluster.run(raw,[tape],local=2,genus=0,tape_limit=1)
            frozen=repeat_qualification.frozen_qualification(dest,raw)
            repeat_qualification.qualify_tape(raw/tape,dest/tape,frozen)
            self.assertTrue(repeat_qualification.qualified(dest/tape))
            for ch in (1,2,20):
                _,events,_=repeat_qualification.evidence_for(dest/tape/'catalog'/f'CH{ch:02d}',tape,ch)
                self.assertFalse(events)
