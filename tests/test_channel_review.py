"""Bounded stage-03 v1 cleanup cache integration."""
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

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
from _bootstrap import install as _install
_install()
from channel_review import cleanup_channel, cleanup_block, cleanup_results, ensure_denoised


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



if __name__=='__main__':
    unittest.main()
