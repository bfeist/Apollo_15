"""Synthesize terminal evidence boundary, not campaign/production acceptance."""
from deflutter_campaign import ROOT,read,write,stamp,append,sha
from flutter_policy import implementation_digest,policy_digest


def run():
    records=[]
    for name in ('I049_irig_availability','I052_irig_projection','I053_irig_catalog_followup'):
        for row in read(ROOT/'iterations'/name/'results.json'):
            paths=row.get('paths',[dict(path=name,attempts=row.get('attempts',[]))])
            for path in paths:
                records.append(dict(iteration=name,scope=row['scope'],path=path['path'],
                    attempts=len(path['attempts']),emitted_frames=sum(len(a['frames']) for a in path['attempts']),
                    valid_frames=sum(sum(f['valid_fields'] for f in a['frames']) for a in path['attempts']),
                    longest_run=max(a['longest_run'] for a in path['attempts'])))
    unique={(r['scope']['tape'],r['scope']['start'],r['scope']['end']) for r in records}
    write(ROOT/'timecode_availability_summary.json',dict(at=stamp(),unique_windows=len(unique),unique_source_seconds=sum(b-a for _,a,b in unique),
        attempts=sum(r['attempts'] for r in records),emitted_frames=sum(r['emitted_frames'] for r in records),
        valid_frames=sum(r['valid_frames'] for r in records),longest_run=max(r['longest_run'] for r in records),records=records,
        limitation='Declared windows/legacydecoder/frozenpaths only; not archive-wide absence'))
    nulls=[]
    for row in read(ROOT/'iterations/I051_event_local_falsification/results.json'):
        assessed=row.get('reserved_assessment',{})
        if row['kind']!='true_motion' and assessed.get('quality',{}).get('passed'):
            nulls.append(dict(index=row['index'],source_pair_index=row['source_pair_index'],kind=row['kind'],shape=row['shape'],nuisance_scope=row['nuisance_scope'],
                final_measurements=assessed['quality']['measurements'],numeric_restoration_label=assessed['quality']['restoration_target_met']))
    write(ROOT/'evidence_limits_event_local_I051.json',dict(at=stamp(),counterexamples=nulls,
        decision='Stop off-event response nuisance measurement as transport authentication under unrestricted event-local interference',
        applies_to='Interpretation of response-based numeric gains; no new real candidate promotion',
        preserves='All numerical observations, exact maps, losses, old feature/frequency exposure markers and failed generations',
        not_established='Cause of real I024gain; absence of recoverable flutter; robustness of any unrelated future independent witness',
        next_required='Independent event-local nuisance bound or physically qualified orthogonal witness, frozen before outcomes',
        no_threshold_or_pilot_rescue=True,no_production_upgrade=True))
    frozen=read(ROOT/'campaign.json')
    verification=dict(at=stamp(),implementation_sha256=implementation_digest(),policy_sha256=policy_digest())
    verification['baseline_unchanged']=verification['implementation_sha256']==frozen['implementation_sha256'] and verification['policy_sha256']==frozen['policy_sha256']
    write(ROOT/'shared_engine_verification_I054.json',verification)
    if not verification['baseline_unchanged']:raise ValueError('Shared baseline identity changed')
    pause='Investigated routes stopped: event-local false accepts; no decoded code under fixed paths; no usable broadband pair; no certified independent electrical donor. A distinct positive source/provenance lead is required before another informative real test.'
    write(ROOT/'state.json',dict(at=stamp(),completed_iteration='I054_broadband_copy_availability',status='paused_at_scientific_evidence_boundary',
        champion='frozen v5; no challenger qualified for real correction',running_jobs=[],campaign_complete=False,production_accepted=False,
        next='Bind independent witness/donor evidence, then resume the frozen staged protocol; do not repeat failed validator variants',pause_reason=pause))
    write(ROOT/'active_jobs.json',dict(at=stamp(),jobs=[],completed_iteration='I054_broadband_copy_availability',
        analysis_in_progress='Decision consolidation complete; see DECISION_SUMMARY.md',promotion_withheld=True,pause_reason=pause))
    (ROOT/'NEXT.md').write_text('# Next scientifically discriminating step\n\n'+pause+'\n\n'
        'Needed evidence: demonstrable coded modulation; a usable broadband copy with documented pre-record delay/transfer order; or an independent electrical donor with target-content/copy exclusions. '
        'The fixed donor A-to-B protocol remains unrun because source independence is unavailable. No denied operation or approval is pending. '
        'Do not reopen consumed reservations or call these findings archive-wide absence.\n\n'
        'Read DECISION_SUMMARY.md, evidence_limits_event_local_I051.json, evidence_limits_feature_sealing_I050.json, and the isolated lines/ protocols. '
        'The runbook still lacks a general authenticated measurement route, completed per-tape recommendations, independent qualification, and perceptual speech benefit. '
        'No active workers; campaign outcome remains incomplete.\n',encoding='utf-8')
    append(ROOT/'experiment_ledger.jsonl',dict(iteration='I054_campaign_synthesis',decision='scientific_evidence_boundary',
        artifact='DECISION_SUMMARY.md',running_jobs=[],campaign_complete=False,reason=pause))
    print('Consolidated',len(nulls),'stationary/wrong-motion false accepts;',len(unique),'timecode windows;',verification['baseline_unchanged'],flush=True)


if __name__=='__main__':run()
