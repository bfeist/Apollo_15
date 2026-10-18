"""Shared playback clocks, shifted historical duplicates, and exhaustive survey gates."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
import soundfile as sf

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
from quindar_tape import pair_matches,consolidate,build_shared_plan,target_map
from quindar_survey import inventory_all,scan_file,implementation,survey
from quindar_retime import retime,write_json
import pipeline


def event(t,index=0,shift=0.,sd=2.):
    return dict(start_seconds=t+shift-.125,end_seconds=t+shift+.125,duration_seconds=.25,
                frequency_hz=(2525 if index%2==0 else 2475)*.987,band_power_fraction=.95,
                amplitude_ratio=1.05,trim_range_hz=.1,residual_fm_sd_hz=sd,
                tape_seconds=t+shift,event_id=f'{shift}:{index}')


def saved_scan(path,row,events):
    data=dict(status='complete',source=row,events=events,implementation_sha256='fixture',
              events_sha256=hashlib.sha256(json.dumps(events,separators=(',',':')).encode()).hexdigest())
    path.parent.mkdir(parents=True,exist_ok=True)
    write_json(path,data)


class TapeQuindarTests(unittest.TestCase):
    def test_local_offsets_switch_and_periodic_ambiguity(self):
        rng=np.random.default_rng(95)
        times=np.cumsum(rng.uniform(17,40,180))
        offsets=np.where(times<1800,1.3,np.where(times<3600,-1.3,13.))
        left=[event(t,i) for i,t in enumerate(times)]
        right=[event(t,i,d) for i,(t,d) in enumerate(zip(times,offsets))]
        links,regions,ambiguous=pair_matches(left,right)
        self.assertGreater(len(links),170)
        self.assertFalse(ambiguous)
        modes={round(m['lag_seconds'],1) for r in regions for m in r['lag_modes']}
        self.assertEqual(modes,{-1.3,1.3,13.})
        # A periodic stream supports several equally plausible alignments.
        left=[event(t,0) | {'event_id':f'l{i}'} for i,t in enumerate(np.arange(100.,500.,2.))]
        right=[event(t,0,1.3) | {'event_id':f'r{i}'} for i,t in enumerate(np.arange(100.,500.,2.))]
        _,_,ambiguous=pair_matches(left,right)
        self.assertGreater(len(ambiguous),300)

    def test_duplicates_keep_observed_playback_times(self):
        times=np.arange(50.,1750.,20.)
        scans=[dict(source=dict(id=f'CH{i}',channel=i,start_seconds=0.),
                    events=[event(t,j,shift,sd) for j,t in enumerate(times)])
               for i,shift,sd in ((1,0.,2.),(2,1.3,1.))]
        result=consolidate(scans,post=True)
        self.assertEqual(result['event_groups'],len(times))
        self.assertEqual(result['duplicate_copies_removed'],len(times))
        for g,t in zip(result['groups'],times):
            chosen=next(e for e in g['observations'] if e['event_id']==g['representative'])
            self.assertAlmostEqual(chosen['tape_seconds'],t+1.3)
            self.assertAlmostEqual(g['tape_seconds'],t+.65)

    def test_exhaustive_gate_shared_plan_and_no_tone_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);tape='A15_T510001';folder=root/'analysis'/tape
            originals=[];post=[];sources=[]
            times=np.arange(50.,1750.,20.)
            for ch in range(1,5):
                original=root/f'original{ch}.wav';original.write_bytes(b'fixture original')
                stat=original.stat()
                row=dict(id=f'orig{ch}',tape=tape,stage='original',channel=ch,path=str(original.resolve()),
                         bytes=stat.st_size,mtime_ns=stat.st_mtime_ns,origin_filename_seconds=1000,
                         absolute_start=1000,start_seconds=0.,duration_seconds=1801.)
                originals.append(row)
                source=root/f'CH{ch:02d}'/'deflutter.wav';source.parent.mkdir()
                duration=1801
                x=np.zeros(duration*8000,dtype=np.float32)
                sf.write(source,x,8000,subtype='FLOAT');sources.append(source)
                review=dict(status='complete',tape=tape,channel=ch,source=row,frames=len(x),sample_rate=8000,
                            start_seconds=0.)
                write_json(source.parent/'review.json',review)
                if ch<=4:
                    stat=source.stat()
                    ref=dict(id=f'CH{ch}',tape=tape,stage='defluttered',channel=ch,path=str(source.resolve()),
                             bytes=stat.st_size,mtime_ns=stat.st_mtime_ns,start_seconds=0.,
                             review_sha256=hashlib.sha256((source.parent/'review.json').read_bytes()).hexdigest())
                    post.append(ref)
                    saved_scan(folder/'survey'/'defluttered'/f'CH{ch}.json',ref,
                               [event(t,i,(ch-1)*1.3,3.-ch) for i,t in enumerate(times)] if ch<3 else [])
            index=dict(schema=2,input_stage='defluttered',complete_defluttered_coverage=True,
                       inventory=post,targets=originals,implementation_sha256='fixture',issues=[])
            write_json(folder.parent/'survey.json',index)
            # A missing defluttered scan prevents accepting partial coverage.
            missing=folder/'survey'/'defluttered'/'CH2.json';backup=missing.read_bytes();missing.unlink()
            with self.assertRaisesRegex(ValueError,'incomplete'):
                build_shared_plan(folder)
            self.assertFalse((folder/'shared_plan'/'plan.json').exists())
            missing.write_bytes(backup)
            plan=build_shared_plan(folder)
            self.assertEqual(plan['reference_count'],4)
            self.assertEqual(plan['unique_calibration_events'],len(times))
            path=folder/'shared_plan'/'plan.json'
            # Independently stored target excerpts use the same full-tape plan.
            for ch in (3,4):
                target=root/f'excerpt{ch}'/'deflutter.wav';target.parent.mkdir()
                x=.2*np.sin(2*np.pi*600*np.arange(12*8000)/8000)
                sf.write(target,x,8000,subtype='FLOAT')
                review=dict(status='complete',tape=tape,channel=ch,source=originals[ch-1],frames=len(x),sample_rate=8000,
                            start_seconds=400.+(ch-3)*1.3)
                write_json(target.parent/'review.json',review)
                sources[ch-1]=target
            model,a,b,s,meta=target_map(path,sources[2])
            np.testing.assert_allclose([r['input_seconds'] for r in model['anchors']],times+1.3)
            self.assertAlmostEqual(meta['output_start_seconds'],400*.987,places=6)
            _,c,d,_,other=target_map(path,sources[3])
            # Identical absolute playback instants map to identical output instants.
            common=405.
            self.assertAlmostEqual(np.interp(common-400,a,b)+meta['output_start_seconds'],
                                   np.interp(common-401.3,c,d)+other['output_start_seconds'],places=9)
            # Render a target with no Quindars: it must never scan or fit its own curve.
            with patch('quindar_retime.scan',side_effect=AssertionError('unexpected per-channel scan')):
                result=retime(sources[2],root/'retimed',shared_plan=path,progress=lambda _:None)
            self.assertEqual(result['audio']['frames'],round(12*.987*8000))
            self.assertEqual(result['shared_plan']['sha256'],hashlib.sha256(path.read_bytes()).hexdigest())
            self.assertFalse((root/'retimed'/'candidates.json').exists())
            # Frozen plan reuse doesn't re-fit, and artifact changes fail closed.
            with patch('quindar_tape.curve_model',side_effect=AssertionError('unexpected re-fit')):
                self.assertEqual(build_shared_plan(folder),plan)
            with (path.parent/'timing_map.npz').open('ab') as f:f.write(b'tamper')
            with self.assertRaisesRegex(ValueError,'artifact changed'):
                target_map(path,sources[3])

    def test_requires_defluttered_channels_and_never_scans_originals(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);folder=root/'original'/'A15_T510001';folder.mkdir(parents=True)
            for name in ('A15_T510001_HR1U_CH01_IRIG_208-10-00-00_208-10-00-02.wav',
                         'A15_T510001_HR1U_CH02_CAPCOM_208-10-00-00_208-10-00-02.wav'):
                sf.write(folder/name,np.zeros(16000),8000)
            rows,issues=inventory_all(folder.parent,root/'defluttered')
            self.assertFalse(rows);self.assertEqual(len(issues),1)
            with patch('quindar_survey.ProcessPoolExecutor') as pool:
                with self.assertRaisesRegex(ValueError,'not started'):
                    survey(folder.parent,root/'defluttered',root/'analysis',workers=1)
                pool.assert_not_called()
            self.assertFalse((root/'analysis').exists())
            original=next(folder.glob('*CH02*'));stat=original.stat()
            source=root/'defluttered'/folder.name/'CH02'/'deflutter.wav';source.parent.mkdir(parents=True)
            x=np.zeros(16000,dtype=np.float32);sf.write(source,x,8000,subtype='FLOAT')
            origin=208*86400+10*3600
            review=dict(status='complete',tape=folder.name,channel=2,start_seconds=0.,frames=16000,sample_rate=8000,
                        source=dict(path=str(original.resolve()),bytes=stat.st_size,mtime_ns=stat.st_mtime_ns,
                                    absolute_start=origin,start_seconds=0.),
                        audio=dict(deflutter=dict(decoded_float32_sha256=hashlib.sha256(x.tobytes()).hexdigest())))
            write_json(source.parent/'review.json',review)
            rows,issues=inventory_all(folder.parent,root/'defluttered')
            self.assertEqual(len(rows),1);self.assertFalse(issues)
            self.assertTrue(all(r['stage']=='defluttered' for r in rows))
            result=survey(folder.parent,root/'defluttered',root/'analysis',workers=1)
            self.assertEqual(result['status'],'complete')
            with patch('quindar_survey.ProcessPoolExecutor') as pool:
                again=survey(folder.parent,root/'defluttered',root/'analysis',workers=1)
                self.assertEqual(len(again['results']),1)
                pool.return_value.__enter__.return_value.submit.assert_not_called()
            path=Path(rows[0]['path']);sf.write(path,np.ones(16000)*.1,8000)
            changed=survey(folder.parent,root/'defluttered',root/'analysis',workers=1)
            self.assertEqual(changed['status'],'partial')
            self.assertTrue(changed['issues'])
            with self.assertRaisesRegex(ValueError,'only scans'):
                scan_file(dict(stage='original'),root/'bad.json','fixture')

    def test_batch_uses_one_plan_per_tape(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            for tape in ('A15_T510001','A15_T510002'):
                p=root/tape/'CH01';p.mkdir(parents=True);(p/'deflutter.wav').touch()
            args,selected=pipeline.parse_args(['plan','--all-tapes','--stages','05c','--defluttered-root',str(root)])
            jobs=pipeline.make_jobs(args,selected,root/'runs')
            self.assertEqual(len(jobs),2)
            self.assertTrue(all('--all-channels' in j['command'] for j in jobs))
            self.assertTrue(all('--experimental-single-channel' not in j['command'] for j in jobs))


if __name__=='__main__':unittest.main()
