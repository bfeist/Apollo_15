"""Apollo 15's local work ledger and adapters for the existing pipeline.

The dashboard never substitutes a manual checkbox for scientific acceptance.
Commands are constructed here, not accepted from the browser.
"""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import threading
import uuid

import soundfile as sf

from carrier_inventory import load_inventory
from channel_review import completed_denoised
from flutter_planning import inventory
from flutter_policy import digest, implementation_digest, require_production, require_rollout
from quindar_survey import inventory_all, implementation as survey_implementation
from quindar_tape import load_shared_plan
from review_sampling import adverse, fingerprint

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
ACTIVE = {'queued', 'running', 'stopping'}
STAGES = [
    ('assembled', '01', 'Assembled'), ('denoised', '03', 'Denoised'),
    ('survey', '04', 'Carriers'), ('pilot', '05a', 'Pilots'),
    ('plan', '05a', 'Full plan'), ('review', 'QA', 'Accepted'),
    ('defluttered', '05b', 'Defluttered'), ('quindar', 'Q', 'Quindars'),
    ('speed_plan', 'Q', 'Speed curve'), ('retimed', '05c', 'Retimed'),
]
ACTIONS = {
    'denoise': ('03', 'Denoise channels', 'Resume tone, hum and broadband cleanup. Matching completed channels are skipped.'),
    'carrier_benchmark': ('04', 'Benchmark carrier survey', 'Screen up to 30 windows per source before committing to a complete tape survey.'),
    'survey': ('04', 'Survey original carriers', 'Screen the complete original tape. Existing matching survey blocks are reused.'),
    'pilot': ('05a', 'Plan a bounded pilot', 'Measure a selected original-audio interval for listening and method validation.'),
    'plan': ('05a', 'Plan the full tape', 'Requires accepted methodology. Analyze originals and save regional correction maps.'),
    'deflutter': ('05b', 'Render accepted corrections', 'Render only accepted speech channels with prepared denoised audio. Existing outputs are preserved.'),
    'quindar': ('Q', 'Survey post-deflutter Quindars', 'Requires all speech channels to finish defluttering. Survey the completed corrected audio.'),
    'speed_plan': ('Q', 'Freeze the shared speed curve', 'Build one common slow-speed curve from the completed post-deflutter survey.'),
    'retime': ('05c', 'Render retimed channels', 'Apply the frozen shared curve. Existing compatible output is verified by the CLI.'),
}


def now():
    return datetime.now(timezone.utc).isoformat()


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.partial')
    temporary.write_text(json.dumps(value, indent=2, allow_nan=False), encoding='utf-8')
    temporary.replace(path)


def stage(done=0, total=1, reason='', pending='pending'):
    return dict(status='complete' if total and done == total else 'partial' if done else pending,
                done=done, total=total, reason=reason)


class ControlRoom:
    def __init__(self, desk, config):
        self.desk = desk
        self.config = config
        self.state = Path(config['pipeline_state']).resolve()
        roots = [Path(config[k]).resolve() for k in
                 ('originals', 'delivery', 'surveys', 'plans', 'denoised', 'defluttered', 'quindar', 'retimed', 'state')]
        for i, root in enumerate(roots):
            for other in roots[i+1:]:
                if root == other or root in other.parents or other in root.parents:
                    raise ValueError('Data, output and review roots must be separate: '+str(root)+' / '+str(other))
            if self.state == root or root in self.state.parents or self.state in root.parents:
                raise ValueError('Pipeline state must be separate from every data/review root')
        self.state.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.wake = threading.Event()
        self.closed = False
        self.paused = True  # Restarting the app must never launch a saved queue.
        self.process = None
        self.previews = {}
        self.jobs = []
        self.recovery_errors = []
        for path in sorted((self.state/'jobs').glob('*.json')):
            try:
                job = read(path)
                if job['status'] in {'running', 'stopping'}:
                    job.update(status='interrupted', finished_at=now(),
                               error='Server stopped during this job. Check its log and any external process before retrying.')
                    save(path, job)
                self.jobs.append(job)
            except (ValueError, OSError, KeyError) as exc:
                self.recovery_errors.append(f'Unreadable job record {path.name}: {exc}')
        self.jobs.sort(key=lambda j: j['created_at'])
        self.snapshot = dict(tapes=[], scanned_at=None)
        try:
            self.snapshot = read(self.state/'inventory.json')
        except (ValueError, OSError):
            pass
        self.thread = threading.Thread(target=self._worker, daemon=True, name='apollo-pipeline')
        self.thread.start()

    def root(self, key):
        return Path(self.config[key]).resolve()

    def tape_path(self, tape):
        if not isinstance(tape, str) or not re.fullmatch(r'A15_T\d+', tape):
            raise ValueError('Select an Apollo 15 tape from the inventory')
        path = (self.root('originals')/tape).resolve()
        if path.parent != self.root('originals') or not path.is_dir():
            raise ValueError('Unknown canonical Apollo 15 tape')
        return path

    def rollout(self):
        return Path(self.config.get('rollout_approval') or self.root('state')/'methodology/rollout_approval.json')

    def approval(self, tape):
        return Path(self.config.get('production_approval_root') or self.root('state')/'approvals')/tape/'production_acceptance.json'

    def analysis_root(self, tape):
        # The survey CLI owns one root-wide survey.json. Isolate new per-tape
        # jobs so surveying a second tape cannot replace the first tape's index.
        # Existing CLI analyses remain readable in the original flat layout.
        isolated = self.root('quindar')/'per_tape'/tape
        if isolated.exists():
            return isolated
        if (self.root('quindar')/tape/'shared_plan/plan.json').exists():
            return self.root('quindar')
        return isolated

    def survey_status(self, tape, folder):
        path = self.root('surveys')/tape/'survey.json'
        index = read(path)
        self.desk.guard.check(index['source'])
        if Path(index['source']).resolve() != folder:
            raise ValueError('Carrier survey belongs to another original tape')
        for record in index['files']:
            self.desk.guard.check(record['path'])
            self.desk.guard.check(path.parent/record['cache'])
        index, _ = load_inventory(path,require_complete=False)
        actual = {p.resolve() for p in folder.rglob('*') if p.suffix.lower()=='.wav'}
        declared = [Path(r['path']).resolve() for r in index['files']]
        if len(set(declared)) != len(declared) or not set(declared) <= actual or (index['complete'] and set(declared) != actual):
            raise ValueError('Carrier survey does not match the complete original-file inventory')
        result = stage(sum(r['complete'] for r in index['files']),len(actual),
                       'Complete source-bound survey checkpoints; feature hashes are checked before planning and acceptance' if index['complete'] else 'Partial survey checkpoint; resume without a benchmark window limit')
        if not index['complete']:
            result['status']='partial'
        return result, index.get('performance',{})

    def _render_status(self, tape, row, plan_hash):
        folder = self.root('defluttered')/tape/f'CH{row["channel"]:02d}'
        path = folder/'review.json'
        if not path.exists():
            return 'missing', ''
        try:
            record = read(path)
            if record['status'] != 'complete':
                return record['status'], record.get('error', 'Incomplete render')
            source = record['source']
            if (record['tape'] != tape or record['channel'] != row['channel'] or
                    Path(source['path']).resolve() != Path(row['path']).resolve() or
                    (source['bytes'], source['mtime_ns']) != (row['bytes'], row['mtime_ns']) or
                    record['frames'] != row['frames'] or record['start_seconds'] != row['start_seconds'] or
                    record.get('experimental_transfer') or record.get('plan_sha256') != plan_hash):
                raise ValueError('Render source, scope or plan differs from the current production plan')
            expected = hashlib.sha256(b''.join((HERE/n).read_bytes() for n in
                ('channel_review.py', 'flutter.py', 'audio_processing.py', 'normalization.py'))).hexdigest()
            if record.get('implementation_sha256') != expected:
                raise ValueError('Render implementation changed')
            info = sf.info(folder/'deflutter.wav')
            if info.frames != row['frames'] or info.samplerate != row['sample_rate'] or info.channels != 1:
                raise ValueError('Rendered audio header differs from its source')
            for name, sha in record['timing_artifacts'].items():
                artifact = (folder/name).resolve()
                if artifact.parent != folder.resolve() or digest(artifact) != sha:
                    raise ValueError('Rendered timing map changed')
            return 'complete', ''
        except (ValueError, OSError, KeyError, TypeError, RuntimeError) as exc:
            return 'stale', str(exc)

    def _retime_status(self, tape, row, speed_hash):
        folder = self.root('retimed')/tape/f'CH{row["channel"]:02d}'
        if not (folder/'retime.json').exists():
            return 'missing', ''
        try:
            record = read(folder/'retime.json')
            if record.get('status') != 'complete':
                return record.get('status', 'failed'), record.get('error', 'Incomplete retiming')
            source = self.root('defluttered')/tape/f'CH{row["channel"]:02d}'/'deflutter.wav'
            stat = source.stat()
            declared = record['source']
            if (Path(declared['path']).resolve() != source.resolve() or
                    (declared['bytes'], declared['mtime_ns']) != (stat.st_size, stat.st_mtime_ns) or
                    record['shared_plan']['sha256'] != speed_hash):
                raise ValueError('Retimed source or shared speed plan changed')
            expected = hashlib.sha256((HERE/'quindar_retime.py').read_bytes()+(HERE/'quindar_speed.py').read_bytes()).hexdigest()
            if record.get('implementation_sha256') != expected:
                raise ValueError('Retiming implementation changed')
            info = sf.info(folder/'retimed.wav')
            if info.frames != record['audio']['frames'] or info.samplerate != row['sample_rate'] or info.channels != 1:
                raise ValueError('Retimed audio header differs from its manifest')
            for name, sha in record['artifacts'].items():
                artifact = (folder/name).resolve()
                if artifact.parent != folder.resolve() or digest(artifact) != sha:
                    raise ValueError('Retiming artifact changed')
            return 'complete', ''
        except (ValueError, OSError, KeyError, TypeError, RuntimeError) as exc:
            return 'stale', str(exc)

    def inspect_tape(self, tape):
        folder = self.tape_path(tape)
        result = dict(tape=tape, stages={}, channels=[], errors=[], excluded_files=[], missing_channels=[])
        s = result['stages']
        try:
            inv = inventory(folder)
        except (ValueError, OSError, RuntimeError) as exc:
            result['errors'].append(str(exc))
            for key, _, _ in STAGES:
                s[key] = stage(reason=str(exc), pending='blocked')
            return result
        result.update(duration_seconds=inv['duration_seconds'], missing_channels=inv['missing_channels'],
                      excluded_files=inv.get('excluded_files', []), source_folder=str(folder),
                      heads=sorted({r['head'] for r in inv['channels'] if r.get('head')}))
        targets = [r for r in inv['channels'] if r['role'] != 'timing_only']
        total = len(targets)
        result['expected_channels'] = total
        s['assembled'] = stage(len(inv['channels']), len(inv['channels']), 'Canonical WAV headers and filename timelines inventoried; delivery completeness is not inferred.')
        survey_ok = False
        try:
            s['survey'], result['survey_performance'] = self.survey_status(tape,folder)
            survey_ok = s['survey']['status']=='complete'
        except (ValueError, OSError, KeyError, TypeError) as exc:
            s['survey'] = stage(reason='Complete compatible original-audio carrier survey required: '+str(exc),
                                pending='stale' if (self.root('surveys')/tape/'survey.json').exists() else 'pending')
        plan_path = self.root('plans')/tape/'plan.json'
        plan_hash = None
        plan = None
        plan_ok = False
        try:
            plan = self.desk.safe_plan(plan_path)
            from flutter_policy import METHODOLOGY, policy_digest
            plan_ok = bool(plan['inventory']['tape'] == tape and plan.get('complete') and plan.get('full_tape') and
                           plan.get('methodology') == METHODOLOGY and plan.get('implementation_sha256') == implementation_digest() and
                           plan.get('policy_sha256') == policy_digest() and (self.desk.demo or not plan.get('demo')))
            plan_hash = digest(plan_path)
            s['plan'] = stage(int(plan_ok), reason='Complete current full-tape plan' if plan_ok else 'Plan is incomplete, stale or bounded', pending='stale')
            result['plan_path'] = str(plan_path)
        except (ValueError, OSError, KeyError, TypeError) as exc:
            s['plan'] = stage(reason='Full-tape plan required: '+str(exc))
        pilots = []
        for p in sorted((self.root('plans')/tape/'audits').glob('*/plan.json')):
            try:
                pilot = self.desk.safe_plan(p)
                from flutter_policy import METHODOLOGY, policy_digest
                if (pilot['inventory']['tape'] == tape and pilot.get('complete') and
                        pilot.get('methodology') == METHODOLOGY and pilot.get('policy_sha256') == policy_digest() and
                        pilot.get('implementation_sha256') == implementation_digest() and (self.desk.demo or not pilot.get('demo'))):
                    pilots.append(dict(path=str(p), start_seconds=pilot.get('start_seconds'), duration_seconds=pilot.get('duration_seconds')))
            except (ValueError, OSError, KeyError, TypeError):
                continue
        result['pilots'] = pilots
        s['pilot'] = stage(int(bool(pilots)), reason=f'{len(pilots)} compatible bounded pilot(s); listening is separate')
        analysis = self.analysis_root(tape)
        speed_path = analysis/tape/'shared_plan/plan.json'
        speed_hash = None
        try:
            shared = load_shared_plan(speed_path)
            if shared['tape'] != tape:
                raise ValueError('Shared speed plan belongs to another tape')
            speed_hash = digest(speed_path)
            s['speed_plan'] = stage(1, reason='Frozen shared curve and reference identities validated')
        except (ValueError, OSError, KeyError, TypeError) as exc:
            s['speed_plan'] = stage(reason='Frozen shared tape speed curve required: '+str(exc))
        for row in inv['channels']:
            ch = row['channel']
            den_ok = completed_denoised(row, self.root('denoised')/tape/f'CH{ch:02d}', 0, row['frames'])
            accepted = False
            approval_error = 'Full current plan and explicit production acceptance required'
            if row['role'] != 'timing_only' and plan_ok:
                try:
                    require_production(self.approval(tape), plan_path, ch)
                    accepted = True
                    approval_error = ''
                except (ValueError, OSError, KeyError, TypeError) as exc:
                    approval_error = str(exc)
            render, render_error = self._render_status(tape, row, plan_hash) if row['role'] != 'timing_only' else ('excluded', '')
            retimed, retime_error = self._retime_status(tape, row, speed_hash) if row['role'] != 'timing_only' else ('excluded', '')
            if retimed == 'complete' and render != 'complete':
                retimed, retime_error = 'stale', 'Upstream deflutter render is missing or stale'
            result['channels'].append(dict(channel=ch, head=row.get('head'), role=row['role'],
                source=Path(row['path']).name, duration_seconds=row['duration_seconds'],
                denoised=den_ok, accepted=accepted, approval_error=approval_error,
                defluttered=render, render_error=render_error, retimed=retimed, retime_error=retime_error))
        speech = [r for r in result['channels'] if r['role'] != 'timing_only']
        s['denoised'] = stage(sum(r['denoised'] for r in speech), total, 'Compatible prepared speech intermediates; timing-only channels are listed separately.')
        s['review'] = stage(sum(r['accepted'] for r in speech), total, 'Validated production acceptance for the current plan; judgments and notes alone do not authorize rendering.')
        s['defluttered'] = stage(sum(r['defluttered'] == 'complete' for r in speech), total, 'Completed full-channel renders with current source, plan and map provenance.')
        s['retimed'] = stage(sum(r['retimed'] == 'complete' for r in speech), total, 'Completed outputs with current input and shared speed plan.')
        try:
            q = read(analysis/'survey.json')
            if (q.get('schema') != 2 or q.get('status') != 'complete' or q.get('input_stage') != 'defluttered' or
                    not q.get('complete_defluttered_coverage') or q.get('implementation_sha256') != survey_implementation()):
                raise ValueError('Post-deflutter survey is incomplete or stale')
            actual, issues = inventory_all(self.root('originals'), self.root('defluttered'), [tape])
            if issues or sorted(actual, key=lambda r:r['id']) != sorted(q['inventory'], key=lambda r:r['id']):
                raise ValueError('Post-deflutter source inventory changed or is incomplete')
            results = q['results']
            if len(results) != total or {r['channel'] for r in results} != {r['channel'] for r in speech}:
                raise ValueError('Quindar survey does not cover every speech channel')
            for r in results:
                p = Path(r['path']).resolve()
                if self.root('quindar') not in p.parents or digest(p) != r['sha256']:
                    raise ValueError('Quindar survey artifact changed')
            s['quindar'] = stage(1, reason='Completed exhaustive post-deflutter survey')
        except (ValueError, OSError, KeyError, TypeError) as exc:
            s['quindar'] = stage(reason='Post-deflutter Quindar survey required: '+str(exc))
        if result['excluded_files']:
            result['errors'].append('Unidentified recording is retained outside numbered-channel processing; exhaustive Quindar analysis requires identification.')
        if result['missing_channels']:
            result['errors'].append('Absent numbered channels: '+', '.join(map(str, result['missing_channels'])))
        result['next_action'] = ('survey' if not survey_ok else 'pilot' if not pilots else
                                 'review' if not plan_ok else 'review' if s['review']['status'] != 'complete' else
                                 'deflutter' if s['defluttered']['status'] != 'complete' else
                                 'quindar' if s['quindar']['status'] != 'complete' else
                                 'speed_plan' if not speed_hash else 'retime' if s['retimed']['status'] != 'complete' else 'complete')
        return result

    def scan(self, progress):
        self.desk.scan(progress)
        tapes = []
        folders = sorted(p for p in self.root('originals').glob('A15_T*') if p.is_dir() and re.fullmatch(r'A15_T\d+', p.name))
        for i, folder in enumerate(folders):
            progress(f'Pipeline inventory {i+1}/{len(folders)}: {folder.name}')
            tapes.append(self.inspect_tape(folder.name))
        progress('Checking current methodology acceptance')
        try:
            require_rollout(self.rollout())
            methodology = dict(status='accepted', path=str(self.rollout()))
        except (ValueError, OSError, KeyError, TypeError) as exc:
            methodology = dict(status='pending', reason=str(exc), path=str(self.rollout()))
        snapshot = dict(tapes=tapes, scanned_at=now(), methodology=methodology,
                        errors=[] if folders else ['No canonical Apollo 15 tapes found'])
        with self.lock:
            save(self.state/'inventory.json', snapshot)
            self.snapshot = snapshot
        return dict(tapes=len(tapes), scanned_at=snapshot['scanned_at'])

    def review_summary(self):
        summaries = {}
        errors = []
        for path in (self.root('state')/'queues').glob('*/queue.json'):
            try:
                q = read(path)
                judged = {e['sample_id'] for e in self.desk.events(q['id'])}
                for tape in q['tapes']:
                    local = [s for s in q['samples'] if any(p['tape'] == tape for p in s['provenance'])]
                    entry = summaries.setdefault(tape, dict(comparisons=0, judged=0, adverse=0, queues=[]))
                    entry['comparisons'] += len(local)
                    entry['judged'] += sum(s['id'] in judged for s in local)
                    entry['queues'].append(q['id'])
            except (ValueError, OSError, KeyError, TypeError) as exc:
                errors.append(f'Listening queue {path.parent.name}: {exc}')
        try:
            events = self.desk.events()
        except (ValueError, OSError, KeyError, TypeError) as exc:
            events = []
            errors.append('Listening ledger could not be read: '+str(exc))
        for e in events:
            if adverse(e):
                for tape in {p['tape'] for p in e['coordinates']}:
                    if tape in summaries:
                        summaries[tape]['adverse'] += 1
        return summaries, errors

    def overview(self):
        with self.lock:
            result = deepcopy(self.snapshot)
            result.update(jobs=deepcopy(self.jobs), paused=self.paused, token=self.desk.token,
                          stages=[dict(key=k, number=n, label=label) for k,n,label in STAGES],
                          actions=[dict(id=k, number=v[0], label=v[1], description=v[2]) for k,v in ACTIONS.items()],
                          roots={k:str(self.root(k)) for k in ('delivery','originals','denoised','surveys','plans','defluttered','quindar','retimed','state','pipeline_state')},
                          demo=self.desk.demo, recovery_errors=self.recovery_errors)
        # Polling serves the last explicit inventory. Acceptance can bind very
        # large audio files; never rehash them on a three-second browser poll.
        result.setdefault('methodology',dict(status='pending',path=str(self.rollout()),reason='Refresh the archive to validate methodology acceptance'))
        reviews, errors = self.review_summary()
        result['review_errors'] = errors
        for t in result['tapes']:
            t['listening'] = reviews.get(t['tape'], dict(comparisons=0, judged=0, adverse=0, queues=[]))
            notes = sorted((self.state/'notes'/t['tape']).glob('*.json'), reverse=True)
            t['notes'] = [read(p) for p in notes[:50]]
        result['scan_jobs'] = list(self.desk.jobs.values())[-8:]
        return result

    def command(self, tape, action, options):
        """Recompute prerequisites from disk for both preview and execution."""
        self.tape_path(tape)
        if action not in ACTIONS:
            raise ValueError('Unknown pipeline action')
        if self.desk.demo:
            raise ValueError('Demo mode supports inspection and listening only; it cannot run production commands')
        row = self.inspect_tape(tape)
        if not row['channels']:
            raise ValueError('; '.join(row['errors']))
        s = row['stages']
        channel = options.get('channel')
        if channel not in (None, ''):
            channel = int(channel)
            if channel not in {r['channel'] for r in row['channels']}:
                raise ValueError('Requested channel is absent from this tape')
            if action not in ('denoise','deflutter','retime'):
                raise ValueError('This action runs once per tape; clear the channel selection')
        speech = [r for r in row['channels'] if r['role'] != 'timing_only' and (channel is None or r['channel'] == channel)]
        workers = int(options.get('workers', 2))
        if not 1 <= workers <= 16:
            raise ValueError('Use 1–16 workers')
        prefix = [sys.executable, '-u']
        common = ['--concatenated', str(self.root('originals')), '--denoised-root', str(self.root('denoised')),
                  '--carrier-survey-root', str(self.root('surveys')), '--flutter-planning-root', str(self.root('plans')),
                  '--defluttered-root', str(self.root('defluttered')), '--retimed-root', str(self.root('retimed')),
                  '--quindar-analysis-root', str(self.root('quindar')), '--output', str(self.state/'runs')]
        if action == 'denoise':
            return prefix+[str(HERE/'pipeline.py'), 'run', '--stages', '03', '--tape', tape,
                           '--denoise-workers', str(workers), '--plain']+common+(['--channel',str(channel)] if channel else ['--all-channels'])
        if action in ('survey', 'carrier_benchmark'):
            cmd = prefix+[str(HERE/'carrier_survey.py'),'--input',str(self.root('originals')/tape),'--output',str(self.root('surveys'))]
            return cmd+(['--max-windows','30'] if action == 'carrier_benchmark' else [])
        if action in ('pilot', 'plan'):
            if s['survey']['status'] != 'complete':
                raise ValueError('Complete the original carrier survey before planning')
            cmd = prefix+[str(HERE/'05a_plan_deflutter.py'),'--input',str(self.root('originals')/tape),
                          '--planning-root',str(self.root('plans')),'--carrier-survey-root',str(self.root('surveys')),'--workers',str(workers)]
            if action == 'plan':
                try:
                    require_rollout(self.rollout())
                except (ValueError, OSError, KeyError, TypeError) as exc:
                    raise ValueError('Accept the current methodology in the listening desk before full-tape planning: '+str(exc)) from exc
                return cmd+['--rollout-approval',str(self.rollout())]
            start, duration = float(options.get('start',0)), float(options.get('duration',180))
            if not math.isfinite(start+duration) or start < 0 or not 15 <= duration <= 600 or start+duration > row['duration_seconds']:
                raise ValueError('Pilot must be a 15–600 second interval inside this tape')
            return cmd+['--start',str(start),'--duration',str(duration)]
        if action == 'deflutter':
            if s['plan']['status'] != 'complete':
                raise ValueError('Generate a complete current full-tape plan first')
            if not speech:
                raise ValueError('Select a speech channel')
            pending = [r for r in speech if r['defluttered'] != 'complete']
            if not pending:
                raise ValueError('Selected channels already have current complete defluttered outputs')
            for r in pending:
                if not r['denoised']:
                    raise ValueError(f'CH{r["channel"]:02d}: complete denoising first')
                try:
                    require_production(self.approval(tape), self.root('plans')/tape/'plan.json', r['channel'])
                except (ValueError, OSError, KeyError, TypeError) as exc:
                    raise ValueError(f'CH{r["channel"]:02d}: current production acceptance required: {exc}') from exc
                out = self.root('defluttered')/tape/f'CH{r["channel"]:02d}'
                if out.exists() and any(out.iterdir()):
                    raise ValueError(f'CH{r["channel"]:02d}: existing output needs inspection; the dashboard will not overwrite it')
            # All-channels CLI does not skip existing 05b output. Render the remaining
            # exact accepted channels through a fixed local batch adapter instead.
            return prefix+[str(HERE/'pipeline_app.py'),'--render-batch',tape,'--batch-config',str(self.state/'runner_config.json'),
                           '--batch-channels',','.join(str(r['channel']) for r in pending)]
        if action == 'quindar':
            if s['defluttered']['status'] != 'complete':
                raise ValueError('Every speech channel must finish current defluttering before Quindar analysis')
            if row['excluded_files']:
                raise ValueError('Identify the unlabelled recording before exhaustive Quindar analysis')
            return prefix+[str(HERE/'quindar_survey.py'),'--original-root',str(self.root('originals')),
                           '--defluttered-root',str(self.root('defluttered')),'--output',str(self.root('quindar')/'per_tape'/tape),
                           '--tape',tape,'--workers',str(workers)]
        if action == 'speed_plan':
            if s['quindar']['status'] != 'complete' or s['defluttered']['status'] != 'complete':
                raise ValueError('Complete the current exhaustive post-deflutter survey first')
            return prefix+[str(HERE/'05c_retime.py'),'--tape',tape,'--plan-only','--quindar-analysis-root',str(self.analysis_root(tape))]
        if action == 'retime':
            if s['speed_plan']['status'] != 'complete' or s['defluttered']['status'] != 'complete':
                raise ValueError('Complete defluttering and freeze the shared speed curve first')
            if not speech:
                raise ValueError('Select a speech channel')
            return prefix+[str(HERE/'05c_retime.py'),'--tape',tape,'--defluttered-root',str(self.root('defluttered')),
                           '--retimed-root',str(self.root('retimed')),'--quindar-analysis-root',str(self.analysis_root(tape))]+(
                               ['--channel',str(channel)] if channel else ['--all-channels'])
        raise ValueError('Unsupported action')

    def preview(self, value):
        tapes = value.get('tapes', [])
        if not isinstance(tapes, list) or not 1 <= len(tapes) <= 39 or len(set(tapes)) != len(tapes):
            raise ValueError('Choose 1–39 distinct Apollo 15 tapes')
        options = {k:v for k,v in value.get('options',{}).items() if k in ('channel','workers','start','duration')}
        action = value['action']
        if action not in ACTIONS:
            raise ValueError('Unknown action')
        rows = []
        for tape in tapes:
            self.tape_path(tape)
            try:
                command = self.command(tape, action, options)
                rows.append(dict(tape=tape, action=action, options=options, command=command, ready=True))
            except (ValueError, OSError, KeyError, TypeError, RuntimeError) as exc:
                rows.append(dict(tape=tape, action=action, options=options, ready=False, error=str(exc)))
        preview = dict(id=uuid.uuid4().hex, created_at=now(), action=action, jobs=rows,
                       ready=sum(r['ready'] for r in rows), blocked=sum(not r['ready'] for r in rows))
        with self.lock:
            self.previews = {preview['id']:preview}
        return preview

    def enqueue(self, preview_id):
        with self.lock:
            preview = self.previews.get(preview_id)
            if not preview:
                raise ValueError('Preview expired; preview this work again')
            added = []
            for row in preview['jobs']:
                if not row['ready']:
                    continue
                key = fingerprint(dict(tape=row['tape'],action=row['action'],options=row['options']))
                if any(j.get('key') == key and j['status'] in ACTIVE for j in self.jobs):
                    continue
                job = dict(deepcopy(row), id=uuid.uuid4().hex, key=key, status='queued', created_at=now())
                self.jobs.append(job)
                save(self.state/'jobs'/f'{job["id"]}.json', job)
                added.append(job['id'])
            self.previews.clear()
            self.wake.set()
            return dict(added=added, paused=self.paused)

    def queue_control(self, action):
        with self.lock:
            if action not in ('start','pause'):
                raise ValueError('Unknown queue control')
            if action == 'start' and self.recovery_errors:
                raise ValueError('Inspect unreadable job records before starting more processing')
            if action == 'start' and any(j['status']=='interrupted' and j.get('pid') and self._pid_alive(j['pid']) for j in self.jobs):
                raise ValueError('A process from an interrupted job may still be running; inspect it before starting more work')
            self.paused = action == 'pause'
            self.wake.set()
            return dict(paused=self.paused)

    @staticmethod
    def _pid_alive(pid):
        if os.name == 'nt':
            # os.kill(pid, 0) can terminate a process on Windows.
            import ctypes
            handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))
            if not handle:
                return False
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        try:
            os.kill(int(pid),0)
            return True
        except OSError:
            return False

    def cancel(self, job_id):
        with self.lock:
            job = next((j for j in self.jobs if j['id'] == job_id), None)
            if not job:
                raise ValueError('Unknown job')
            if job['status'] == 'queued':
                job.update(status='cancelled',finished_at=now())
            elif job['status'] == 'running':
                job['status'] = 'stopping'
                self.paused = True
                self._stop_process()
            else:
                raise ValueError('Only queued or running work can be stopped')
            save(self.state/'jobs'/f'{job_id}.json',job)
            return dict(job)

    def _stop_process(self):
        process = self.process
        if process and process.poll() is None:
            if os.name == 'nt':
                subprocess.run(['taskkill','/PID',str(process.pid),'/T','/F'],stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW, check=False)
            else:
                os.killpg(process.pid, signal.SIGTERM)

    def _worker(self):
        while not self.closed:
            self.wake.wait(1)
            self.wake.clear()
            while not self.closed:
                with self.lock:
                    job = next((j for j in self.jobs if j['status']=='queued'), None)
                    if self.paused or not job:
                        break
                    job.update(status='running', started_at=now())
                    save(self.state/'jobs'/f'{job["id"]}.json',job)
                try:
                    # A preview is not a permanent grant: inputs and approvals can change.
                    command = self.command(job['tape'],job['action'],job['options'])
                    if command != job['command']:
                        raise ValueError('Job scope changed since preview; create a new preview')
                    save(self.state/'runner_config.json', self.config)
                    with (self.state/'jobs'/f'{job["id"]}.log').open('ab',buffering=0) as log:
                        log.write(('Started '+now()+'\n'+subprocess.list2cmdline(command)+'\n\n').encode())
                        with self.lock:
                            if job['status'] == 'stopping' or self.closed:
                                raise ValueError('Stopped before process launch')
                            self.process = subprocess.Popen(command, cwd=REPO, stdin=subprocess.DEVNULL,
                                stdout=log, stderr=subprocess.STDOUT, env={**os.environ,'PYTHONIOENCODING':'utf-8','PYTHONUNBUFFERED':'1'},
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
                                start_new_session=os.name != 'nt')
                            job['pid'] = self.process.pid
                            save(self.state/'jobs'/f'{job["id"]}.json',job)
                        code = self.process.wait()
                    with self.lock:
                        job.update(returncode=code, status='cancelled' if job['status']=='stopping' else 'complete' if code==0 else 'failed')
                        if code != 0:
                            job['error'] = 'Processing stopped or failed; inspect the log before retrying.'
                except Exception as exc:
                    with self.lock:
                        job.update(status='cancelled' if job['status']=='stopping' else 'blocked',error=str(exc))
                finally:
                    with self.lock:
                        self.process = None
                        job['finished_at'] = now()
                        save(self.state/'jobs'/f'{job["id"]}.json',job)
                        if job['status'] != 'complete':
                            self.paused = True
                    self.desk.submit('Refresh pipeline after job', self.scan, coalesce=True)

    def log(self, job_id):
        with self.lock:
            job = next((j for j in self.jobs if j['id']==job_id), None)
            if not job:
                raise ValueError('Unknown job')
            result = deepcopy(job)
        path = self.state/'jobs'/f'{job_id}.log'
        result['tail'] = ''
        if path.exists():
            with path.open('rb') as stream:
                stream.seek(max(0,path.stat().st_size-48000))
                result['tail'] = stream.read().decode('utf-8',errors='replace')
        return result

    def note(self, value):
        tape = value['tape']
        self.tape_path(tape)
        text = str(value.get('text','')).strip()
        if not text or len(text)>4000:
            raise ValueError('Enter a note of 1–4000 characters')
        record = dict(id=uuid.uuid4().hex, tape=tape, text=text, created_at=now())
        save(self.state/'notes'/tape/f'{record["created_at"].replace(":","-")}_{record["id"]}.json',record)
        return record

    def close(self):
        with self.lock:
            self.closed = True
            self.paused = True
            for j in self.jobs:
                if j['status']=='running':
                    j['status']='stopping'
            self._stop_process()
            self.wake.set()
        self.thread.join(timeout=10)
