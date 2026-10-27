"""Headless deflutter evidence, bounded listening excerpts and acceptance records.

Only map construction, reserved numeric checks and bounded excerpts run here.
Stage 03 and production audio rendering are deliberately separate commands.
"""
from concurrent.futures import CancelledError
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import secrets
import shutil
import threading
import uuid

import numpy as np
import soundfile as sf

from carrier_survey import load_survey
from carrier_inventory import load_inventory
from channel_review import (completed_denoised, continuous_map, denoised_signature,
                            joined_map_checks, prepare, check_source)
from flutter import resample_at
from flutter_plan_store import load_saved_plan, checked_artifact, is_continuous_plan, iter_plan_artifacts
from flutter_planning import inventory
from flutter_policy import (METHODOLOGY, digest,
                            implementation_digest, policy_digest, require_rollout)
from review_sampling import (POLICY, PILOTS, CONDITIONS, FLAGS, ELIGIBLE, fingerprint,
                             policy_hash, read_json, immutable_json, bound, verify_bindings,
                             cohort_key, select_samples, assess, adverse, validate_map_review)
from review_sampling import (effective_coverage, audio_activity, listening_requirements,
                             validate_tape_spot_check, comparison_change, automatic_no_change)
from review_worker import ReviewWorker

HERE = Path(__file__).resolve().parent


def now():
    return datetime.now(timezone.utc).isoformat()


def file_hash(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(4*1024*1024), b''):
            h.update(block)
    return h.hexdigest()


def review_intervals(folder, plan):
    """Expose actual reference lifetimes to sampling without inventing map joins."""
    for summary in plan['regions']:
        if summary['status'] != 'analyzed':
            continue
        _, raw = checked_artifact(folder, summary['detail_file'], summary['detail_sha256'])
        region = json.loads(raw)
        if not is_continuous_plan(plan):
            yield summary, region
            continue
        for target in region['targets']:
            spans = target.get('reference_spans') or [dict(
                start_seconds=region['start_seconds'], end_seconds=region['end_seconds'],
                action='leave_uncorrected', reason=target['reason'])]
            for index, span in enumerate(spans):
                applied = span['action'] == 'use_reference'
                evidence_kind = span.get('evidence_kind')
                action = ('group_render_and_review' if evidence_kind == 'transfer_group' else
                          'render_and_review' if evidence_kind == 'direct_numeric' else target['action'])
                local = dict(target, selected=span.get('selected'),
                             reference_channel=span.get('reference_channel'), reference_spans=[span],
                             action=action if applied else 'leave_uncorrected',
                             reason=span.get('reason', target['reason']))
                for field in ('evidence', 'numeric_transfer_passed', 'transfer_group', 'quality_level', 'restoration_target_met'):
                    if field in span:
                        local[field] = span[field]
                interval = dict(region, id=f'{region["id"]}_ch{target["channel"]}_span{index:05d}',
                                start_seconds=span['start_seconds'], end_seconds=span['end_seconds'],
                                targets=[local])
                yield interval, interval


class Roots:
    def __init__(self, config):
        self.roots = [Path(config[k]).resolve() for k in ('originals', 'surveys', 'plans', 'denoised', 'state')]
        self.roots += [Path(p).resolve() for p in config.get('evidence_roots', [])]

    def check(self, path):
        path = Path(path).resolve()
        if not any(path == r or r in path.parents for r in self.roots):
            raise ValueError('Artifact is outside configured data roots: '+str(path))
        return path


class Desk:
    def __init__(self, config):
        self.config = config
        self.guard = Roots(config)
        self.state = Path(config['state']).resolve()
        for key in ('originals', 'surveys', 'plans', 'denoised'):
            other = Path(config[key]).resolve()
            if self.state == other or other in self.state.parents or self.state in other.parents:
                raise ValueError('Review state must be separate from every input root')
        self.state.mkdir(parents=True, exist_ok=True)
        self.demo = bool(config.get('demo'))
        self.pool = ReviewWorker()
        self.lock = threading.RLock()
        self.closed = False
        self.jobs = {}
        self.pending_refreshes = {}
        self.catalog = dict(tapes=[], channels=[], candidates=[], required_conditions=[], errors=[], scanned_at=None)
        self.token = secrets.token_urlsafe(32)
        self.preview_cache = {}
        self.hash_cache = {}
        self.activity_cache = {}
        self.comparison_cache = {}

    def verify(self, bindings, force=False):
        """Cache file identity for browsing; deliberate approval always rehashes."""
        for item in bindings:
            path = self.guard.check(item['path'])
            stat = path.stat()
            key = (str(path), stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, item['sha256'])
            if force or key not in self.hash_cache:
                if file_hash(path) != item['sha256']:
                    raise ValueError('Stale evidence: '+str(path))
                self.hash_cache[key] = True

    def submit(self, name, fn, *, coalesce=False):
        with self.lock:
            if self.closed:
                return None
            # Bound work in flight; double clicks reuse the current job.
            running = next((j for j in self.jobs.values() if j['status'] in ('queued', 'running')), None)
            if running:
                if coalesce:
                    self.pending_refreshes[fn] = name
                return running
            job = dict(id=uuid.uuid4().hex, name=name, status='queued', progress='', started_at=now())
            self.jobs[job['id']] = job

        def run():
            def progress(message):
                with self.lock:
                    if self.closed:
                        raise CancelledError('Review interrupted by app shutdown')
                    job['progress'] = str(message)
            try:
                progress('Starting')
                job['status'] = 'running'
                result = fn(progress)
                job.update(status='complete', result=result, finished_at=now())
            except Exception as exc:
                job.update(status='cancelled' if self.closed else 'failed', error=str(exc), finished_at=now())
            finally:
                with self.lock:
                    if self.pending_refreshes and not self.closed:
                        followup, title = next(iter(self.pending_refreshes.items()))
                        del self.pending_refreshes[followup]
                        self.submit(title, followup, coalesce=True)
        self.pool.submit(run)
        return job

    def close(self):
        # A request may hold the desk lock while hashing approval evidence.
        # Shutdown must not wait for that request before releasing the terminal.
        self.closed = True
        self.pending_refreshes.clear()
        self.pool.shutdown(wait=False, cancel_futures=True)

    def safe_plan(self, path):
        path = self.guard.check(path)
        plan = read_json(path)
        for row in plan['inventory']['channels']+plan['inventory'].get('excluded_files', []):
            self.guard.check(row['path'])
        survey = plan['inventory'].get('carrier_survey')
        if survey:
            self.safe_survey(survey['path'])
        for r in plan['regions']:
            for name in [r.get('detail_file'), r.get('maps', {}).get('file')]:
                if name:
                    self.guard.check(path.parent/name)
        return load_saved_plan(path, expand_regions=False)

    def safe_survey(self, path, *, inventory_only=False):
        path = self.guard.check(path)
        if path.is_dir():
            path /= 'survey.json'
        survey = read_json(path)
        self.guard.check(survey['source'])
        for r in survey['files']:
            self.guard.check(r['path'])
            self.guard.check(path.parent/r['cache'])
        return (load_inventory if inventory_only else load_survey)(path)

    def plan_bindings(self, path, plan):
        bindings = []
        for artifact, expected in iter_plan_artifacts(path.parent, plan):
            artifact = self.guard.check(artifact)
            record = bound(artifact)
            if record['sha256'] != expected:
                raise ValueError('Saved plan artifact changed while binding review evidence')
            bindings.append(record)
        return bindings

    def baseline_tapes(self):
        baseline = HERE.parent/'reports/bespoke_expanded_20261017/summary.json'
        if baseline.exists():
            return {r['tape'] for r in read_json(baseline)['rows']}
        return set(self.config.get('prior_study_tapes', []))

    def scan(self, progress):
        tapes, channels, candidates = [], [], []
        folders = sorted(Path(self.config['originals']).glob('A15_T*'))
        old_tapes = self.baseline_tapes()
        newly = next((p.name for p in folders if old_tapes and p.name not in old_tapes and p.name not in PILOTS), None)
        if self.demo:
            newly = 'A15_T599999'
        discovered_islands = False
        for i, folder in enumerate(folders):
            progress(f'Inventory {i+1}/{len(folders)}: {folder.name}; no audio processing')
            row = dict(tape=folder.name, denoised_channels=0, expected_channels=0,
                       survey_complete=False, plan_complete=False, map_checks_passed=0,
                       production_channels=0, coverage={}, errors=[], excluded_intervals=[],
                       search_budget_exhausted=0, newly_inventoried=folder.name == newly)
            tapes.append(row)
            try:
                self.guard.check(folder)
                for source_path in folder.rglob('*.wav'):
                    self.guard.check(source_path)
                inv = inventory(folder)
                targets = [r for r in inv['channels'] if r['role'] != 'timing_only']
                row['expected_channels'] = len(targets)
                for target in targets:
                    den = Path(self.config['denoised'])/folder.name/f'CH{target["channel"]:02d}'
                    for name in ('denoised.wav','denoised.json','cleanup.jsonl'):
                        self.guard.check(den/name)
                    row['denoised_channels'] += int(completed_denoised(target, den, 0, target['frames']))
                path = Path(self.config['plans'])/folder.name/'plan.json'
                override = self.config.get('plan_overrides', {}).get(folder.name)
                if override:
                    path = self.guard.check(override)
                elif not path.exists():
                    audits = sorted((Path(self.config['plans'])/folder.name/'audits').glob('*/plan.json'),key=lambda p:p.stat().st_mtime)
                    if audits:
                        path = audits[-1]
                try:
                    plan = self.safe_plan(path)
                except (ValueError, OSError, KeyError, TypeError) as exc:
                    row['errors'].append('Plan unavailable/stale: '+str(exc))
                    continue
                islands = []
                row['survey_required'] = not is_continuous_plan(plan)
                if row['survey_required']:
                    try:
                        index, surveys = self.safe_survey(Path(self.config['surveys'])/folder.name, inventory_only=True)
                        row['survey_complete'] = Path(index['source']).resolve() == folder.resolve()
                        row['survey_performance'] = index.get('performance', {})
                        discovered_islands |= any(s.get('islands') for s in surveys)
                        for source in surveys:
                            original = next((r for r in inv['channels'] if Path(r['path']).resolve() == Path(source['source']['path']).resolve()), None)
                            if original:
                                for island in source.get('islands', []):
                                    islands.append(dict(channel=original['channel'],
                                        start_seconds=original['start_seconds']+island['start_sample']/source['source']['sample_rate'],
                                        end_seconds=original['start_seconds']+island['end_sample']/source['source']['sample_rate']))
                    except (ValueError, OSError, KeyError, TypeError) as exc:
                        row['errors'].append('Carrier survey: '+str(exc))
                compatible = plan.get('methodology') == METHODOLOGY and plan.get('implementation_sha256') == implementation_digest() and plan.get('policy_sha256') == policy_digest()
                if plan.get('demo') and not self.demo:
                    compatible = False
                row['plan_complete'] = bool(compatible and plan.get('complete') and plan.get('full_tape'))
                row['pilot_plan_available'] = bool(compatible and plan.get('complete') and not plan.get('full_tape'))
                if not compatible:
                    row['errors'].append('Plan methodology, implementation or numeric policy is stale')
                if not plan.get('full_tape'):
                    row['errors'].append('Bounded plan: production requires a full-channel timing map')
                annotation_path = path.parent/'review_conditions.json'
                self.guard.check(annotation_path)
                annotations = read_json(annotation_path) if annotation_path.exists() else {}
                plan_hash = digest(path)
                channel_index = {}
                for target in targets:
                    ch = target['channel']
                    den = Path(self.config['denoised'])/folder.name/f'CH{ch:02d}'
                    ready = completed_denoised(target, den, 0, target['frames'])
                    item = dict(tape=folder.name, channel=ch, head=target.get('head'), source=target,
                                plan_path=str(path.resolve()), plan_sha256=plan_hash, intervals=[], cohorts=[], required_conditions=[],
                                blockers=[], production_blockers=[], full_tape=bool(plan.get('full_tape')),
                                denoised=str(den.resolve()), survey_complete=row['survey_complete'],
                                continuous_reference=is_continuous_plan(plan))
                    if not compatible or not plan.get('complete'):
                        item['blockers'].append('Complete compatible plan required')
                    if not row['plan_complete']:
                        item['production_blockers'].append('Production requires a complete compatible full-tape plan; this is pilot evidence only')
                    if not ready:
                        item['blockers'].append('Prepared stage-03 intermediate missing or stale')
                    if row['survey_required'] and not row['survey_complete']:
                        item['blockers'].append('Original carrier survey incomplete or stale')
                    if self.config.get('clock_conflicts', {}).get(folder.name):
                        item['blockers'].append('Unresolved recorded clock conflict')
                    clock_report = path.parent/'clock_conflicts.json'
                    self.guard.check(clock_report)
                    if clock_report.exists() and read_json(clock_report).get('status') != 'consistent':
                        item['blockers'].append('Unresolved recorded clock conflict')
                    item['clock_reports'] = []
                    for report in self.config.get('clock_reports', {}).get(folder.name, []):
                        report_path = self.guard.check(report)
                        clock = read_json(report_path)
                        item['clock_reports'].append(bound(report_path))
                        if clock.get('clock_constraints', {}).get('status', clock.get('status')) != 'consistent':
                            item['blockers'].append('Configured clock evidence has unresolved conflicts or insufficient evidence')
                    # Diagnostic playback still requires valid inputs and clocks.
                    # Numeric qualification below gates acceptance, not exploration.
                    item['diagnostic_blockers'] = list(item['blockers'])
                    if item['full_tape']:
                        item['diagnostic_blockers'].append('Diagnostic pilot listening requires a bounded pilot plan')
                    channels.append(item)
                    channel_index[ch] = item
                for summary, region in review_intervals(path.parent, plan):
                    exhausted = region.get('search_budget', {}).get('exhausted', False)
                    row['search_budget_exhausted'] += int(exhausted)
                    variants = {v['key']: v for v in region.get('variants', [])}
                    for t in region['targets']:
                        item = channel_index.get(t['channel'])
                        if not item:
                            continue
                        a, b = max(summary['start_seconds'], item['source']['start_seconds']), min(summary['end_seconds'], item['source']['end_seconds'])
                        if b <= a:
                            continue
                        category = ELIGIBLE.get(t['action'], 'proposal' if t['action'] == 'validate_transfer' else 'bypass')
                        if t['action'] in ELIGIBLE:
                            supported = sum(max(0,min(b,s['end_seconds'])-max(a,s['start_seconds'])) for s in t.get('reference_spans',[]) if s['action'] == 'use_reference')
                            faded = sum(max(0,min(b,s['end_seconds'])-max(a,s['start_seconds'])) for s in t.get('reference_spans',[]) if s['action'] == 'fade_correction')
                            row['coverage'][category] = row['coverage'].get(category,0)+supported
                            row['coverage']['transition_fades'] = row['coverage'].get('transition_fades',0)+faded
                            row['coverage']['bypass'] = row['coverage'].get('bypass',0)+max(0,b-a-supported-faded)
                        else:
                            row['coverage'][category] = row['coverage'].get(category,0)+b-a
                        v = variants.get(t.get('selected'), {})
                        evidence = t.get('evidence', {})
                        quality = t.get('quality_level')
                        strong = (t.get('numeric_transfer_passed') is True and evidence.get('level') == 'multiple_time_windows') if category == 'direct_numeric' else t.get('transfer_group', {}).get('passed') is True if category == 'transfer_group' else False
                        reference_identity = f"CH{v.get('reference_channel', t.get('reference_channel'))}@{v.get('carrier_hz','unknown')}Hz"
                        cohort = cohort_key(item['head'], reference_identity, v.get('method'), category+(':'+quality if quality else ''))
                        interval = dict(start_seconds=a, end_seconds=b, region=region['id'], category=category,
                                        applied=t['action'] in ELIGIBLE, eligible=category in ('direct_numeric','transfer_group') and strong,
                                        cohort=cohort, reason=t['reason'], selected=t.get('selected'),
                                        quality_level=quality, restoration_target_met=t.get('restoration_target_met', False),
                                        support_spans=t.get('reference_spans', []))
                        item['intervals'].append(interval)
                        if exhausted and 'Reference search budget exhausted' not in item['blockers']:
                            item['blockers'].append('Reference search budget exhausted')
                        if interval['applied'] and not strong:
                            item['blockers'].append(f'{region["id"]}: sparse or failed direct/group numeric evidence')
                        if interval['applied'] and not interval['support_spans']:
                            item['blockers'].append(f'{region["id"]}: reference support spans are missing')
                        if interval['eligible']:
                            item['cohorts'].append(cohort)
                        else:
                            row['excluded_intervals'].append(dict(channel=t['channel'], **interval))
                        if b-a < 15 or not (row['plan_complete'] or row['pilot_plan_available']):
                            continue
                        length = min(20., b-a)
                        center = (a+b)/2
                        sr = item['source']['sample_rate']
                        start_frame = round((center-length/2-item['source']['start_seconds'])*sr)
                        conditions = ['tape:'+folder.name, 'head:'+str(item['head']), 'reference:'+str(v.get('reference_channel', t.get('reference_channel'))),
                                      'method:'+str(v.get('method')), 'evidence:'+category]
                        if quality:
                            conditions.append('quality:'+quality)
                        fraction = (center-item['source']['start_seconds'])/(item['source']['end_seconds']-item['source']['start_seconds'])
                        conditions.append('early' if fraction < 1/3 else 'middle' if fraction < 2/3 else 'late')
                        if interval['eligible']:
                            conditions.append('cohort:'+cohort)
                        if folder.name == newly:
                            conditions.append('newly_inventoried')
                        if category == 'transfer_group' and not t.get('holdout_event_count', 0):
                            conditions.append('no_beep_group')
                        if any(s['channel'] == v.get('reference_channel') and s['start_seconds'] < center+length/2 and s['end_seconds'] > center-length/2 for s in islands):
                            conditions.append('timecode_island')
                        note = annotations.get(region['id'], {}).get(str(t['channel']), {})
                        conditions += [tag for tag in note.get('conditions', []) if tag in CONDITIONS or tag == 'timecode_island']
                        risk = (4 if not interval['eligible'] else 0) + (2 if category == 'transfer_group' else 0)
                        risk += 2*int(any(c in conditions for c in ('strong_correction','dropout','fade','reference_change')))
                        gaps = [s for s in t.get('reference_spans', []) if s.get('action') != 'use_reference']
                        if any(s['start_seconds'] < center+length/2 and s['end_seconds'] > center-length/2 for s in gaps):
                            conditions.append('dropout'); risk += 3
                        candidate = dict(id=fingerprint([folder.name, t['channel'], start_frame, round(length*sr), plan_hash])[:24],
                                         tape=folder.name, channel=t['channel'], start_frame=start_frame, frames=round(length*sr), sample_rate=sr,
                                         start_seconds=item['source']['start_seconds']+start_frame/sr, region=region['id'],
                                         conditions=conditions, risk=risk, eligible=interval['eligible'], cohort=cohort,
                                         plan_sha256=plan_hash, selected=t.get('selected'), reference=v,
                                         support=t.get('reference_spans', []), evidence=evidence,
                                         numeric_results=dict(holdout=t.get('holdout'), transfer_group=t.get('transfer_group'), reason=t['reason']),
                                         annotations=note)
                        if interval['eligible']:
                            item['required_conditions'].extend(conditions)
                        candidates.append(candidate)
                        if is_continuous_plan(plan) and b-a >= 60:
                            # Long lifetimes need temporal coverage without
                            # turning samples into correction boundaries.
                            for center_sample in (a+length/2, b-length/2):
                                frame = round((center_sample-length/2-item['source']['start_seconds'])*sr)
                                fraction_sample = (center_sample-item['source']['start_seconds'])/(item['source']['end_seconds']-item['source']['start_seconds'])
                                temporal = 'early' if fraction_sample < 1/3 else 'middle' if fraction_sample < 2/3 else 'late'
                                sample_conditions = [c for c in conditions if c not in ('early','middle','late')]+[temporal]
                                candidates.append(dict(candidate, id=fingerprint([candidate['id'],frame])[:24],
                                    start_frame=frame, start_seconds=item['source']['start_seconds']+frame/sr,
                                    conditions=sample_conditions))
                                if interval['eligible']:
                                    item['required_conditions'].append(temporal)
                        if gaps and 'dropout' not in conditions:
                            gap = max(gaps, key=lambda g:g['end_seconds']-g['start_seconds'])
                            center_gap = (gap['start_seconds']+gap['end_seconds'])/2
                            gap_frame = max(0,min(item['source']['frames']-round(length*sr),round((center_gap-length/2-item['source']['start_seconds'])*sr)))
                            candidates.append(dict(candidate,id=fingerprint([candidate['id'],'dropout',gap_frame])[:24],
                                start_frame=gap_frame,start_seconds=item['source']['start_seconds']+gap_frame/sr,
                                conditions=conditions+['dropout'],risk=risk+3))
                            if interval['eligible']:
                                item['required_conditions'].append('dropout')
                        # Review boundaries using the same full-channel map, not a local anchor.
                        previous = next((x for x in reversed(item['intervals'][:-1]) if x['end_seconds'] == a), None)
                        if previous and previous['selected'] != interval['selected'] and a-item['source']['start_seconds'] >= 10 and item['source']['end_seconds']-a >= 10:
                            boundary = dict(candidate)
                            boundary['start_frame'] = round((a-10-item['source']['start_seconds'])*sr)
                            boundary['start_seconds'] = a-10
                            boundary['frames'] = 20*sr
                            boundary['id'] = fingerprint([candidate['id'], 'boundary'])[:24]
                            transition = next((z for z in plan.get('transitions',[]) if z['at_seconds'] == a), {})
                            transition_target = next((z for z in transition.get('targets',[]) if z['channel'] == t['channel']), {})
                            fade_expected = 'fade' in transition_target.get('action',transition.get('action',''))
                            boundary['conditions'] = conditions+['reference_change']+(['fade'] if fade_expected else [])
                            boundary['risk'] = risk+4
                            boundary['cohorts'] = sorted({previous['cohort'], cohort})
                            candidates.append(boundary)
                            if interval['eligible']:
                                item['required_conditions'].extend(['reference_change']+(['fade'] if fade_expected else []))
                for item in channel_index.values():
                    item['cohorts'] = sorted(set(item['cohorts']))
                    item['required_conditions'] = sorted(set(item['required_conditions']) | {'tape:'+item['tape'], 'head:'+str(item['head'])} |
                                                          {'cohort:'+x for x in item['cohorts']})
                    record = self.map_cached(item)
                    if record and record.get('joined_map_checks', {}).get('status') == 'passed':
                        row['map_checks_passed'] += 1
                    item['map_check_status'] = record.get('joined_map_checks', {}).get('status') if record else 'missing'
                    if record:
                        item['required_conditions'] = sorted(set(item['required_conditions']) | {tag for marker in record['markers'] for kind,tag in (('fade','fade'),('support_gap','dropout')) if marker['kind'] == kind})
                    try:
                        from flutter_policy import require_production
                        approval = self.state/'approvals'/folder.name/'production_acceptance.json'
                        require_production(approval, path, item['channel'])
                        row['production_channels'] += 1
                    except (ValueError, OSError, KeyError, TypeError, StopIteration):
                        pass
            except (ValueError, OSError, KeyError, TypeError, RuntimeError) as exc:
                row['errors'].append(str(exc))
        required = {'tape:'+t for t in PILOTS} | set(CONDITIONS) | {'newly_inventoried'}
        if discovered_islands:
            required.add('timecode_island')
        result = dict(tapes=tapes, channels=channels, candidates=candidates, required_conditions=sorted(required),
                      errors=[] if folders else ['No canonical tape folders found under configured originals root'], scanned_at=now(),
                      newly_inventoried=newly, policy_version=POLICY, demo=self.demo)
        with self.lock:
            self.catalog = result
        return dict(tapes=len(tapes), candidate_count=len(candidates), newly_inventoried=newly)

    def item(self, tape, channel):
        return next(c for c in self.catalog['channels'] if c['tape'] == tape and c['channel'] == channel)

    def map_directory(self, item, diagnostic=False):
        identity = [item['plan_sha256'], item['channel'], implementation_digest(), policy_hash()]
        if diagnostic:
            identity.append('diagnostic-pilot')
        return self.state/'maps'/fingerprint(identity)

    def map_cached(self, item, diagnostic=False):
        path = self.map_directory(item, diagnostic)/'review.json'
        if not path.exists():
            return None
        try:
            review = read_json(path)
            self.verify(review['bindings'])
            for optional in review.get('optional_inputs', []):
                if Path(optional['path']).exists() != optional['exists']:
                    return None
            for name, sha in review['timing_artifacts'].items():
                if file_hash(path.parent/name) != sha:
                    return None
            return review
        except (ValueError, OSError, KeyError):
            return None

    def build_map(self, item, progress, diagnostic=False):
        blockers = item['diagnostic_blockers'] if diagnostic else item['blockers']
        if blockers:
            raise ValueError('; '.join(blockers))
        cached = self.map_cached(item, diagnostic)
        if cached:
            return self.map_directory(item, diagnostic)/'review.json'
        path = Path(item['plan_path'])
        source_plan = self.safe_plan(path)
        map_plan_path = path if source_plan.get('full_tape') else self.pilot_map_plan(path,source_plan)
        original = item['source']
        _, plan, row, regions, start, offset, frames = prepare(map_plan_path, item['channel'], original['start_seconds'])
        den = self.guard.check(Path(item['denoised']))
        if not completed_denoised(row, den, 0, frames):
            raise ValueError('Prepared stage-03 intermediate missing or stale')
        progress(f'{item["tape"]} CH{item["channel"]}: hashing prepared denoised input; no denoising')
        bindings = [bound(path), bound(map_plan_path), bound(den/'denoised.json'), bound(den/'cleanup.jsonl')]
        bindings += item['clock_reports']
        audio_path = den/'denoised.wav'
        before = audio_path.stat()
        audio_binding = dict(path=str(audio_path.resolve()), sha256=file_hash(audio_path))
        tt, nominal, speed, stats, markers, evidence = continuous_map(map_plan_path.parent, regions, row['channel'], start, frames, row['sample_rate'], diagnostic, progress)
        progress(f'{item["tape"]} CH{item["channel"]}: checking reserved events through the full joined map')
        checks = joined_map_checks(map_plan_path.parent, regions, row, row['channel'], start, tt, nominal, speed)
        if diagnostic:
            checks = dict(checks, automated_status=checks['status'], status='diagnostic_only')
        bindings.extend(self.plan_bindings(map_plan_path, plan))
        bindings.extend(self.plan_bindings(path, source_plan))
        if not is_continuous_plan(source_plan):
            survey_path = Path(self.config['surveys'])/item['tape']/'survey.json'
            self.safe_survey(survey_path)
            bindings.append(bound(survey_path))
            for cache in read_json(survey_path)['files']:
                cache_path = survey_path.parent/cache['cache']
                bindings.append(bound(cache_path))
                for block in read_json(cache_path)['feature_blocks']:
                    bindings.append(bound(survey_path.parent/block['file']))
        optional_inputs = []
        for optional in ('review_conditions.json', 'clock_conflicts.json'):
            optional_inputs.append(dict(path=str((path.parent/optional).resolve()), exists=(path.parent/optional).exists()))
            if (path.parent/optional).exists():
                bindings.append(bound(path.parent/optional))
        if (before.st_size, before.st_mtime_ns) != (audio_path.stat().st_size, audio_path.stat().st_mtime_ns):
            raise ValueError('Denoised audio changed during map construction')
        check_source(row)
        folder = self.map_directory(item, diagnostic)
        if folder.exists():
            # A changed input never overwrites the old cached evidence.
            raise ValueError('Stale map cache retained. Rescan after changing evidence; use a fresh --state directory for reevaluation')
        folder.mkdir(parents=True)
        np.save(folder/'nominal_seconds.npy', nominal, allow_pickle=False)
        np.save(folder/'speed_error.npy', speed, allow_pickle=False)
        record = dict(status='complete', map_only=True, experimental_transfer=diagnostic, methodology=METHODOLOGY,
                      tape=item['tape'], channel=row['channel'], plan_sha256=digest(path), frames=frames,
                      start_seconds=start, sample_rate=row['sample_rate'], timing=stats, markers=markers,
                      joined_map_checks=checks, evidence=evidence, audio_provenance=audio_binding,
                      bindings=bindings+[audio_binding],
                      optional_inputs=optional_inputs,
                      map_plan=bound(map_plan_path), pilot_only=not bool(source_plan.get('full_tape')),
                      timing_artifacts={name:file_hash(folder/name) for name in ('nominal_seconds.npy', 'speed_error.npy')})
        immutable_json(folder/'review.json', record)
        if not diagnostic:
            item['map_check_status'] = checks['status']
            item['required_conditions'] = sorted(set(item['required_conditions']) | {tag for marker in markers for kind,tag in (('fade','fade'),('support_gap','dropout')) if marker['kind'] == kind})
        return folder/'review.json'

    def pilot_map_plan(self, source_path, plan):
        """Embed pilots in a full-channel map with explicit identity elsewhere.

        This is a full-duration integration, never an independently anchored
        excerpt. The derived plan is review-only and cannot pass production.
        """
        folder = self.state/'pilot_plans'/digest(source_path)
        path = folder/'plan.json'
        if path.exists():
            return path
        folder.mkdir(parents=True,exist_ok=True)
        if is_continuous_plan(plan):
            return self.continuous_pilot_plan(source_path, plan, folder)
        regions = []
        for summary in plan['regions']:
            _, raw = checked_artifact(source_path.parent, summary['detail_file'], summary['detail_sha256'])
            detail = json.loads(raw)
            if detail.get('maps'):
                old = source_path.parent/detail['maps']['file']
                copied = 'pilot_'+Path(detail['maps']['file']).name
                shutil.copyfile(old,folder/copied)
                detail['maps'] = dict(detail['maps'],file=copied)
            name = 'pilot_'+Path(summary['detail_file']).name
            immutable_json(folder/name,detail)
            regions.append(dict(detail,detail_file=name,detail_sha256=digest(folder/name)))
        start = min(r['start_seconds'] for r in plan['inventory']['channels'])
        end = max(r['end_seconds'] for r in plan['inventory']['channels'])
        edges = sorted({start,end} | {r[k] for r in regions for k in ('start_seconds','end_seconds')})
        for i,(a,b) in enumerate(zip(edges,edges[1:])):
            if any(r['start_seconds'] <= a and r['end_seconds'] >= b for r in regions):
                continue
            name = f'pilot_identity_{i:05d}'
            targets = [dict(channel=r['channel'],action='leave_uncorrected',selected=None,
                            reason='Outside bounded pilot plan: explicit identity bypass, no correction accepted')
                       for r in plan['inventory']['channels'] if r['role'] != 'timing_only']
            detail = dict(id=name,start_seconds=a,end_seconds=b,analysis_start=a,analysis_end=b,
                          status='analyzed',maps={},targets=targets)
            immutable_json(folder/(name+'.json'),detail)
            regions.append(dict(detail,detail_file=name+'.json',detail_sha256=digest(folder/(name+'.json'))))
        derived = dict(plan,full_tape=True,review_only=True,pilot_source_plan=bound(source_path),
                       regions=sorted(regions,key=lambda r:r['start_seconds']))
        immutable_json(path,derived)
        return path

    def continuous_pilot_plan(self, source_path, plan, folder):
        """Keep a bounded stream's coordinates in a review-only full-tape map."""
        start = min(r['start_seconds'] for r in plan['inventory']['channels'])
        end = max(r['end_seconds'] for r in plan['inventory']['channels'])
        regions = []
        for summary in plan['regions']:
            _, raw = checked_artifact(source_path.parent, summary['detail_file'], summary['detail_sha256'])
            detail = json.loads(raw)
            artifacts = list(detail.get('maps', {}).get('streams', {}).values())+detail.get('monitor_records', [])
            for stream in artifacts:
                old = self.guard.check(source_path.parent/stream['file'])
                if source_path.parent.resolve() not in old.parents or file_hash(old) != stream['sha256']:
                    raise ValueError('Continuous pilot evidence changed')
                name = 'pilot_'+fingerprint(stream['file'])[:12]+'_'+old.name
                shutil.copyfile(old, folder/name)
                stream['file'] = name
            for target in detail['targets']:
                spans = target.get('reference_spans', [])
                prefix = ([dict(start_seconds=start, end_seconds=detail['start_seconds'],
                                action='leave_uncorrected', reason='Outside bounded pilot: explicit identity')]
                          if start < detail['start_seconds'] else [])
                suffix = ([dict(start_seconds=detail['end_seconds'], end_seconds=end,
                                action='leave_uncorrected', reason='Outside bounded pilot: explicit identity')]
                          if detail['end_seconds'] < end else [])
                target['reference_spans'] = prefix+spans+suffix
            detail.update(start_seconds=start, end_seconds=end, analysis_start=start, analysis_end=end)
            name = 'pilot_'+Path(summary['detail_file']).name
            immutable_json(folder/name, detail)
            regions.append(dict(detail, detail_file=name, detail_sha256=digest(folder/name)))
        derived = dict(plan, full_tape=True, review_only=True, pilot_source_plan=bound(source_path), regions=regions)
        immutable_json(folder/'plan.json', derived)
        return folder/'plan.json'

    def events(self, queue_id=None):
        return [dict(read_json(p), event_path=str(p.resolve())) for p in sorted((self.state/'events').glob('*.json'))
                if queue_id is None or read_json(p)['queue_id'] == queue_id]

    def queue(self, queue_id):
        if not re.fullmatch(r'[a-f0-9]{32}', queue_id):
            raise ValueError('Invalid queue identity')
        return read_json(self.state/'queues'/queue_id/'queue.json')

    def candidate_activity(self, candidate):
        item = self.item(candidate['tape'], candidate['channel'])
        path = self.guard.check(Path(item['denoised'])/'denoised.wav')
        stat = path.stat()
        key = (str(path), stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns,
               candidate['start_frame'], candidate['frames'])
        if key not in self.activity_cache:
            with sf.SoundFile(path) as stream:
                stream.seek(candidate['start_frame'])
                samples = stream.read(candidate['frames'], dtype='float64')
                if len(samples) != candidate['frames'] or not np.isfinite(samples).all():
                    raise ValueError('Invalid activity-screen input')
                self.activity_cache[key] = audio_activity(samples, stream.samplerate)
        return self.activity_cache[key]

    def selection(self, options, followup=None):
        kind = options.get('kind', 'methodology')
        if kind not in ('diagnostic', 'methodology', 'production'):
            raise ValueError('Choose diagnostic pilot listening, methodology or production')
        diagnostic = kind == 'diagnostic'
        tapes = options.get('tapes') or [t['tape'] for t in self.catalog['tapes']]
        tape_random = options.get('tape_random') in (True, 'true', '1')
        if tape_random and (len(tapes) != 1 or kind == 'methodology' or followup):
            raise ValueError('Random tape spot checks require one tape and pilot or production review')
        candidates = [dict(c) for c in self.catalog['candidates'] if c['tape'] in tapes]
        if diagnostic:
            candidates = [c for c in candidates if not self.item(c['tape'], c['channel'])['full_tape']]
        seconds = float(options.get('seconds', 20))
        budget = int(options.get('budget', 30))
        # Validate bounds before allocating or reading any requested audio.
        select_samples([], budget, seconds, tape_random=tape_random)
        for c in candidates:
            # Preserve center as duration changes; clamp to channel boundaries.
            item = self.item(c['tape'], c['channel'])
            frames = round(seconds*c['sample_rate'])
            c['start_frame'] = max(0, min(item['source']['frames']-frames, c['start_frame']+(c['frames']-frames)//2))
            c['frames'] = frames
            c['start_seconds'] = item['source']['start_seconds']+c['start_frame']/c['sample_rate']
            c['id'] = fingerprint([c['id'], c['start_frame'], frames])[:24]
            if diagnostic:
                c['id'] = fingerprint([c['id'], 'diagnostic-pilot'])[:24]
            overlaps = [r for r in item['intervals'] if r['start_seconds'] < c['start_seconds']+seconds and r['end_seconds'] > c['start_seconds']]
            c['cohorts'] = sorted({r['cohort'] for r in overlaps})
        required = set(self.catalog['required_conditions']) if options.get('kind', 'methodology') == 'methodology' else set()
        for item in self.catalog['channels']:
            if item['tape'] in tapes and not item['blockers']:
                required.update(item['required_conditions'])
        if tape_random:
            required = {'tape:'+tapes[0]}
        prior = []
        if followup:
            parent = self.queue(followup)
            failures = assess(parent, self.events(followup))['failed_cohorts']
            candidates = [c for c in candidates if c['cohort'] in failures]
            required = {'cohort:'+c for c in failures}
            prior = [p for q in (self.state/'queues').glob('*/queue.json') for s in read_json(q)['samples'] for p in s['provenance']]
        # Screen the exact requested coordinates before risk/coverage selection.
        # Quiet boundaries stay uncovered; never move them or the correction map.
        usable, skipped = [], []
        for c in candidates:
            try:
                c['activity'] = self.candidate_activity(c)
                if c['activity']['usable']:
                    usable.append(c)
                    continue
                reason = c['activity']['status']
            except (OSError, ValueError, RuntimeError) as exc:
                reason = str(exc)
            skipped.append(dict(tape=c['tape'], channel=c['channel'], start_seconds=c['start_seconds'],
                                reason=reason, activity=c.get('activity')))
        candidates = usable
        # Exact audio identity dedup is available from a previous bounded render.
        for c in candidates:
            cached = self.state/'excerpt_identity'/f'{c["id"]}.json'
            if cached.exists():
                record = read_json(cached)
                if record['plan_sha256'] == c['plan_sha256']:
                    c['comparison_sha256'] = record.get('comparison_sha256')
        result = select_samples(candidates, budget, seconds, options.get('seed', 15024), required, prior, tape_random=tape_random)
        result.update(tapes=tapes, kind=kind, diagnostic_only=diagnostic, followup_of=followup,
                      activity_screened_out=skipped)
        return result

    def excerpt(self, sample, review_path, folder):
        """Invert the full production map at clip times, with the production halo."""
        item = self.item(sample['tape'], sample['channel'])
        review = read_json(review_path)
        sr, begin, frames = sample['sample_rate'], sample['start_frame'], sample['frames']
        nominal = np.load(review_path.parent/'nominal_seconds.npy', mmap_mode='r', allow_pickle=False)
        # Same nonuniform final grid sample as continuous_map/render_warp.
        times = np.arange(begin, begin+frames)/sr
        lo = max(0, int(np.searchsorted(nominal, times[0]))-1)
        hi = min(len(nominal), int(np.searchsorted(nominal, times[-1]))+2)
        grid = np.arange(lo, hi)/200.
        if hi == len(nominal):
            grid[-1] = (item['source']['frames']-1)/sr
        pos = np.interp(times, nominal[lo:hi], grid)*sr
        left, right = max(0, int(np.floor(pos[0]))-256), min(item['source']['frames'], int(np.ceil(pos[-1]))+257)
        with sf.SoundFile(Path(item['denoised'])/'denoised.wav') as stream:
            stream.seek(begin); a = stream.read(frames, dtype='float64')
            stream.seek(left); x = stream.read(right-left, dtype='float64')
        b = x[begin-left:begin-left+frames] if np.max(abs(pos-np.arange(begin,begin+frames))) < 1e-7 else resample_at(x, pos-left)
        if len(a) != frames or len(b) != frames or not np.isfinite(a).all() or not np.isfinite(b).all():
            raise ValueError('Invalid bounded audio excerpt')
        # Equal RMS at an audible review level, with bounded boost and joint peak limiting.
        rms_a, rms_b = float(np.sqrt(np.mean(a*a))), float(np.sqrt(np.mean(b*b)))
        gain_b = rms_a/rms_b if min(rms_a, rms_b) > 1e-9 else 1.
        common = min(10**(30/20), 10**(-24/20)/max(rms_a, 1e-9),
                     .95/max(float(np.max(abs(a))), float(np.max(abs(b)))*gain_b, 1e-9))
        folder.mkdir(parents=True, exist_ok=True)
        sf.write(folder/'a.wav', a*common, sr, subtype='PCM_24')
        sf.write(folder/'b.wav', b*gain_b*common, sr, subtype='PCM_24')
        # Test the actual encoded playback files, including matched playback gain.
        playback_a, _ = sf.read(folder/'a.wav', dtype='float64')
        playback_b, _ = sf.read(folder/'b.wav', dtype='float64')
        speed = np.load(review_path.parent/'speed_error.npy', mmap_mode='r', allow_pickle=False)
        metadata = dict(sample_id=sample['id'], source_audio=review['audio_provenance'], continuous_review=bound(review_path),
                        diagnostic_only=review.get('experimental_transfer', False),
                        start_frame=begin, frames=frames, source_start_seconds=sample['start_seconds'],
                        content_sha256=hashlib.sha256(a.tobytes()).hexdigest(), gain_a=common, gain_b=common*gain_b,
                        denoised_rms_dbfs=20*math.log10(max(rms_a,1e-12)), faint_level_candidate=rms_a < .01,
                        playback_rms_dbfs=20*math.log10(max(rms_a*common,1e-12)),
                        strong_correction=bool(np.max(abs(speed[max(0,lo):hi])) >= .005),
                        map_conditions=sorted({tag for m in review['markers'] for kind,tag in (('fade','fade'),('support_gap','dropout'))
                                               if m['kind'] == kind and m['start_seconds'] < (begin+frames)/sr and m['end_seconds'] > begin/sr}),
                        matching='Equal RMS targeting -24 dBFS, boost capped at 30 dB, joint peak limiting; same stage-03 source; full-channel inverse map',
                        comparison_check=comparison_change(playback_a, playback_b),
                        map_seconds=(times[::max(1, len(times)//250)]-times[0]).tolist(),
                        displacement_ms=((pos[::max(1, len(times)//250)]/sr-times[::max(1, len(times)//250)])*1000).tolist(),
                        speed_error=np.asarray(speed[max(0,lo):hi:max(1,(hi-lo)//250)]).tolist(),
                        audio_hashes={name:file_hash(folder/name) for name in ('a.wav','b.wav')})
        immutable_json(folder/'excerpt.json', metadata)
        ident = self.state/'excerpt_identity'/f'{sample["id"]}.json'
        if not ident.exists():
            immutable_json(ident, dict(plan_sha256=sample['plan_sha256'], comparison_sha256=fingerprint(metadata['audio_hashes'])))
        return metadata

    def create_queue(self, options, progress, followup=None):
        selection = self.selection(options, followup)
        if not selection['samples']:
            raise ValueError('No active review candidates. Choose another pilot passage or channel with speech and prepared stage-03 audio; diagnostic listening requires a compatible bounded pilot')
        queue_id = uuid.uuid4().hex
        folder = self.state/'queues'/queue_id
        selection.update(id=queue_id, created_at=now(), methodology=METHODOLOGY,
                         implementation_sha256=implementation_digest(), numeric_policy_sha256=policy_digest(),
                         sampling_policy_sha256=policy_hash(), demo=self.demo, bindings=[],
                         blinded=bool(options.get('blinded', True)), label_order={})
        # Save failure/progress record even if a later clip is blocked.
        immutable_json(folder/'selection.json', selection)
        binding_index = {}
        exact_content = {}
        unique = []
        for i, sample in enumerate(selection['samples']):
            progress(f'Comparison {i+1}/{len(selection["samples"])}: {sample["tape"]} CH{sample["channel"]}')
            item = self.item(sample['tape'], sample['channel'])
            try:
                review_path = self.build_map(item, progress, diagnostic=selection['diagnostic_only'])
                metadata = self.excerpt(sample, review_path, folder/sample['id'])
                sample['conditions'] = sorted(set(sample['conditions']) | set(metadata['map_conditions']))
                if metadata['strong_correction']:
                    sample['conditions'] = sorted(set(sample['conditions']) | {'strong_correction'})
                key = fingerprint(metadata['audio_hashes'])
                for b in read_json(review_path)['bindings']+[bound(review_path), bound(folder/sample['id']/'excerpt.json')]:
                    binding_index[b['path']] = b
                for name in ('a.wav','b.wav'):
                    b = bound(folder/sample['id']/name); binding_index[b['path']] = b
                if key in exact_content:
                    # Safe exact duplicates merge listening work, retaining every coordinate.
                    original = exact_content[key]
                    original['provenance'].extend(sample['provenance'])
                    original['conditions'] = sorted(set(original['conditions']) | set(sample['conditions']))
                    original['selection_reason'] += '; exact duplicate provenance merged'
                    continue
                sample['excerpt'] = metadata
                sample['ready'] = True
            except (ValueError, OSError, KeyError, RuntimeError) as exc:
                sample.update(ready=False, error=str(exc))
            exact_content[key if sample.get('ready') else sample['id']] = sample
            unique.append(sample)
            selection['label_order'][sample['id']] = secrets.randbelow(2)
        selection['samples'] = unique
        selection['comparisons'] = len(unique)
        selection['estimated_listening_seconds'] = sum(s['frames']/s['sample_rate']*2 for s in unique if not automatic_no_change(s))
        selection['automated_unchanged'] = sum(automatic_no_change(s) for s in unique)
        selection['bindings'] = list(binding_index.values())
        selection['covered_conditions'] = sorted({c for s in unique if s.get('ready') for c in s['conditions']})
        selection['uncovered_conditions'] = sorted(set(selection['required_conditions'])-set(selection['covered_conditions']))
        selection['random_selected'] = sum(s['selection_reason'].startswith('Reproducible') for s in unique if s.get('ready'))
        selection['unavailable_ordinary_random'] = selection['random_selected'] < selection['random_required']
        immutable_json(folder/'queue.json', selection)
        return dict(queue_id=queue_id, comparisons=len(unique), blocked=sum(not s.get('ready') for s in unique))

    def judge(self, payload):
        reviewer = str(payload.get('reviewer','')).strip()
        if not reviewer:
            raise ValueError('Reviewer name is required')
        queue = self.queue(payload['queue_id'])
        sample = next(s for s in queue['samples'] if s['id'] == payload['sample_id'])
        if not sample.get('ready'):
            raise ValueError('A blocked comparison cannot be judged')
        # A finding describes the saved audio the reviewer actually heard.
        # Verify those bounded files now; full source/survey evidence is rehashed
        # by preview/acceptance, not while holding the UI lock for every save.
        excerpt_paths = {(self.state/'queues'/queue['id']/sample['id']/name).resolve()
                         for name in ('a.wav', 'b.wav', 'excerpt.json')}
        excerpt_bindings = [b for b in queue['bindings'] if Path(b['path']) in excerpt_paths]
        if {Path(b['path']) for b in excerpt_bindings} != excerpt_paths:
            raise ValueError('Selected excerpt is missing its evidence bindings')
        self.verify(excerpt_bindings, force=True)
        judgment = payload['judgment']
        if judgment not in ('improved','unchanged','worse','uncertain'):
            raise ValueError('Unknown judgment')
        flags = {key:bool(payload.get('flags', {}).get(key)) for key in FLAGS}
        event = dict(id=uuid.uuid4().hex, queue_id=queue['id'], sample_id=sample['id'], reviewer=reviewer,
                     diagnostic_only=queue.get('diagnostic_only', False),
                     reviewed_at=now(), judgment=judgment, flags=flags, notes=str(payload.get('notes',''))[:10000],
                     coordinates=sample['provenance'], cohorts=sorted({c for p in sample['provenance'] for c in p.get('cohorts',[p['cohort']])}),
                     implementation_sha256=queue['implementation_sha256'], policy_version=POLICY,
                     confirmed_conditions=['faint_speech'] if payload.get('faint_speech_confirmed') else [],
                     queue_sha256=digest(self.state/'queues'/queue['id']/'queue.json'), demo=self.demo)
        if not sample['eligible'] and adverse(event):
            # An adverse bypass/transition can implicate adjacent corrections.
            event['cohorts'] = sorted(set(event['cohorts']) | set(self.item(sample['tape'],sample['channel'])['cohorts']))
        immutable_json(self.state/'events'/f'{event["reviewed_at"].replace(":", "-")}_{event["id"]}.json', event)
        self.preview_cache.clear()
        return event

    def validate_scope(self, tapes, progress):
        items = [c for c in self.catalog['channels'] if c['tape'] in tapes]
        errors = []
        for i, item in enumerate(items):
            progress(f'Full-channel maps {i+1}/{len(items)}; no full audio render')
            try:
                self.build_map(item, progress)
            except (ValueError, OSError, KeyError, RuntimeError) as exc:
                errors.append(dict(tape=item['tape'], channel=item['channel'], error=str(exc)))
        return dict(channels=len(items), errors=errors)

    def current_events(self, queue):
        return [e for e in self.events() if e['implementation_sha256'] == queue['implementation_sha256']]

    def queue_updates(self, queue_id):
        """Small live finding updates without resending source/survey bindings."""
        queue = self.queue(queue_id)
        events = self.events(queue_id)
        judged = {e['sample_id'] for e in events}
        labels = {}
        for sample in queue['samples']:
            order = queue['label_order'][sample['id']] if queue['blinded'] else 0
            labels[sample['id']] = ((['Corrected','Denoised'] if order else ['Denoised','Corrected'])
                                    if sample['id'] in judged or not queue['blinded'] else ['Version A','Version B'])
        return dict(id=queue_id, events=events, labels=labels,
                    findings=assess(queue, events, self.current_events(queue)))

    def preview(self, payload):
        queue = self.queue(payload['queue_id'])
        if queue.get('diagnostic_only') or queue['kind'] == 'diagnostic':
            raise ValueError('Diagnostic pilot listening cannot authorize methodology or production. Use fresh qualified evidence for acceptance')
        kind = payload.get('kind', queue['kind'])
        if queue.get('tape_random') and kind != 'production':
            raise ValueError('A tape spot check cannot qualify the methodology')
        reviewer = str(payload.get('reviewer','')).strip()
        errors = []
        if not reviewer:
            errors.append('Reviewer identity is required')
        if (queue['implementation_sha256'] != implementation_digest() or queue['numeric_policy_sha256'] != policy_digest()
                or queue['sampling_policy_sha256'] != policy_hash()):
            errors.append('Method or policy changed; fresh evaluation is required')
        try:
            self.verify(queue['bindings'], force=True)
        except (ValueError, OSError) as exc:
            errors.append(str(exc))
        events = self.events(queue['id'])
        coverage = effective_coverage(queue,events)
        historical = self.current_events(queue)
        findings = assess(queue, events, historical)
        try:
            validate_tape_spot_check(queue)
        except ValueError as exc:
            errors.append(str(exc))
        if findings['pending']:
            errors.append(f'{len(findings["pending"])} comparisons lack judgments')
        if any(not s.get('ready') for s in queue['samples']):
            errors.append('Queue contains blocked audio comparisons')
        if queue['unavailable_ordinary_random']:
            errors.append('Fewer than 3 usable random tape excerpts are available' if queue.get('tape_random') else 'Ordinary random sample quota could not be filled')
        # Fresh evaluation cannot reuse material reviewed under an earlier implementation.
        coords = {(p['tape'], p['channel'], p['start_frame'], p['frames']) for s in queue['samples'] for p in s['provenance']}
        for event in self.events():
            if event['implementation_sha256'] != queue['implementation_sha256'] or event.get('diagnostic_only'):
                for p in event['coordinates']:
                    if any(t == p['tape'] and ch == p['channel'] and a < p['start_frame']+p['frames'] and p['start_frame'] < a+n for t,ch,a,n in coords):
                        errors.append('Fresh evaluation overlaps diagnostic pilot listening' if event.get('diagnostic_only') else
                                      'Fresh evaluation overlaps a holdout reviewed under an earlier method')
                        break
        scope = []
        for item in self.catalog['channels']:
            if item['tape'] not in queue['tapes']:
                continue
            blockers = list(item['blockers'])
            if kind == 'production':
                blockers += item['production_blockers']
            requirements = listening_requirements(queue, item)
            missing = set(requirements)-coverage
            if missing:
                blockers.append('Uncovered conditions: '+', '.join(sorted(missing)))
            if set(item['cohorts']) & set(findings['failed_cohorts']):
                blockers.append('Retained adverse listening finding affects this cohort')
            review = self.map_cached(item)
            if not review:
                blockers.append('Full-channel map evidence missing or stale; validate cohort maps')
            elif review['joined_map_checks']['status'] != 'passed':
                blockers.append('Final joined-map checks: '+review['joined_map_checks']['status'])
            scope.append(dict(tape=item['tape'], channel=item['channel'], plan_path=item['plan_path'],
                              plan_sha256=item['plan_sha256'], cohorts=item['cohorts'],
                              required_conditions=requirements, accepted=not blockers, blockers=blockers,
                              continuous_review=bound(self.map_directory(item)/'review.json') if review else None,
                              intervals=item['intervals'], exclusions=[v for v in item['intervals'] if not v['eligible']]))
        if kind not in ('methodology','production'):
            raise ValueError('Choose methodology or production acceptance')
        evidence = {}
        if kind == 'methodology':
            for sample in queue['samples']:
                for provenance in sample['provenance']:
                    reviewed_item = self.item(provenance['tape'],provenance['channel'])
                    mapped = self.map_cached(reviewed_item)
                    if reviewed_item['blockers'] or not mapped or mapped['joined_map_checks']['status'] != 'passed':
                        errors.append(f'{reviewed_item["tape"]} CH{reviewed_item["channel"]}: sampled plan lacks passed automated evidence')
            if findings['failed_cohorts']:
                errors.append('Methodology has retained failed/uncertain listening holdouts')
            uncovered = sorted(set(queue['required_conditions'])-coverage)
            if uncovered:
                errors.append('Methodology coverage incomplete: '+', '.join(uncovered))
            for name in ('carrier_qualification','fresh_holdouts'):
                try:
                    ref = self.config.get('methodology_evidence', {})[name]
                    p = self.guard.check(ref['path'])
                    if digest(p) != ref['sha256'] or read_json(p).get('status') != 'accepted':
                        raise ValueError('Hash-bound report must explicitly be accepted')
                    report = read_json(p)
                    if not self.demo and (report.get('implementation_sha256') != implementation_digest() or report.get('policy_sha256') != policy_digest()):
                        raise ValueError('Report must identify the current frozen implementation and numeric policy')
                    evidence[name] = bound(p)
                except (KeyError, ValueError, OSError) as exc:
                    errors.append(name+': '+str(exc))
        else:
            try:
                rollout = self.guard.check(self.config.get('rollout_approval') or self.state/'methodology'/'rollout_approval.json')
                if self.demo:
                    # Exercise the UI lifecycle while keeping demo records rejected by CLI gates.
                    value = read_json(rollout)
                    if not value.get('demo') or value.get('status') != 'accepted':
                        raise ValueError('Synthetic methodology must be accepted first')
                else:
                    require_rollout(rollout)
                evidence['rollout_approval'] = bound(rollout)
            except (ValueError, OSError, KeyError, TypeError) as exc:
                errors.append('Methodology acceptance required: '+str(exc))
        if not any(s['accepted'] for s in scope):
            errors.append('No channels have sufficient eligible evidence and sampling coverage')
        preview = dict(id=uuid.uuid4().hex, kind=kind, reviewer=reviewer, created_at=now(), queue_id=queue['id'],
                       queue=bound(self.state/'queues'/queue['id']/'queue.json'), status='ready' if not errors else 'blocked',
                       errors=sorted(set(errors)), scope=scope, findings=findings, evidence=evidence,
                       uncovered_conditions=sorted(set(queue['required_conditions'])-coverage), demo=self.demo,
                       summary=dict(accepted_channels=sum(s['accepted'] for s in scope), excluded_channels=sum(not s['accepted'] for s in scope),
                                    accepted_interval_seconds=sum(max(0,min(i['end_seconds'],r['end_seconds'])-max(i['start_seconds'],r['start_seconds']))
                                        for s in scope if s['accepted'] for i in s['intervals'] if i['eligible']
                                        for r in i['support_spans'] if r['action'] == 'use_reference')))
        self.preview_cache[preview['id']] = dict(payload=payload, preview=preview, event_digest=fingerprint(self.events()))
        return preview

    def accept(self, preview_id):
        cached = self.preview_cache.get(preview_id)
        if not cached:
            raise ValueError('Preview expired; review the approval preview again')
        # Recompute immediately to close stale-evidence and later-judgment races.
        fresh = self.preview(cached['payload'])
        expected, actual = dict(cached['preview']), dict(fresh)
        for v in (expected, actual):
            v.pop('id'); v.pop('created_at')
        if expected != actual or fingerprint(self.events()) != cached['event_digest']:
            raise ValueError('Evidence or decisions changed since preview; preview again')
        if fresh['status'] != 'ready':
            raise ValueError('Approval is blocked: '+'; '.join(fresh['errors']))
        queue = self.queue(fresh['queue_id'])
        accepted = [s for s in fresh['scope'] if s['accepted']]
        bindings = {r['path']:r for r in queue['bindings']}
        for s in accepted:
            review = read_json(s['continuous_review']['path'])
            for r in review['bindings']+[s['continuous_review']]:
                bindings[r['path']] = r
            validate_map_review(s['continuous_review']['path'], s['plan_path'], s['channel'])
            # Check original/survey freshness in addition to recorded file hashes.
            accepted_plan = self.safe_plan(s['plan_path'])
            if not is_continuous_plan(accepted_plan):
                self.safe_survey(Path(self.config['surveys'])/s['tape'])
        cert = dict(status='accepted', kind=fresh['kind'], policy_version=POLICY,
                    sampling_policy_sha256=policy_hash(), methodology=METHODOLOGY,
                    implementation_sha256=implementation_digest(), policy_sha256=policy_digest(),
                    reviewer=fresh['reviewer'], reviewed_at=now(), demo=self.demo, queue=fresh['queue'],
                    seed=queue['seed'], scope=fresh['scope'], exclusions=[s for s in fresh['scope'] if not s['accepted']],
                    bindings=list(bindings.values()), events=[bound(e['event_path']) for e in self.events(queue['id'])],
                    ledger_root=str((self.state/'events').resolve()), findings=fresh['findings'], evidence=fresh['evidence'])
        certificate_path = self.state/'decisions'/f'{uuid.uuid4().hex}.json'
        immutable_json(certificate_path, cert)
        if fresh['kind'] == 'methodology':
            target = self.state/'methodology'/certificate_path.stem
            listening_path = target/'speech_listening.json'
            immutable_json(listening_path, dict(status='accepted', reviewer=cert['reviewer'], reviewed_at=cert['reviewed_at'],
                                               sampling_certificate=bound(certificate_path), demo=self.demo))
            rollout = dict(status='accepted', methodology=METHODOLOGY, implementation_sha256=implementation_digest(),
                           policy_sha256=policy_digest(), reviewer=cert['reviewer'], reviewed_at=cert['reviewed_at'],
                           speech_listening=bound(listening_path), demo=self.demo, **fresh['evidence'])
            immutable_json(target/'rollout_approval.json', rollout)
            pointer = self.state/'methodology'/'rollout_approval.json'
            temporary = pointer.with_suffix('.partial')
            temporary.write_text(json.dumps(rollout,indent=2),encoding='utf-8'); temporary.replace(pointer)
        else:
            for tape in sorted({s['tape'] for s in accepted}):
                target = self.state/'approvals'/tape/'production_acceptance.json'
                # Version history survives an explicit replacement of the CLI pointer.
                payload = dict(status='accepted', acceptance_policy=POLICY, sampling_certificate=bound(certificate_path),
                               rollout_approval=fresh['evidence']['rollout_approval']['path'],
                               plan_sha256=next(s['plan_sha256'] for s in accepted if s['tape'] == tape),
                               channels=[dict(channel=s['channel'], continuous_review=s['continuous_review']) for s in accepted if s['tape'] == tape])
                target.parent.mkdir(parents=True, exist_ok=True)
                temp = target.with_suffix('.partial')
                temp.write_text(json.dumps(payload, indent=2), encoding='utf-8'); temp.replace(target)
        self.preview_cache.clear()
        return dict(certificate=str(certificate_path), kind=fresh['kind'], channels=len(accepted),
                    production_launched=False, demo=self.demo)

    def public_queue(self, queue_id):
        queue = self.queue(queue_id)
        events = self.events(queue_id)
        judged = {e['sample_id'] for e in events}
        # Keep the blind mapping server-side until a judgment is recorded.
        result = dict(queue)
        result.pop('label_order')
        result['events'] = events
        result['findings'] = assess(queue, events, self.current_events(queue))
        for s in result['samples']:
            if s.get('ready') and not s.get('excerpt', {}).get('comparison_check'):
                # Add a cached measurement to old saved sets without rewriting their evidence.
                paths = [self.state/'queues'/queue_id/s['id']/name for name in ('a.wav','b.wav')]
                key = tuple((str(p), p.stat().st_size, p.stat().st_mtime_ns) for p in paths)
                if key not in self.comparison_cache:
                    a, _ = sf.read(paths[0], dtype='float64'); b, _ = sf.read(paths[1], dtype='float64')
                    self.comparison_cache[key] = comparison_change(a, b)
                s['comparison_measurement'] = self.comparison_cache[key]
            order = queue['label_order'][s['id']] if queue['blinded'] else 0
            s['audio_urls'] = [f'/audio/{queue_id}/{s["id"]}/{n}.wav' for n in (0,1)]
            s['labels'] = (['Corrected','Denoised'] if order else ['Denoised','Corrected']) if s['id'] in judged or not queue['blinded'] else ['Version A','Version B']
        return result

    def overview(self):
        with self.lock:
            result = {k:v for k,v in self.catalog.items() if k not in ('candidates','channels')}
            result['tapes'] = [dict(t, map_checks_passed=sum(c.get('map_check_status') == 'passed' for c in self.catalog['channels'] if c['tape'] == t['tape']),
                                    final_map_failures=[dict(channel=c['channel'], status=c.get('map_check_status')) for c in self.catalog['channels']
                                                        if c['tape'] == t['tape'] and c.get('map_check_status') not in (None,'passed','missing')]) for t in result['tapes']]
        result.update(jobs=list(self.jobs.values())[-12:], token=self.token, roots={k:str(self.config[k]) for k in ('originals','surveys','plans','denoised','state')},
                      queues=[dict(id=q.parent.name, created_at=read_json(q)['created_at'], comparisons=read_json(q)['comparisons'])
                              for q in sorted((self.state/'queues').glob('*/queue.json'), key=lambda p:p.stat().st_mtime, reverse=True)])
        return result
