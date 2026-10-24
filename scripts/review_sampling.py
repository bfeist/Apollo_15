"""Explicit sampling acceptance contract shared by the local UI and CLI gates.

Sampling approves listening for a recorded cohort. It never promotes a numeric
proposal, changes a map, or fits content lag. Artifacts and decisions are immutable.
"""
import hashlib
import json
from pathlib import Path
import random

from flutter_policy import METHODOLOGY, digest, implementation_digest, policy_digest

POLICY = 'representative-listening-v1'
PILOTS = ('A15_T510083', 'A15_T510077', 'A15_T510073', 'A15_T510092', 'A15_T510076')
CONDITIONS = ('early', 'middle', 'late', 'faint_speech', 'strong_correction',
              'reference_change', 'fade', 'dropout', 'no_beep_group')
FLAGS = ('intelligibility_loss', 'new_artifacts', 'unstable_pitch', 'transition_problems')
ELIGIBLE = {'render_and_review': 'direct_numeric', 'group_render_and_review': 'transfer_group'}


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


def policy_hash():
    # Includes server extraction/decision code as well as sampling logic.
    return hashlib.sha256(Path(__file__).read_bytes() + Path(__file__).with_name('deflutter_review.py').read_bytes()).hexdigest()


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


def select_samples(candidates, budget=30, seconds=20, seed=15024, required=(), prior=()):
    """Risk quota, weighted set coverage, then a seeded ordinary random sample.

    Duplicates require an exact denoised excerpt hash (or an explicitly supplied
    content identity with independent provenance). Content lag is never consumed.
    """
    if not isinstance(budget, int) or not 1 <= budget <= 100:
        raise ValueError('Listening budget must be 1–100 comparisons')
    if not 15 <= seconds <= 25:
        raise ValueError('Clip length must be 15–25 seconds')
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


def assess(queue, events, historical=()):
    latest = {e['sample_id']: e for e in events}
    failed = set()
    for e in list(events)+list(historical):
        if adverse(e):
            failed.update(e['cohorts'])
    pending = [s['id'] for s in queue['samples'] if s['id'] not in latest]
    return dict(pending=pending, failed_cohorts=sorted(failed),
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
    from flutter_plan_store import load_saved_plan
    from carrier_survey import load_survey
    current_plan = load_saved_plan(plan_path, expand_regions=False)
    if not current_plan.get('full_tape') or current_plan.get('review_only'):
        raise ValueError('Sampling production acceptance requires a complete full-tape plan')
    survey = current_plan['inventory'].get('carrier_survey')
    if not survey:
        raise ValueError('Sampling acceptance requires a bound original carrier survey')
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
