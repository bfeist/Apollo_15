"""Screening mathematics and reuse/fallback safeguards."""
import os,tempfile,unittest
from pathlib import Path
import numpy as np
import soundfile as sf
from broad_discovery import quick_track,try_window,reusable

class BroadDiscoveryTests(unittest.TestCase):
    def setUp(self):
        base=Path(__file__).resolve().parent/'test_tmp';base.mkdir(exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=base);self.root=Path(self.temp.name).resolve()
        assert self.root.is_relative_to(base.resolve());self.addCleanup(self.temp.cleanup)
    def channel(self,start=0):
        p=self.root/'source.wav';sf.write(p,np.sin(2*np.pi*777*np.arange(40*8000)/8000)*.2,8000,subtype='PCM_16');os.utime(p,(1700000000,1700000000));s=p.stat()
        seg=dict(path=str(p),start=start,source_start=0,duration=40,eligible_age=True,bytes=s.st_size,mtime_ns=s.st_mtime_ns)
        return dict(channel=1,representation='canonical',segments=[seg],overlaps=[])
    def test_quick_phase_recovers_known_fractional_motion_and_rejects_static(self):
        sr=8000;t=np.arange(30*sr)/sr;truth=.02*np.sin(2*np.pi*6.5*t);tau=t-.02/(2*np.pi*6.5)*np.cos(2*np.pi*6.5*t)
        c=dict(carrier_hz=777.,occupancy=1.,contrast_db=30.)
        stats,e=quick_track(np.sin(2*np.pi*777*tau),sr,c)
        self.assertGreater(np.corrcoef(e[400:-400],truth[::40][400:-400])[0,1],.99)
        self.assertTrue(stats['screening_eligible'])
        stats,_=quick_track(np.sin(2*np.pi*777*t),sr,c)
        self.assertFalse(stats['screening_eligible'])
    def test_fallback_records_actual_time_instead_of_compressing_gap(self):
        c=self.channel(100);x,sr,m=try_window(c,20,30,[])
        self.assertEqual(m['sampling'],'fallback_available_segment');self.assertEqual(m['requested_start'],20)
        self.assertGreaterEqual(m['start'],100);self.assertEqual(len(x),30*sr);self.assertTrue(m['prior_attempts'])
    def test_reuse_rejects_changed_timeline_even_when_file_unchanged(self):
        c=self.channel();s=c['segments'][0]
        prior=[dict(tape='A15_T1',start=0,duration=30,coverage=[dict(channel=1,status='sampled',sources=[dict(path=s['path'],bytes=s['bytes'],mtime_ns=s['mtime_ns'],start_seconds=0,stream_start=0)])],candidates=[],status='no_trustworthy_reference',selected=None)]
        self.assertIsNotNone(reusable(prior,'A15_T1',c,0))
        c['segments'][0]['start']=5
        self.assertIsNone(reusable(prior,'A15_T1',c,0))

if __name__=='__main__':unittest.main(verbosity=2)
