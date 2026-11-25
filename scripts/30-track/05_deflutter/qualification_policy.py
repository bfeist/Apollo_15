"""Frozen tape-level qualification policy: flags, partitions and decisions.

Pure functions; no file access. The policy decides what the *shared* tape map
may apply. Detection gates remain in ``flutter_census``. The policy never
flips a sign, never adds a per-channel lag and never treats missing check
beeps as a rejection. Beep audits are split once into a decision half (used to
flag and resolve contradictions) and a held-out half (used only to validate),
so validation never reuses the measurements that selected a decision.
"""
import hashlib
import json
from itertools import combinations

import numpy as np
from flutter_census import same_source

VERSION = 'tape-qualification-policy-v1'
DECISIONS = ('apply', 'bypass_unsupported', 'bypass_unresolved_contradiction', 'incomplete')
CONTRADICTION_FLAGS = ('adverse_audit', 'reversed_audit', 'concentration_reversed')
FLAG_ORDER = (*CONTRADICTION_FLAGS, 'sign_conflict', 'overlap_inconsistent', 'near_threshold', 'boundary')

POLICY = dict(
    version=VERSION,
    # Physical beep events, never channel copies, decide contradictions.
    audit=dict(decision_fraction=.5, min_physical_events=3, adverse_ratio_above=1.,
               reversed_margin=.9, concentration_min_lines=2),
    near_threshold=dict(fm_fraction=.1, clipped_fraction_of_max=.5),
    overlap=dict(min_r=.8),
    boundary_seconds=2.,
    sample=dict(candidate_fraction=.02, unsupported_fraction=.02, max_per_tape=4),
    resolution=dict(reproduction_min_r=.9, family_min_r=.7, max_group_share=.5, hf_min_lines=2,
                    beep_min_samples=60, beep_follow_r=.3, beep_independent_abs_r=.3,
                    hf_family_separation=.025, mirror_explained_fraction=.5, accept_new_hypotheses=False),
    # Split by tape: neighbouring windows and copies of one event stay together.
    # Tapes inspected by ear on November 15/17 are development evidence only.
    split=dict(validation_fraction=.5,
               development_tapes=['A15_T510073', 'A15_T510083', 'A15_T510106']),
    validation=dict(min_held_out_events=20, max_worse_fraction=.05, max_median_ratio=.5),
)


def normalized(value):
    return json.loads(json.dumps(value, sort_keys=True, allow_nan=False))


def policy_digest(policy=POLICY):
    return hashlib.sha256(json.dumps(normalized(policy), sort_keys=True).encode()).hexdigest()


def unit_hash(*parts):
    value = hashlib.sha256(':'.join(str(p) for p in parts).encode()).hexdigest()
    return int(value[:12], 16)/16**12


def event_partition(seed, tape, event_id, policy=POLICY):
    """Deterministic, per physical event: 'decision' or 'held_out'."""
    return 'decision' if unit_hash(seed, 'event', tape, event_id) < policy['audit']['decision_fraction'] else 'held_out'


def tape_partition(seed, tape, policy=POLICY):
    split = policy['split']
    if tape in split['development_tapes']:
        return 'development'
    return 'validation' if unit_hash(seed, 'tape', tape) < split['validation_fraction'] else 'development'


def sampled(seed, tape, window_id, kind, fraction):
    return unit_hash(seed, 'sample', kind, tape, window_id) < fraction


# ---------------------------------------------------------------- pass-2 flags

def near_threshold(window, gates, policy=POLICY):
    """Gate margins, for eligible and ineligible windows alike."""
    if 'gates' not in window:
        return False
    p = policy['near_threshold']
    agree = len(window.get('agreeing_groups', []))
    hum = window.get('hum_cluster')
    fm = window.get('consensus_fm_percent', 0.)
    low, high = gates['consensus_fm_percent']
    cluster = window.get('cluster_fm_percent', 0.)
    near = [
        agree in (gates['min_agreeing'], gates['min_agreeing']-1),
        hum and agree in (gates['hum_min_agreeing'], gates['hum_min_agreeing']-1),
        abs(fm-low) <= p['fm_fraction']*low or abs(fm-high) <= p['fm_fraction']*high,
        abs(cluster-gates['cluster_min_fm_percent']) <= p['fm_fraction']*gates['cluster_min_fm_percent'],
        window.get('clipped_fraction', 0.) >= p['clipped_fraction_of_max']*gates['max_clipped_fraction'],
    ]
    failed = [k for k, v in window['gates'].items() if not v]
    if window.get('eligible'):
        return any(near)
    return len(failed) == 1 and any(near)


def audit_flags(physical, concentration, policy=POLICY):
    """Contradiction flags from decision-half physical events and witnesses."""
    a = policy['audit']
    flags = []
    if physical['n'] >= a['min_physical_events']:
        if physical['median_ratio'] > a['adverse_ratio_above']:
            flags.append('adverse_audit')
        if physical['median_wrong_sign_ratio'] < a['reversed_margin']*physical['median_ratio']:
            flags.append('reversed_audit')
    lines = concentration or []
    if len(lines) >= a['concentration_min_lines'] and \
            sum(r['wrong_sign'] > r['curve'] for r in lines) > len(lines)/2:
        flags.append('concentration_reversed')
    return flags


def window_flags(window, physical, overlap_min_r, boundary, gates, policy=POLICY):
    flags = []
    if window.get('eligible'):
        flags += audit_flags(physical, window.get('concentration_audit'), policy)
        if window.get('sign_conflict'):
            flags.append('sign_conflict')
        if overlap_min_r is not None and overlap_min_r < policy['overlap']['min_r']:
            flags.append('overlap_inconsistent')
    if near_threshold(window, gates, policy):
        flags.append('near_threshold')
    if boundary:
        flags.append('boundary')
    return [f for f in FLAG_ORDER if f in flags]


def classify(window, flags):
    """Analysis state, not a render permission."""
    if not window.get('eligible'):
        return 'unsupported'
    return 'needs_analysis' if flags else 'candidate'


# ---------------------------------------------------------------- follow-up evidence

def pooled_beeps(beeps, lags, policy=POLICY):
    """Pool per-beep sufficient statistics; each beep was demeaned separately."""
    r = policy['resolution']
    n = sum(b['n'] for b in beeps)
    if n < r['beep_min_samples'] or not beeps:
        return dict(state='insufficient', samples=n, beeps=len(beeps))
    dd = sum(b['dd'] for b in beeps)
    cc = np.sum([b['cc'] for b in beeps], 0)
    dc = np.sum([b['dc'] for b in beeps], 0)
    rr = dc/np.sqrt(np.maximum(dd*cc, 1e-30))
    zero = lags.index(0)
    best = int(np.argmax(abs(rr)))
    r0 = float(rr[zero])
    if r0 >= r['beep_follow_r']:
        state = 'follows'
    elif r0 <= -r['beep_follow_r']:
        state = 'inverted'
    elif abs(r0) < r['beep_independent_abs_r'] and abs(float(rr[best])) < 2*r['beep_follow_r']:
        state = 'independent'
    else:
        state = 'lagged'
    return dict(state=state, samples=n, beeps=len(beeps), r0=round(r0, 4),
                best_lag_seconds=lags[best]/200, best_r=round(float(rr[best]), 4),
                gain=round(float(dc[zero]/max(cc[zero], 1e-30)), 4))


def resolve(row, followup, policy=POLICY):
    """Decision for one census window under this frozen policy.

    ``row`` is the pass-2 window row; ``followup`` the follow-up measurements
    for it (with ``decision_beeps`` already restricted to the decision half),
    or None when it was not analysed.
    """
    r = policy['resolution']
    flags = set(row['flags'])
    if followup is not None and followup.get('status') != 'complete':
        return dict(decision='incomplete', reasons=['queued follow-up measurements are missing'], resolution=None)
    if not row['eligible']:
        return dict(decision='bypass_unsupported', reasons=['detector gates failed: '+', '.join(row.get('reasons', []))],
                    resolution=None)
    if followup is None or followup.get('status') != 'complete':
        if row['class'] == 'candidate':
            return dict(decision='apply', reasons=['eligible without flags'], resolution=None)
        return dict(decision='incomplete', reasons=['flagged window has no completed follow-up analysis'],
                    resolution=None)
    beeps = followup['beep_state']
    hf = followup['hf']
    hf_lines = [v for v in hf if v['concentrates'] is not None]
    hf_reversed = sum(v['reversed_better'] for v in hf_lines)
    # Two sharpening lines must represent separate feeds and frequency families.
    confirming = [v for v in hf_lines if v['concentrates']]
    def independent(a, b):
        return (a['group'] != b['group'] and not same_source(a['hz'], b['hz'])
                and abs(a['hz']-b['hz']) >= r['hf_family_separation']*max(a['hz'], b['hz']))
    hf_confirms = hf_reversed == 0 and any(
        all(independent(a, b) for a, b in combinations(lines, 2))
        for lines in combinations(confirming, r['hf_min_lines']))
    family = followup['family']
    sign = followup.get('sign_conflict') or {}
    checks = dict(
        reproduced=(followup['reproduced']['eligible'] and followup['reproduction_r'] is not None
                    and followup['reproduction_r'] >= r['reproduction_min_r']),
        families_consistent=family['min_r'] is None or family['min_r'] >= r['family_min_r'],
        not_single_feed=(family['max_group_share'] is not None
                         and family['max_group_share'] <= r['max_group_share']),
        overlap_consistent='overlap_inconsistent' not in flags,
        sign_conflict_explained=(not (row.get('sign_conflict') or sign.get('reproduced'))
                                 or sign.get('explained_fraction', 0.) >= r['mirror_explained_fraction']),
        hf_confirms=hf_confirms,
        hf_not_reversed=not (len(hf_lines) >= r['hf_min_lines'] and hf_reversed > len(hf_lines)/2),
        beeps_not_inverted=beeps['state'] != 'inverted')
    support_ok = all(checks[k] for k in ('reproduced', 'families_consistent', 'not_single_feed', 'overlap_consistent'))
    contradiction = sorted(flags & set(CONTRADICTION_FLAGS))
    if not checks['hf_not_reversed']:
        contradiction.append('followup_hf_reversed')
    if not checks['beeps_not_inverted']:
        contradiction.append('followup_beeps_inverted')
    if not checks['sign_conflict_explained']:
        contradiction.append('unexplained_sign_conflict')
    failed = [k for k, v in checks.items() if not v and k in
              ('reproduced', 'families_consistent', 'not_single_feed', 'overlap_consistent')]
    if contradiction:
        # A contradiction is resolved only when the adverse witness is shown not
        # to follow any motion and independent >200 Hz tones confirm this sign.
        if (support_ok and hf_confirms and beeps['state'] == 'independent'
                and checks['sign_conflict_explained'] and checks['hf_not_reversed']):
            return dict(decision='apply', reasons=contradiction, checks=checks,
                        resolution='audit witness does not follow tape motion; independent tones confirm the sign')
        return dict(decision='bypass_unresolved_contradiction', reasons=contradiction+failed, checks=checks,
                    resolution=None)
    if support_ok:
        return dict(decision='apply', reasons=sorted(flags) or ['eligible without flags'], checks=checks,
                    resolution='follow-up stability checks passed' if flags else None)
    return dict(decision='bypass_unresolved_contradiction', reasons=failed, checks=checks, resolution=None)
