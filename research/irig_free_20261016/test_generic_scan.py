"""Meaningful mapping, concurrency, discovery and negative-control tests."""
import os,tempfile,unittest
from pathlib import Path
import numpy as np
import soundfile as sf
from generic_scan import stream_read,make_manifest,scan_window,seconds

class GenericScannerTests(unittest.TestCase):
    def setUp(self):
        base=Path(__file__).resolve().parent/'test_tmp';base.mkdir(exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=base);self.root=Path(self.temp.name).resolve()
        assert self.root.is_relative_to(base.resolve())
        self.addCleanup(self.temp.cleanup)
    def wav(self,name,x,sr=8000):
        p=self.root/name;p.parent.mkdir(parents=True,exist_ok=True);sf.write(p,x,sr,subtype='PCM_16');os.utime(p,(1_700_000_000,1_700_000_000));return p
    def seg(self,p,start,duration):
        s=p.stat();return dict(path=str(p),start=start,duration=duration,source_start=0,bytes=s.st_size,mtime_ns=s.st_mtime_ns,eligible_age=True)
    def channel(self,ch,segs):return dict(channel=ch,representation='canonical' if len(segs)==1 else 'chunks',segments=segs,overlaps=[],issues=[])
    def test_chunk_boundary_equals_canonical_and_gap_is_not_compressed(self):
        sr=8000;x=np.linspace(-.5,.5,4*sr);a=self.wav('a.wav',x[:2*sr]);b=self.wav('b.wav',x[2*sr:]);c=self.wav('c.wav',x)
        mapped=self.channel(1,[self.seg(a,0,2),self.seg(b,2,2)]);full=self.channel(1,[self.seg(c,0,4)])
        p,_,meta=stream_read(mapped,1,2);q,_,_=stream_read(full,1,2)
        np.testing.assert_array_equal(p,q);self.assertEqual(len(meta),2)
        mapped['segments'][1]['start']=3
        with self.assertRaisesRegex(ValueError,'gap'):stream_read(mapped,1,3)
    def test_changed_file_is_deferred(self):
        p=self.wav('a.wav',np.zeros(16000));c=self.channel(1,[self.seg(p,0,2)])
        os.utime(p,(1_700_000_001,1_700_000_001))
        with self.assertRaisesRegex(ValueError,'changed'):stream_read(c,0,1)
    def test_midnight_hour24_normalizes_without_time_compression(self):
        self.assertEqual(seconds('207-24-05-00'),seconds('208-00-05-00'))
        self.assertEqual(seconds('207-24-05-00')-seconds('207-23-35-00'),1800)
    def test_timestamp_free_canonical_requires_explicit_common_start(self):
        self.wav('canonical/A15_T1/A15_T1_CH1_TEST.wav',np.zeros(16000))
        m=make_manifest(self.root/'none',self.root/'canonical',self.root/'unknown.json')
        self.assertEqual(m['tapes'][0]['channels'][0]['representation'],'unmapped')
        m=make_manifest(self.root/'none',self.root/'canonical',self.root/'declared.json',True)
        c=m['tapes'][0]['channels'][0];self.assertEqual(c['representation'],'canonical')
        x,sr,_=stream_read(c,0,1);self.assertEqual(len(x),sr)
    def test_canonical_replaces_chunks_and_duplicates_block(self):
        rel='A15_T1/A15-CH1_TEST/A15_T1_CH1_TEST_001-00-00-00_001-00-00-02-0.wav'
        self.wav('delivery/'+rel,np.zeros(16000))
        self.wav('canonical/A15_T1/A15_T1_CH1_TEST_001-00-00-00_001-00-00-02.wav',np.zeros(16000))
        m=make_manifest(self.root/'delivery',self.root/'canonical',self.root/'manifest.json');c=m['tapes'][0]['channels'][0]
        self.assertEqual(c['representation'],'canonical');self.assertEqual(len(c['segments']),1);self.assertEqual(c['superseded_chunk_files'],1)
        self.wav('canonical/A15_T1/duplicate_CH1_TEST.wav',np.zeros(16000))
        m=make_manifest(self.root/'delivery',self.root/'canonical',self.root/'manifest2.json');self.assertEqual(m['tapes'][0]['channels'][0]['representation'],'ambiguous')
    def test_unknown_carriers_shared_warp_and_noise_abstention(self):
        sr=8000;t=np.arange(40*sr)/sr;tau=t-.02/(2*np.pi*6.5)*np.cos(2*np.pi*6.5*t);rng=np.random.default_rng(7)
        arrays=[.2*np.sin(2*np.pi*777*tau),.2*np.sin(2*np.pi*1229*tau)]
        gate=np.zeros(len(t))
        for begin in [22,25,28,31,34]:gate[(t>begin)&(t<begin+.25)]=1
        arrays.append(.2*gate*np.sin(2*np.pi*2475*tau))
        channels=[]
        for i,x in enumerate(arrays,1):
            p=self.wav(f'CH{i}.wav',x+.001*rng.normal(size=len(x)));channels.append(self.channel(i,[self.seg(p,0,40)]))
        r=scan_window(dict(tape='synthetic_unknown_carriers',channels=channels),0,40,2)
        self.assertTrue(any(abs(c['carrier_hz']-777)<25 for c in r['candidates']))
        self.assertTrue(any(abs(c['carrier_hz']-1229)<25 for c in r['candidates']))
        self.assertEqual(r['status'],'locally_supported_research_candidate',r.get('target_summary'))
        self.assertGreater(r['target_summary']['deduplicated_median_reduction_percent'],50)
        p=self.wav('noise.wav',rng.normal(0,.01,len(t)))
        n=scan_window(dict(tape='noise',channels=[self.channel(1,[self.seg(p,0,40)])]),0,40,2)
        self.assertEqual(n['status'],'no_trustworthy_reference')

if __name__=='__main__':unittest.main(verbosity=2)
