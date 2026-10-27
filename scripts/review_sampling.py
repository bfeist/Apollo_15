"""Explicit sampling acceptance contract shared by the local UI and CLI gates.

Sampling approves listening for a recorded cohort. It never promotes a numeric
proposal, changes a map, or fits content lag. Artifacts and decisions are immutable.
"""
import hashlib
import json
import math
from pathlib import Path
import random

import numpy as np
from scipy import signal

from flutter_policy import METHODOLOGY, digest, implementation_digest, policy_digest

POLICY = 'representative-listening-v1'
PILOTS = ('A15_T510083', 'A15_T510077', 'A15_T510073', 'A15_T510092', 'A15_T510076')
CONDITIONS = ('early', 'middle', 'late', 'faint_speech', 'strong_correction',
              'reference_change', 'fade', 'dropout', 'no_beep_group')
FLAGS = ('intelligibility_loss', 'new_artifacts', 'unstable_pitch', 'transition_problems')
ELIGIBLE = {'render_and_review': 'direct_numeric', 'group_render_and_review': 'transfer_group'}


def comparison_change(a, b):
    """Compare the exact playback samples, without lag fitting or an audibility claim."""
    a, b = np.asarray(a, dtype=np.float64), np.asarray(b, dtype=np.float64)
    if a.ndim != 1 or a.shape != b.shape or not a.size or not np.isfinite(a).all() or not np.isfinite(b).all():
        raise ValueError('Comparison requires equal, nonempty, finite mono excerpts')
    delta = b-a
    rms = float(np.sqrt(np.mean(delta*delta)))
    reference = float(np.sqrt(np.mean(a*a)))
    identical = bool(np.array_equal(a, b))
    return dict(version='exact-playback-comparison-v1', status='identical' if identical else 'changed',
                human_review_required=not identical, rms_difference=rms,
                peak_difference=float(np.max(abs(delta))),
                difference_dbfs=20*math.log10(rms) if rms else None,
                difference_relative_db=20*math.log10(rms/reference) if rms and reference else None,
                audible_benefit='not_assessed',
                reason='Playback samples are identical; listening is unnecessary' if identical else
                       'Playback samples differ; waveform difference alone does not establish audibility or improvement')


def automatic_no_change(sample):
    """Only exact, hash-bound equality can replace a listening task automatically."""
    excerpt = sample.get('excerpt', {})
    check = excerpt.get('comparison_check', {})
    hashes = excerpt.get('audio_hashes', {})
    return bool(sample.get('ready') and check.get('version') == 'exact-playback-comparison-v1'
                and check.get('status') == 'identical' and check.get('rms_difference') == 0
                and check.get('peak_difference') == 0 and hashes.get('a.wav')
                and hashes.get('a.wav') == hashes.get('b.wav'))


def audio_activity(samples, sample_rate):
    """Cheap activity screen, not speech recognition or human speech evidence.

    Ignore low-frequency hum and require sustained, varying speech-band energy.
    Measure the prepared input only, before hearing or evaluating corrections.
    """
    band = signal.sosfilt(signal.butter(4, [300, min(3000, sample_rate*.45)],
                                      fs=sample_rate, btype='bandpass', output='sos'), samples)
    hop = max(1, round(sample_rate*.25))
    blocks = band[:len(band)//hop*hop].reshape(-1, hop)
    levels = 10*np.log10(np.mean(blocks*blocks, axis=1)+1e-30)
    floor, upper = np.percentile(levels, [20, 90])
    active_seconds = float(np.sum(levels > max(-60., floor+3))*hop/sample_rate)
    rms_dbfs = float(10*np.log10(np.mean(band*band)+1e-30))
    usable = rms_dbfs > -60 and upper-floor >= 3 and active_seconds >= 2
    return dict(usable=bool(usable), speech_band_rms_dbfs=rms_dbfs,
                active_seconds=active_seconds, variation_db=float(upper-floor),
                status='Activity candidate; human speech confirmation pending' if usable else
                       'Insufficient speech-band activity; choose another passage or channel')


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


def policy_hash():
    # Includes server extraction/decision code as well as sampling logic.
    return hashlib.sha256(Path(__file__).read_bytes() + Path(__file__).with_name('review_evidence.py').read_bytes()).hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def immutable_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('x', encoding='utf-8') as stream:
        json.dump(value, stream, indent=2, allow_nan=False)


def bound(path):
    return dict(path=str(Path(path).resolve()), sha256=digest(path))


def verify_bindings(bindings):
    for item in bindings:
        if digest(item['path']) != item['sha256']:
            raise ValueError('Stale evidence: '+item['path'])


def cohort_key(head, reference, method, evidence):
    return '|'.join(str(x or 'unknown') for x in (head, reference, method, evidence))


def select_samples(candidates, budget=30, seconds=20, seed=15024, required=(), prior=(), tape_random=False):
    """Risk quota, weighted set coverage, then a seeded ordinary random sample.

    Duplicates require an exact denoised excerpt hash (or an explicitly supplied
    content identity with independent provenance). Content lag is never consumed.
    """
    if not isinstance(budget, int) or not 1 <= budget <= 100:
        raise ValueError('Listening budget must be 1–100 comparisons')
    if not 15 <= seconds <= 25:
        raise ValueError('Clip length must be 15–25 seconds')
    if tape_random and not 3 <= budget <= 5:
        raise ValueError('Tape spot checks require 3–5 excerpts')
    rng = random.Random(str(seed))
    old = {(c['tape'], c['channel'], c['start_frame'], c['frames']) for c in prior}
    groups = {}
    for original in sorted(candidates, key=lambda c: c['id']):
        if (original['tape'], original['channel'], original['start_frame'], original['frames']) in old:
            continue
        c = dict(original)
        # No fuzzy speech similarity or lag-based grouping is trusted.
        key = c.get('comparison_sha256') or c['id']
        if key in groups:
            groups[key]['provenance'].append(original)
            groups[key]['conditions'] = sorted(set(groups[key]['conditions']) | set(c['conditions']))
            groups[key]['risk'] = max(groups[key]['risk'], c['risk'])
        else:
            c['provenance'] = [original]
            c['conditions'] = sorted(set(c['conditions']))
            groups[key] = c
    pool = list(groups.values())
    rng.shuffle(pool)
    required = set(required)
    covered, selected = set(), []
    random_quota = max(1, budget//5) if budget >= 5 else 0
    risk_quota = max(1, budget//4)

    def take(c, why):
        c = dict(c, selection_reason=why)
        selected.append(c)
        covered.update(c['conditions'])
        pool.remove(next(p for p in pool if p['id'] == c['id']))

    if tape_random:
        # A bounded per-tape listening decision, separate from method qualification.
        # Shuffle the stable, deduplicated speech-active candidate pool uniformly.
        for c in list(pool)[:budget]:
            take(c, 'Reproducible random tape spot check')
        return dict(policy_version=POLICY, seed=str(seed), budget=budget, clip_seconds=seconds,
                    tape_random=True, sampling_design='tape-random-spot-check-v1',
                    candidate_count=len(groups), minimum_comparisons=3,
                    comparisons=len(selected), estimated_listening_seconds=sum(c['frames']/c['sample_rate']*2 for c in selected),
                    random_required=3, random_selected=len(selected), samples=selected,
                    required_conditions=sorted(required), covered_conditions=sorted(covered),
                    uncovered_conditions=sorted(required-covered), unavailable_ordinary_random=len(selected)<3)

    for c in sorted(pool, key=lambda c: -c['risk'])[:min(risk_quota, budget-random_quota)]:
        if c['risk'] > 0:
            take(c, 'Highest-risk exception quota')
    while pool and len(selected) < budget-random_quota:
        best = max(pool, key=lambda c: sum(4 if t.startswith(('tape:', 'cohort:')) else 1
                                          for t in set(c['conditions']) & (required-covered)))
        if not (set(best['conditions']) & (required-covered)):
            break
        take(best, 'Covers '+', '.join(sorted(set(best['conditions']) & (required-covered))))
    ordinary = [c for c in pool if c.get('eligible') and not c['risk']]
    rng.shuffle(ordinary)
    for c in ordinary[:min(random_quota, budget-len(selected))]:
        take(c, 'Reproducible random sample of ordinary eligible regions')
    while pool and len(selected) < budget:
        best = max(pool, key=lambda c: (len(set(c['conditions'])-covered), c['risk']))
        take(best, 'Additional condition coverage' if set(best['conditions'])-covered else 'Seeded supplementary sample')
    return dict(policy_version=POLICY, seed=str(seed), budget=budget, clip_seconds=seconds,
                comparisons=len(selected), estimated_listening_seconds=sum(c['frames']/c['sample_rate']*2 for c in selected),
                random_required=random_quota, random_selected=sum(c['selection_reason'].startswith('Reproducible') for c in selected),
                samples=selected, required_conditions=sorted(required), covered_conditions=sorted(covered),
                uncovered_conditions=sorted(required-covered),
                unavailable_ordinary_random=random_quota > sum(c['selection_reason'].startswith('Reproducible') for c in selected))


def adverse(event):
    return event['judgment'] in ('worse', 'uncertain') or any(event.get('flags', {}).values())


def effective_coverage(queue, events):
    # Only a human can establish that a quiet excerpt actually contains speech.
    # Numeric/group/reference conditions can never be granted by this checkbox.
    return set(queue['covered_conditions']) | {c for e in events for c in e.get('confirmed_conditions', []) if c == 'faint_speech'}


def listening_requirements(queue, item):
    """Spot-check listening covers the tape; numeric checks still cover each channel."""
    if queue.get('tape_random'):
        return ['tape:'+item['tape']]
    return item['required_conditions']


def validate_tape_spot_check(queue):
    if not queue.get('tape_random'):
        return
    if (queue.get('kind') not in ('diagnostic','production') or len(queue.get('tapes',[])) != 1
            or not 3 <= queue.get('budget',0) <= 5 or not 3 <= len(queue['samples']) <= 5
            or queue.get('random_selected',0) < 3
            or any(s['tape'] != queue['tapes'][0] for s in queue['samples'])):
        raise ValueError('Tape spot check needs 3–5 random excerpts from one tape; no extra listening is added automatically')


def assess(queue, events, historical=()):
    latest = {e['sample_id']: e for e in events}
    failed = set()
    for e in list(events)+list(historical):
        if adverse(e):
            failed.update(e['cohorts'])
    automated = [s['id'] for s in queue['samples'] if automatic_no_change(s)]
    pending = [s['id'] for s in queue['samples'] if s['id'] not in latest and s['id'] not in automated]
    return dict(pending=pending, failed_cohorts=sorted(failed),
                automated_unchanged=automated,
                complete=not pending, accepted_findings=not pending and not failed,
                # Failed holdouts survive subsequent edits to a judgment.
                failed_holdouts=[e for e in list(historical)+list(events) if adverse(e)])


def validate_map_review(review_path, plan_path, channel):
    """Same full-channel/final-map contract for sampled and per-channel approval."""
    plan, review = read_json(plan_path), read_json(review_path)
    row = next(r for r in plan['inventory']['channels'] if r['channel'] == channel)
    if (review.get('status') != 'complete' or review.get('experimental_transfer')
            or review.get('plan_sha256') != digest(plan_path) or review.get('channel') != channel
            or review.get('frames') != row['frames'] or review.get('start_seconds') != row['start_seconds']
            or review.get('joined_map_checks', {}).get('status') != 'passed'
            or set(review.get('timing_artifacts', {})) != {'nominal_seconds.npy', 'speed_error.npy'}):
        raise ValueError('Requires a conservative full-channel map with passed reserved final-map checks')
    for name, expected in review['timing_artifacts'].items():
        if digest(Path(review_path).parent/name) != expected:
            raise ValueError('Accepted continuous map artifact changed')
    for optional in review.get('optional_inputs', []):
        if Path(optional['path']).exists() != optional['exists']:
            raise ValueError('Optional clock or passage annotations changed since review')
    return review


def require_sampled(value, approval_path, plan_path, channel):
    """CLI rendering gate; rechecks immutable certificate and the live failure ledger."""
    certificate_ref = value['sampling_certificate']
    certificate_path = (Path(approval_path).parent/certificate_ref['path']).resolve()
    if digest(certificate_path) != certificate_ref['sha256']:
        raise ValueError('Sampling acceptance certificate changed')
    cert = read_json(certificate_path)
    if (cert.get('policy_version') != POLICY or cert.get('sampling_policy_sha256') != policy_hash()
            or cert.get('status') != 'accepted' or cert.get('kind') != 'production'
            or cert.get('demo') or not cert.get('reviewer') or not cert.get('reviewed_at')
            or cert.get('implementation_sha256') != implementation_digest()
            or cert.get('policy_sha256') != policy_digest()):
        raise ValueError('Sampling certificate is incompatible, synthetic, or unapproved')
    verify_bindings(cert['bindings'])
    verify_bindings(list(cert.get('evidence', {}).values()))
    queue = read_json(cert['queue']['path'])
    if digest(cert['queue']['path']) != cert['queue']['sha256']:
        raise ValueError('Sampling queue changed')
    validate_tape_spot_check(queue)
    events = [read_json(r['path']) for r in cert['events']]
    verify_bindings(cert['events'])
    result = assess(queue, events)
    if not result['complete']:
        raise ValueError('Representative listening is incomplete')
    scope = next((s for s in cert['scope'] if s['plan_sha256'] == digest(plan_path) and s['channel'] == channel
                  and Path(s['plan_path']).resolve() == Path(plan_path).resolve()), None)
    if scope is None or not scope['accepted']:
        raise ValueError('Channel is excluded from this sampling acceptance scope')
    if set(scope['cohorts']) & set(result['failed_cohorts']):
        raise ValueError('Sampling scope contains a failed listening cohort')
    if not set(scope['required_conditions']).issubset(effective_coverage(queue,events)):
        raise ValueError('Sampling scope has uncovered conditions')
    if queue['unavailable_ordinary_random']:
        raise ValueError('Ordinary random holdout quota is incomplete')
    # A later bad observation revokes the affected cohort, including old approvals.
    for p in Path(cert['ledger_root']).glob('*.json'):
        event = read_json(p)
        if event.get('implementation_sha256') == cert['implementation_sha256'] and adverse(event):
            if set(scope['cohorts']) & set(event['cohorts']):
                raise ValueError('Later or retained failed holdout invalidates this cohort')
    validate_map_review(scope['continuous_review']['path'], plan_path, channel)
    from flutter_plan_store import load_saved_plan, is_continuous_plan
    from carrier_survey import load_survey
    current_plan = load_saved_plan(plan_path, expand_regions=False)
    if not current_plan.get('full_tape') or current_plan.get('review_only'):
        raise ValueError('Sampling production acceptance requires a complete full-tape plan')
    survey = current_plan['inventory'].get('carrier_survey')
    if not survey and not is_continuous_plan(current_plan):
        raise ValueError('Sampling acceptance requires a bound original carrier survey')
    if survey:
        load_survey(survey['path'])
    return cert


def require_methodology(reference, base):
    path = (Path(base)/reference['path']).resolve()
    if digest(path) != reference['sha256']:
        raise ValueError('Methodology sampling certificate changed')
    cert = read_json(path)
    if (cert.get('demo') or cert.get('status') != 'accepted' or cert.get('kind') != 'methodology'
            or cert.get('policy_version') != POLICY or cert.get('sampling_policy_sha256') != policy_hash()
            or cert.get('implementation_sha256') != implementation_digest()
            or cert.get('policy_sha256') != policy_digest()):
        raise ValueError('Methodology sampling acceptance is stale, synthetic or incompatible')
    verify_bindings(cert['bindings']+[cert['queue']]+cert['events'])
    verify_bindings(list(cert.get('evidence', {}).values()))
    queue = read_json(cert['queue']['path'])
    events = [read_json(e['path']) for e in cert['events']]
    history = [read_json(p) for p in Path(cert['ledger_root']).glob('*.json')]
    history = [e for e in history if e['implementation_sha256'] == cert['implementation_sha256']]
    result = assess(queue, events, history)
    if (not result['accepted_findings'] or set(queue['required_conditions'])-effective_coverage(queue,events)
            or queue['unavailable_ordinary_random']):
        raise ValueError('Methodology listening is incomplete or has retained failed holdouts')
    return cert
