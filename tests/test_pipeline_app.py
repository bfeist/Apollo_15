"""Local pipeline inventory, acceptance boundaries, durable queue and HTTP."""
from copy import deepcopy
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import sys
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import numpy as np
import soundfile as sf

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from deflutter_review import Desk
from pipeline_app import Handler, PipelineHTTPServer, claim_session
from pipeline_control import ControlRoom, now, read, save
from carrier_survey import survey_tape


class PipelineAppTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = {k:str(self.root/k) for k in ('originals','delivery','surveys','plans','denoised',
                       'defluttered','quindar','retimed','state','pipeline_state')}
        self.tape = 'A15_T510083'
        folder = self.root/'originals'/self.tape
        folder.mkdir(parents=True)
        for channel, label in ((1,'IRIG'),(2,'VOICE')):
            path = folder/f'{self.tape}_HR1U_CH{channel}_{label}_207-00-00-00_207-00-00-20.wav'
            sf.write(path,np.zeros(160000),8000,subtype='FLOAT')
        self.desk = Desk(self.config)
        self.addCleanup(self.desk.pool.shutdown,wait=True,cancel_futures=True)
        self.room = ControlRoom(self.desk,self.config)
        self.addCleanup(self.room.close)

    def wait_job(self, status, index=0):
        deadline = time.monotonic()+10
        while time.monotonic()<deadline:
            if len(self.room.jobs)>index and self.room.jobs[index]['status'] in status:
                return self.room.jobs[index]
            time.sleep(.02)
        self.fail('Queue did not reach '+str(status))

    def command(self, code="print('bounded worker finished')"):
        return [sys.executable,'-u','-c',code]

    def test_inventory_does_not_promote_files_notes_or_judgments_to_acceptance(self):
        output=self.root/'defluttered'/self.tape/'CH02'
        output.mkdir(parents=True)
        sf.write(output/'deflutter.wav',np.zeros(160000),8000)
        self.room.note(dict(tape=self.tape,text='Sounds good; this is a journal entry.'))
        self.room.scan(lambda _:None)
        row=self.room.overview()['tapes'][0]
        self.assertEqual(row['expected_channels'],1)
        self.assertEqual(row['stages']['assembled']['status'],'complete')
        self.assertEqual(row['stages']['review']['done'],0)
        self.assertEqual(row['stages']['defluttered']['done'],0)
        self.assertEqual(row['notes'][0]['text'],'Sounds good; this is a journal entry.')
        self.assertEqual(len(row['channels']),2)
        self.assertEqual(row['channels'][0]['role'],'timing_only')

    def test_commands_are_allowlisted_and_readiness_checked(self):
        for tape in ('../escape','A11_T648',self.tape+'/../other'):
            with self.assertRaises(ValueError):self.room.command(tape,'survey',{})
        with self.assertRaises(ValueError):self.room.command(self.tape,'arbitrary-shell',{})
        for action in ('pilot','plan','deflutter','quindar','speed_plan','retime'):
            with self.subTest(action=action),self.assertRaises(ValueError):
                self.room.command(self.tape,action,{})
        command=self.room.command(self.tape,'survey',{})
        self.assertIn('carrier_survey.py',command[2])
        self.assertNotIn('--max-windows',command)
        self.assertIn('--max-windows',self.room.command(self.tape,'carrier_benchmark',{}))
        with self.assertRaises(ValueError):self.room.command(self.tape,'survey',dict(channel=2))

    def test_pilot_bounds_and_rollout_gate_are_distinct(self):
        row=self.room.inspect_tape(self.tape)
        row['stages']['survey']['status']='complete'
        with patch.object(self.room,'inspect_tape',return_value=row):
            command=self.room.command(self.tape,'pilot',dict(start=0,duration=15))
            self.assertIn('--duration',command)
            for opts in (dict(start=10,duration=15),dict(start=0,duration=float('nan')),dict(start=-1,duration=15)):
                with self.assertRaises(ValueError):self.room.command(self.tape,'pilot',opts)
            with self.assertRaises(ValueError):self.room.command(self.tape,'plan',{})

    def test_production_requires_exact_acceptance_and_preserves_existing_outputs(self):
        row=self.room.inspect_tape(self.tape)
        row['stages']['plan']['status']='complete'
        row['channels'][1]['denoised']=True
        with patch.object(self.room,'inspect_tape',return_value=row):
            with self.assertRaises(ValueError):self.room.command(self.tape,'deflutter',dict(channel=2))
            with patch('pipeline_control.require_production') as gate:
                command=self.room.command(self.tape,'deflutter',dict(channel=2))
                gate.assert_called_once()
                self.assertIn('--batch-channels',command)
                output=self.root/'defluttered'/self.tape/'CH02'
                output.mkdir(parents=True);(output/'partial.txt').write_text('unfinished')
                with self.assertRaisesRegex(ValueError,'existing output'):
                    self.room.command(self.tape,'deflutter',dict(channel=2))

    def test_quindar_indexes_are_isolated_per_tape(self):
        row=self.room.inspect_tape(self.tape)
        row['stages']['defluttered']['status']='complete'
        with patch.object(self.room,'inspect_tape',return_value=row):
            command=self.room.command(self.tape,'quindar',{})
        destination=Path(command[command.index('--output')+1])
        self.assertEqual(destination,self.root/'quindar'/'per_tape'/self.tape)
        self.assertNotEqual(destination,self.room.analysis_root('A15_T510077'))

    def test_real_partial_survey_is_not_reported_complete_and_tampering_is_stale(self):
        output=self.root/'surveys'/self.tape
        survey_tape(self.root/'originals'/self.tape,output,max_windows=2)
        row=self.room.inspect_tape(self.tape)
        self.assertEqual(row['stages']['survey']['status'],'partial')
        self.assertEqual(row['stages']['survey']['done'],0)
        self.assertEqual(row['stages']['survey']['total'],2)
        self.assertIn('projected_remaining_seconds',row['survey_performance'])
        record=read(output/'survey.json')['files'][0]
        (output/record['cache']).write_text('{}')
        self.assertEqual(self.room.inspect_tape(self.tape)['stages']['survey']['status'],'stale')

    def test_demo_and_overlapping_roots_cannot_launch_processing(self):
        self.desk.demo=True
        with self.assertRaisesRegex(ValueError,'Demo'):self.room.command(self.tape,'survey',{})
        config=deepcopy(self.config);config['retimed']=str(self.root/'originals'/'output')
        with self.assertRaisesRegex(ValueError,'separate'):ControlRoom(self.desk,config)

    def test_inventory_uses_checkpoints_while_evidence_use_checks_features(self):
        output=self.root/'surveys'/self.tape
        survey_tape(self.root/'originals'/self.tape,output)
        self.assertEqual(self.room.inspect_tape(self.tape)['stages']['survey']['status'],'complete')
        record=read(output/'survey.json')['files'][0]
        cache=read(output/record['cache'])
        feature=output/cache['feature_blocks'][0]['file']
        feature.write_bytes(feature.read_bytes()+b' ')
        self.assertEqual(self.room.inspect_tape(self.tape)['stages']['survey']['status'],'complete')
        with self.assertRaisesRegex(ValueError,'feature cache changed'):
            self.desk.safe_survey(output)
        Path(record['path']).touch()
        self.assertEqual(self.room.inspect_tape(self.tape)['stages']['survey']['status'],'stale')

    def test_refresh_during_scan_runs_once_more_after_latest_completion(self):
        started=threading.Event()
        release=threading.Event()
        finished=threading.Event()
        calls=[]
        def scan(progress):
            calls.append(len(calls))
            if len(calls)==1:
                started.set()
                self.assertTrue(release.wait(5))
            else:
                finished.set()
        first=self.desk.submit('Initial inventory',scan)
        self.assertTrue(started.wait(5))
        try:
            for _ in range(3):
                self.assertIs(self.desk.submit('Refresh after job',scan,coalesce=True),first)
        finally:
            release.set()
        self.assertTrue(finished.wait(5))
        self.assertEqual(len(calls),2)
        self.assertEqual(len(self.desk.jobs),2)

    def test_preview_has_no_side_effect_and_queue_deduplicates(self):
        with patch.object(self.room,'command',return_value=self.command()):
            p=self.room.preview(dict(tapes=[self.tape],action='survey'))
            self.assertEqual(self.room.jobs,[])
            self.room.enqueue(p['id'])
            p=self.room.preview(dict(tapes=[self.tape],action='survey'))
            self.assertEqual(self.room.enqueue(p['id'])['added'],[])
        self.assertTrue(self.room.paused)
        self.assertEqual(self.room.jobs[0]['status'],'queued')
        self.assertFalse((self.root/'pipeline_state'/'jobs'/f'{self.room.jobs[0]["id"]}.log').exists())

    def test_real_subprocess_log_and_persistent_completed_history(self):
        with patch.object(self.room,'command',return_value=self.command()):
            p=self.room.preview(dict(tapes=[self.tape],action='survey'))
            self.room.enqueue(p['id']);self.room.queue_control('start')
            job=self.wait_job({'complete'})
        self.assertIn('bounded worker finished',self.room.log(job['id'])['tail'])
        self.room.close()
        recovered=ControlRoom(self.desk,self.config)
        self.addCleanup(recovered.close)
        self.assertTrue(recovered.paused)
        self.assertEqual(recovered.jobs[0]['status'],'complete')
        self.assertEqual(recovered.jobs[0]['returncode'],0)

    def test_changed_prerequisites_block_execution_and_pause_remaining_queue(self):
        with patch.object(self.room,'command',return_value=self.command()):
            p=self.room.preview(dict(tapes=[self.tape],action='survey'))
            self.room.enqueue(p['id'])
        with patch.object(self.room,'command',side_effect=ValueError('Approval changed')):
            self.room.queue_control('start')
            job=self.wait_job({'blocked'})
        self.assertIn('Approval changed',job['error'])
        self.assertTrue(self.room.paused)
        self.assertFalse((self.root/'pipeline_state'/'jobs'/f'{job["id"]}.log').exists())

    def test_nonzero_exit_pauses_queue_and_retains_error_log(self):
        with patch.object(self.room,'command',return_value=self.command("print('failure detail');raise SystemExit(3)")):
            p=self.room.preview(dict(tapes=[self.tape],action='survey'))
            self.room.enqueue(p['id']);self.room.queue_control('start')
            job=self.wait_job({'failed'})
        self.assertTrue(self.room.paused)
        self.assertEqual(job['returncode'],3)
        self.assertIn('failure detail',self.room.log(job['id'])['tail'])

    def test_stopping_owned_process_cancels_job(self):
        with patch.object(self.room,'command',return_value=self.command("import time;print('worker running',flush=True);time.sleep(30)")):
            p=self.room.preview(dict(tapes=[self.tape],action='survey'))
            self.room.enqueue(p['id']);self.room.queue_control('start')
            deadline=time.monotonic()+10
            while not self.room.process and time.monotonic()<deadline:time.sleep(.02)
            self.assertIsNotNone(self.room.process)
            self.room.cancel(self.room.jobs[0]['id'])
            self.wait_job({'cancelled'})
        self.assertTrue(self.room.paused)

    def test_interrupted_recovery_never_restarts_processing(self):
        self.room.close()
        save(self.root/'pipeline_state/jobs'/'old.json',dict(id='old',created_at=now(),status='running',tape=self.tape))
        recovered=ControlRoom(self.desk,self.config)
        self.addCleanup(recovered.close)
        self.assertEqual(recovered.jobs[0]['status'],'interrupted')
        self.assertTrue(recovered.paused)
        self.assertEqual(read(self.root/'pipeline_state/jobs/old.json')['status'],'interrupted')

    def test_exclusive_server_session(self):
        handle=claim_session(self.root/'session')
        try:
            with self.assertRaises(ValueError):claim_session(self.root/'session')
        finally:handle.close()
        claim_session(self.root/'session').close()

    def launch(self, port):
        config_path=self.root/'launch_config.json'
        save(config_path,self.config)
        return subprocess.run([sys.executable,str(Path(__file__).resolve().parents[1]/'scripts/pipeline_app.py'),
                               '--config',str(config_path),'--port',str(port)],
                              capture_output=True,text=True,timeout=15)

    def test_second_launch_on_same_port_is_clean_and_does_not_steal_requests(self):
        server=PipelineHTTPServer(('127.0.0.1',0),Handler)
        server.desk,server.room=self.desk,self.room
        worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
        self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
        result=self.launch(server.server_port)
        self.assertEqual(result.returncode,2)
        self.assertNotIn('Traceback',result.stderr)
        self.assertIn(f'http://127.0.0.1:{server.server_port}/',result.stderr)
        with urlopen(f'http://127.0.0.1:{server.server_port}/api/pipeline/overview') as response:
            self.assertEqual(json.loads(response.read())['token'],self.desk.token)

    def test_second_launch_on_other_port_reports_workspace_lock_without_traceback(self):
        handle=claim_session(self.config['pipeline_state'])
        try:
            result=self.launch(0)
        finally:handle.close()
        self.assertEqual(result.returncode,2)
        self.assertNotIn('Traceback',result.stderr)
        self.assertIn('already open in another server',result.stderr)
        self.assertNotIn('PermissionError',result.stderr)

    def test_http_review_mount_assets_and_write_boundaries(self):
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        server.desk,self.server_room=self.desk,self.room
        server.room=self.room
        worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
        self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
        base=f'http://127.0.0.1:{server.server_port}'
        for path,needle in (('/','Pipeline control room'),('/review','pipeline-back'),('/pipeline.js','renderOverview'),('/api/overview','token')):
            with urlopen(base+path) as response:self.assertIn(needle,response.read().decode())
        body=json.dumps(dict(tape=self.tape,text='HTTP test note')).encode()
        for headers in ({'Content-Type':'application/json'},
                        {'Content-Type':'application/json','X-Review-Token':self.desk.token,'Origin':'https://example.com'}):
            with self.assertRaises(HTTPError):urlopen(Request(base+'/api/pipeline/note',data=body,headers=headers))
        headers={'Content-Type':'application/json','X-Review-Token':self.desk.token}
        with urlopen(Request(base+'/api/pipeline/note',data=body,headers=headers)) as response:
            self.assertEqual(json.loads(response.read())['text'],'HTTP test note')
        with self.assertRaises(HTTPError):urlopen(Request(base+'/api/pipeline/overview',headers={'Host':'evil.example'}))
        with self.assertRaises(HTTPError):urlopen(base+'/api/pipeline/log?id=../../secrets')


if __name__=='__main__':unittest.main()
