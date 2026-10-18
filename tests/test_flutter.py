"""Regression checks for the accepted shared reference correction."""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
import numpy as np
import soundfile as sf

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
from flutter import estimate, positions, resample_at
from tone_adjacent_trial import synthetic
spec=importlib.util.spec_from_file_location('deflutter_stage',ROOT/'scripts/05b_deflutter.py')
stage=importlib.util.module_from_spec(spec);spec.loader.exec_module(stage)


class AcceptedFlutterTests(unittest.TestCase):
    def test_interfering_hum_and_independent_tone(self):
        result=synthetic()
        self.assertGreater(result['correlation'],.99)
        self.assertLess(result['rendered_independent_tone_residual_ratio'],.1)

    def test_identity_and_invalid_maps(self):
        sr=8000;n=sr*15;pos=positions(np.zeros(3000),sr,n)
        np.testing.assert_allclose(pos,np.arange(n),atol=1e-8)
        t=np.arange(n)/sr;x=np.sin(2*np.pi*700*t)
        self.assertLess(max(abs(resample_at(x,pos)[100:-100]-x[100:-100])),.001)
        for e in [np.full(3000,np.nan),np.ones(3000),np.zeros(20)]:
            with self.assertRaises(ValueError):positions(e,sr,n)
        with self.assertRaises(ValueError):estimate(np.zeros(n),sr)

    def test_batch_preflight_and_reference_preservation(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);source=root/'input';source.mkdir();out=root/'output'
            sr=8000;t=np.arange(15*sr)/sr;tau=t-.025/(2*np.pi*6.5)*np.cos(2*np.pi*6.5*t)
            sf.write(root/'reference.wav',np.sin(2*np.pi*631.5*tau),sr,subtype='FLOAT')
            sf.write(source/'voice.wav',np.sin(2*np.pi*1000*tau),sr,subtype='FLOAT')
            original=(root/'reference.wav').read_bytes()
            config=dict(tape='synthetic',start_seconds=0,duration_seconds=15,reference=dict(path='reference.wav'),targets=[dict(file='voice.wav',start_seconds=1)])
            path=root/'config.json';path.write_text(json.dumps(config))
            with self.assertRaisesRegex(ValueError,'same source start'):stage.process(source,out,path)
            self.assertFalse(out.exists())
            config['targets'][0]['start_seconds']=0;path.write_text(json.dumps(config))
            r=stage.process(source,out,path)
            self.assertEqual(original,(root/'reference.wav').read_bytes())
            self.assertEqual(r['targets'][0]['frames'],len(t))
            self.assertLess(r['targets'][0]['gain'],1)
            self.assertAlmostEqual(r['targets'][0]['output_peak'],10**(-3/20),places=6)
            self.assertTrue((out/'timing_map.npz').exists())
            with self.assertRaisesRegex(ValueError,'empty'):stage.process(source,out,path)

    def test_matches_frozen_accepted_map(self):
        evidence=ROOT/'reports/accepted_flutter'
        reference=Path(json.loads((evidence/'results.json').read_text())['reference_source'])
        if not reference.exists():self.skipTest('Local delivery reference unavailable')
        from flutter import read_excerpt
        x,sr=read_excerpt(reference,80,50);tt,e,_,_=estimate(x,sr)
        frozen=np.load(evidence/'inputs/accepted_timing_map.npz')
        np.testing.assert_allclose(e,frozen['speed_error'],atol=1e-12,rtol=0)
        np.testing.assert_allclose(positions(e,sr,len(x)),frozen['input_sample_positions'],atol=1e-9,rtol=0)

if __name__=='__main__':unittest.main()
