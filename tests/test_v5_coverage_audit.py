import json,sys,tempfile,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'research'))
from v5_coverage_audit import audit

class CoverageAuditTest(unittest.TestCase):
 def fixture(self,folder,records):
  manifest=dict(start_seconds=.25,requested_end_seconds=2.5,inventory=dict(channels=[dict(channel=1,start_seconds=.5,end_seconds=2.25)]),windows=[dict(start_seconds=.25,end_seconds=2.5,channels={'1':dict(discovery_sources={'denoised':dict(status='analyzed')})})],denoised_root='x',complete=True,all_recorded_sources_covered=True)
  (folder/'survey_manifest.json').write_text(json.dumps(manifest))
  with (folder/'survey.jsonl').open('w') as f:
   for ch in range(1,31):
    for lo,hi in ((.25,1),(1,2),(2,2.5)):
     r=dict(channel=ch,start_seconds=lo,end_seconds=hi,status='missing_channel')
     if ch==1:
      r.update(status='analyzed',available_start=max(lo,.5),available_end=min(hi,2.25))
     f.write(json.dumps(r)+'\n')
   for r in records:f.write(json.dumps(r)+'\n')
 def test_fractional_availability(self):
  with tempfile.TemporaryDirectory() as d:
   self.fixture(Path(d),[]);result=audit(d); self.assertTrue(result['partition_valid'])
   ch=next(x for x in result['channels'] if x['channel']=='1')
   self.assertAlmostEqual(ch['analyzed_seconds'],1.75);self.assertAlmostEqual(ch['unavailable_seconds'],.5);self.assertAlmostEqual(ch['denoised_analyzed_seconds'],1.75)
 def test_overlap_detected(self):
  with tempfile.TemporaryDirectory() as d:
   self.fixture(Path(d),[dict(channel=1,start_seconds=1,end_seconds=2,status='analyzed')]); result=audit(d)
   self.assertFalse(result['partition_valid'])
if __name__=='__main__': unittest.main()
