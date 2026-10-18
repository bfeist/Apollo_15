"""Continuous correction, bounded cleanup, and review artifact integration."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
import soundfile as sf
from scipy.integrate import cumulative_trapezoid
from scipy.signal import hilbert

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
from channel_review import continuous_map, cleanup_channel, cleanup_block, cleanup_results, prepare, render_channel_review, render_warp, ensure_denoised
from flutter import resample_at


def fixture(root, actions=None, phases=None, gap=False):
    sr, duration = 8000, 60
    source = root/'originals'; source.mkdir()
    folder = root/'plan'; folder.mkdir()
    times = np.arange(sr*duration)/sr
    error = .015*np.sin(2*np.pi*6.5*times)
    delay = cumulative_trapezoid(error,times,initial=0)
    x = .3*np.sin(2*np.pi*700*(times+delay))
    path = source/'channel.wav'
    sf.write(path,x,sr,subtype='FLOAT')
    stat = path.stat()
    row = dict(channel=2,path=str(path),role='reference_and_target',bytes=stat.st_size,
               mtime_ns=stat.st_mtime_ns,sample_rate=sr,frames=len(x),start_seconds=0,end_seconds=duration)
    actions = actions or ['render_and_review']*3
    phases = phases or [0]*3
    regions = []
    for i in range(3):
        start, end = i*20,(i+1)*20
        a,b = max(0,start-4),min(duration,end+4)
        t = a+np.arange(round((b-a)*200))/200
        e = .015*np.sin(2*np.pi*6.5*t+phases[i]); support = np.ones(len(t))
        if gap and i==1:
            support[(t>=28)&(t<32)]=0; e*=support
        name = f'r{i:05d}'
        map_path = folder/(name+'_maps.npz')
        np.savez(map_path,reference=e,reference__support=support)
        target = dict(channel=2,action=actions[i],selected='reference',reason='Synthetic test evidence',
                      reference_spans=[dict(start_seconds=start,end_seconds=end,action='use_reference')])
        detail = dict(id=name,start_seconds=start,end_seconds=end,analysis_start=a,analysis_end=b,status='analyzed',
                      targets=[target],maps=dict(file=map_path.name,rate=200,sha256=hashlib.sha256(map_path.read_bytes()).hexdigest()))
        detail_path = folder/(name+'.json'); detail_path.write_text(json.dumps(detail))
        regions.append(dict(detail,detail_file=detail_path.name,detail_sha256=hashlib.sha256(detail_path.read_bytes()).hexdigest()))
    plan = dict(schema=1,complete=True,source_validation='verified',full_tape=True,
                inventory=dict(tape='synthetic',channels=[row]),regions=regions)
    plan_path = folder/'plan.json'; plan_path.write_text(json.dumps(plan))
    return plan_path,plan,row


class ChannelReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)

    def test_continuous_correction_reduces_known_flutter_and_matches_monolithic_render(self):
        path,plan,row=fixture(self.root)
        tt,nominal,speed,stats,markers,_=continuous_map(path.parent,plan['regions'],2,0,row['frames'],8000,progress=lambda _:None)
        self.assertEqual(sum(m['kind']=='blend' for m in markers),2)
        self.assertEqual(nominal[0],0); self.assertEqual(nominal[-1],tt[-1])
        self.assertTrue(np.all(np.diff(nominal)>0))
        output=self.root/'corrected.wav'
        render_warp(row['path'],output,tt,nominal,block_seconds=.71,progress=lambda _:None)
        x,sr=sf.read(row['path']); y,_=sf.read(output)
        fx=np.diff(np.unwrap(np.angle(hilbert(x))))*sr/(2*np.pi)
        fy=np.diff(np.unwrap(np.angle(hilbert(y))))*sr/(2*np.pi)
        core=slice(4*sr,56*sr)
        # Raw instantaneous frequency also sees the 200 Hz piecewise-linear map.
        # Require >90% reduction without smoothing that interpolation error away.
        self.assertLess(np.std(fy[core])/np.std(fx[core]),.1)
        pos=np.interp(np.arange(len(x))/sr,nominal,tt)*sr
        expected=resample_at(x,pos)
        np.testing.assert_allclose(y,expected,atol=3e-8,rtol=0)
        # A different audio chunk grid cannot create timing resets or seams.
        output2=self.root/'other_chunks.wav'
        render_warp(row['path'],output2,tt,nominal,block_seconds=2.31,progress=lambda _:None)
        y2,_=sf.read(output2)
        np.testing.assert_allclose(y,y2,atol=3e-8,rtol=0)

    def test_transfer_opt_in_failed_bypass_support_and_disagreeing_joins(self):
        path,plan,row=fixture(self.root,actions=['render_and_review','validate_transfer','leave_uncorrected'],gap=True)
        tt,_,strict,_,markers,_=continuous_map(path.parent,plan['regions'],2,0,row['frames'],8000,progress=lambda _:None)
        self.assertTrue(np.all(strict[tt>=20]==0))
        tt,nominal,experimental,_,markers,_=continuous_map(path.parent,plan['regions'],2,0,row['frames'],8000,True,lambda _:None)
        self.assertGreater(np.std(experimental[(tt>24)&(tt<27)]),.005)
        np.testing.assert_allclose(experimental[(tt>=28)&(tt<32)],0,atol=1e-12)
        self.assertTrue(np.all(experimental[tt>=40]==0))
        self.assertEqual(sum(m['kind']=='experimental' for m in markers),1)
        self.assertTrue(np.all(np.diff(nominal)>0))

    def test_opposite_curves_fade_at_boundary(self):
        path,plan,row=fixture(self.root,phases=[0,np.pi,0])
        tt,nominal,speed,_,markers,_=continuous_map(path.parent,plan['regions'],2,0,row['frames'],8000,progress=lambda _:None)
        self.assertEqual(sum(m['kind']=='fade' for m in markers),2)
        self.assertEqual(sum(m['kind']=='blend' for m in markers),0)
        for boundary in (20,40):
            at=round(boundary*200)
            self.assertAlmostEqual(speed[at],0,places=12)
            self.assertLess(abs(speed[at-1]),1e-5)
            self.assertLess(abs(speed[at+1]),1e-5)

    def test_overlap_cleanup_preserves_identity_and_handles_offset_short_tail(self):
        _,_,row=fixture(self.root)
        calls=[]
        def tones(x,sr):
            calls.append(len(x)); return x.copy(),{}
        output=self.root/'cleaned.wav'
        with patch('channel_review.clean_tones',side_effect=tones),patch('channel_review.denoise',side_effect=lambda x,sr,cap:(x,[0,4])):
            cleanup_channel(row,3*8000,37*8000+17,output,self.root/'cleanup.jsonl',20,lambda _:None)
        original,_=sf.read(row['path'],start=3*8000,frames=37*8000+17)
        cleaned,_=sf.read(output)
        np.testing.assert_array_equal(original,cleaned)
        self.assertLessEqual(max(calls),28*8000)
        self.assertGreaterEqual(min(calls),15*8000)

    def test_end_to_end_cleanup_precedes_deflutter_and_emits_review(self):
        path,_,row=fixture(self.root,actions=['validate_transfer']*3)
        output=self.root/'review'
        seen=[]
        def tones(x,sr):
            seen.append('tones'); return x*.75,{}
        def noise(x,sr,cap):
            self.assertEqual(seen[-1],'tones'); seen.append('noise')
            self.assertEqual(cap,3); return x*.8,[1,5]
        with patch('channel_review.clean_tones',side_effect=tones),patch('channel_review.denoise',side_effect=noise):
            result=render_channel_review(path,2,output,start=2.345,duration=23.007,progress=lambda _:None)
        self.assertEqual(result['status'],'complete')
        self.assertEqual(result['pipeline'],['tonal_and_residual_hum_removal','broadband_denoise_3dB','continuous_deflutter','constant_peak_normalization'])
        before,_=sf.read(result['denoised_intermediate']['file']); after,_=sf.read(output/'deflutter.wav')
        original,_=sf.read(row['path'],start=round(2.345*8000),frames=len(before))
        np.testing.assert_allclose(before,original*.6,atol=2e-8,rtol=0)
        np.testing.assert_allclose(before*result['gain'],after,atol=3e-8,rtol=0)
        self.assertAlmostEqual(float(np.max(abs(after))),10**(-3/20),places=6)
        self.assertIn('matchLevel', (output/'index.html').read_text())
        self.assertEqual(len(before),round(23.007*8000))
        for name in ('index.html','markers.csv','review.json','cleanup.jsonl','nominal_seconds.npy','speed_error.npy'):
            self.assertTrue((output/name).is_file())
        self.assertFalse(list(output.glob('*.partial')))
        with self.assertRaisesRegex(ValueError,'empty'):
            render_channel_review(path,2,output,progress=lambda _:None)

    def test_parallel_cleanup_matches_serial_with_offset_and_short_tail(self):
        _,_,row=fixture(self.root)
        stats=[]
        for workers in (1, 2):
            stats.append(cleanup_channel(row,3*8000,37*8000+17,
                self.root/f'clean{workers}.wav',self.root/f'log{workers}.jsonl',
                20,lambda _:None,workers=workers))
        self.assertEqual(stats[0],stats[1])
        # FLOAT WAV headers contain creation timestamps; compare decoded samples.
        serial, serial_rate = sf.read(self.root/'clean1.wav', dtype='float32')
        parallel, parallel_rate = sf.read(self.root/'clean2.wav', dtype='float32')
        self.assertEqual(serial_rate, parallel_rate)
        np.testing.assert_array_equal(serial, parallel)
        self.assertEqual((self.root/'log1.jsonl').read_bytes(),(self.root/'log2.jsonl').read_bytes())

    def test_parallel_worker_failure_does_not_publish_cache(self):
        _,_,row=fixture(self.root)
        with sf.SoundFile(row['path'],'r+') as audio:
            audio.seek(25*8000); audio.write(np.full(100,np.nan))
        row['mtime_ns']=Path(row['path']).stat().st_mtime_ns
        cache=self.root/'failed_cache'
        with self.assertRaisesRegex(ValueError,'Invalid source audio block'):
            ensure_denoised(row,cache,0,row['frames'],block_seconds=20,
                            progress=lambda _:None,workers=2)
        self.assertFalse((cache/'denoised.wav').exists())
        self.assertEqual(json.loads((cache/'denoised.json').read_text())['status'],'failed')

    def test_cache_reprocesses_after_implementation_change(self):
        _,_,row=fixture(self.root)
        cache=self.root/'restart_cache'
        with patch('channel_review.clean_tones',side_effect=lambda x,sr:(x,{})),patch('channel_review.denoise',side_effect=lambda x,sr,cap:(x,[0,4])):
            ensure_denoised(row,cache,0,row['frames'],progress=lambda _:None)
            manifest=cache/'denoised.json'
            saved=json.loads(manifest.read_text())
            for status in ('complete', 'failed', 'running'):
                with self.subTest(status=status):
                    saved['signature']['cleanup_sha256']='previous implementation'
                    saved['status']=status
                    manifest.write_text(json.dumps(saved))
                    with self.assertRaisesRegex(ValueError,'different source/settings'):
                        ensure_denoised(row,cache,0,row['frames'],progress=lambda _:None,
                                        require_existing=True)
                    messages=[]
                    result=ensure_denoised(row,cache,0,row['frames'],progress=messages.append)
                    self.assertEqual(result['status'],'complete')
                    self.assertFalse(result['reused'])
                    self.assertNotEqual(result['signature']['cleanup_sha256'],'previous implementation')
                    self.assertTrue(any('reprocessing channel' in m for m in messages))

    def test_preflight_is_read_only_and_corrupt_evidence_stops_before_output(self):
        path,plan,_=fixture(self.root)
        output=self.root/'review'
        result=render_channel_review(path,2,output,dry_run=True,progress=lambda _:None)
        self.assertEqual(result['status'],'preflight'); self.assertFalse(output.exists())
        with self.assertRaisesRegex(ValueError,'overlap'):
            render_channel_review(path,2,path.parent/'rendered',dry_run=True,progress=lambda _:None)
        with self.assertRaisesRegex(ValueError,'non-IRIG'):
            prepare(path,1)
        map_path=path.parent/plan['regions'][0]['maps']['file']
        map_path.write_bytes(map_path.read_bytes()+b'corrupt')
        with self.assertRaisesRegex(ValueError,'artifact changed'):
            render_channel_review(path,2,output,progress=lambda _:None)
        self.assertFalse(output.exists())

    def test_intermediate_reuse_and_corruption_rejection(self):
        _,_,row=fixture(self.root)
        cache=self.root/'temporary'
        with patch('channel_review.clean_tones',side_effect=lambda x,sr:(x,{})),patch('channel_review.denoise',side_effect=lambda x,sr,cap:(x,[0,4])):
            first=ensure_denoised(row,cache,0,row['frames'],progress=lambda _:None)
        before=Path(first['file']).read_bytes()
        real_source = __import__('inspect').getsource
        sources = [real_source(f) for f in (cleanup_block, cleanup_results, cleanup_channel)]
        with patch('channel_review.cleanup_channel',side_effect=AssertionError('Cleanup reran')):
            # Fingerprint the real implementation, rather than the test mock.
            with patch('channel_review.inspect.getsource',side_effect=sources):
                reused=ensure_denoised(row,cache,0,row['frames'],progress=lambda _:None,workers=2)
        self.assertTrue(reused['reused'])
        self.assertEqual(before,Path(reused['file']).read_bytes())
        with self.assertRaisesRegex(ValueError,'different source/settings'):
            ensure_denoised(row,cache,0,row['frames'],block_seconds=30,progress=lambda _:None)
        with sf.SoundFile(first['file'],'r+') as f:
            f.seek(100);f.write(np.ones(50)*.5)
        with self.assertRaisesRegex(ValueError,'intermediate changed'):
            ensure_denoised(row,cache,0,row['frames'],progress=lambda _:None)

    def test_deflutter_requires_prepared_cache_without_silent_cleanup(self):
        path,_,_=fixture(self.root)
        output=self.root/'review'
        with patch('channel_review.cleanup_channel',side_effect=AssertionError('Cleanup reran')):
            with self.assertRaisesRegex(ValueError,'run stage 03'):
                render_channel_review(path,2,output,denoised_input=self.root/'missing',require_prepared=True,progress=lambda _:None)
        self.assertFalse(output.exists())


if __name__=='__main__':
    unittest.main()
