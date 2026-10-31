"""Reporting-only synthesis of saved coverage and already-consumed evidence."""
import collections
import csv
import datetime as dt
import hashlib
import json
import math
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]/'reports/deflutter_iterative_20261030'


def read(path):return json.loads(path.read_text(encoding='utf-8-sig'))
def write(path,value):path.write_text(json.dumps(value,indent=2,allow_nan=False)+'\n',encoding='utf-8')
def digest(path):return hashlib.sha256(path.read_bytes()).hexdigest()


def union(intervals):
    result=[]
    for a,b in sorted(intervals):
        if b<=a:continue
        if result and a<=result[-1][1]+1e-8:result[-1][1]=max(result[-1][1],b)
        else:result.append([a,b])
    return result


def run():
    at=dt.datetime.now(dt.timezone.utc).isoformat()
    old=read(ROOT/'per_tape_recommendations.json');coverage=read(ROOT/'archive_coverage.json')
    archive=ROOT/'archive/report_reconciliation_I054';archive.mkdir(parents=True,exist_ok=True)
    for name in ('per_tape_recommendations.json','archive_coverage.json'):
        target=archive/name
        if not target.exists():target.write_bytes((ROOT/name).read_bytes())
    with (ROOT/'archive_coverage.csv').open(newline='',encoding='utf-8-sig') as stream:rows=list(csv.DictReader(stream))
    groups=collections.defaultdict(list)
    for row in rows:groups[row['tape']].append(row)
    inventory=read(ROOT/'inventory.json')['tapes'];tapes=sorted(t['tape'] for t in inventory)
    assert len(tapes)==39 and set(groups)==set(tapes)
    baseline_csv_hash=digest(ROOT/'archive_coverage.csv')
    gate_names=['I011_gate0','I011_gate1','I011_gate2','I014_gate0','I014_gate1','I014_gate2',
                'I016_gate1R','I024_gate0','I024_gate1','I024_gate2']
    gates=collections.defaultdict(list)
    for name in gate_names:
        folder=ROOT/'iterations'/name;declaration=read(folder/'experiment.json');metrics=read(folder/'metrics.json')
        for p in declaration['passages']:
            gates[p['tape']].append(dict(iteration=name,start_seconds=p['start_seconds'],duration_seconds=p['duration_seconds'],
                targets=metrics['targets'],statuses=metrics['statuses'],evidence=f'iterations/{name}/metrics.json'))
    lead024=read(ROOT/'iterations/I024_gate2/results.json')[0]
    final024=read(ROOT/'iterations/I024_reserved_audit/results.json')[0]
    lead016=read(ROOT/'iterations/I016_gate1R/results.json')[0]
    needed_common=[
        'Independent evidence separating historical transport from digitization/electrical interference, with physically documented transfer or an independently bounded measurement model.',
        'A newly frozen evaluation protocol and eligible independent families after exposure-ledger review; consumed outcomes cannot be reused as fresh.',
        'Absolute restoration goals and perceptual speech-quality assessment before any production recommendation; no production processing is authorized here.']
    output=[]
    for tape in tapes:
        source_rows=groups[tape];analyzed=math.fsum(float(r['analyzed_seconds']) for r in source_rows)
        available=math.fsum(float(r['available_seconds']) for r in source_rows)
        intervals=union([i for r in source_rows for i in json.loads(r.get('analysis_intervals') or '[]')])
        row=dict(tape=tape,reporting_status='current_evidence_synthesis_complete',whole_tape_investigation_complete=False,
            supported_correction_seconds=0,supported_production_seconds=0,production_accepted=False,
            discovery_status='sampled_in_this_campaign' if analyzed else 'not_searched_in_this_campaign',
            discovery_intervals=intervals,analyzed_source_seconds=analyzed,available_source_seconds=available,
            not_searched_source_seconds=math.fsum(float(r['not_searched_seconds']) for r in source_rows),
            errored_source_seconds=math.fsum(float(r['error_seconds']) for r in source_rows),
            analyzed_fraction_of_available=analyzed/available if available else 0.,saved_gate_evidence=gates[tape],
            physical_timing_qualification='withheld',perceptual_validation='not_established',
            scope_caveat='Campaign sampling only; historical v5 material may be exposed. No whole-tape absence finding.',
            evidence_restrictions=['evidence_limits_response_hum_I026.json','evidence_limits_event_local_I051.json',
                                   'evidence_limits_feature_sealing_I050.json','evidence_restrictions.json'])
        if not analyzed:
            row.update(recommendation='hold_unsearched_in_this_campaign',
                dominant_limitation='No new full-spectrum passage was searched on this tape in this campaign. Historical v5 assessments do not establish absence or an authenticated usable reference.',
                evidence_needed_to_reopen=['A distinct positive timing-source or transfer/provenance lead for this tape; any later sampling must be predeclared, preserve original/denoised separation and account for unsearched coverage.']+needed_common)
        elif tape in ('A15_T510085','A15_T510098'):
            row.update(recommendation='hold_event_evidence_unavailable',
                dominant_limitation='The sampled I024 gate had zero eligible targets after local independent calibration/validation/final-family availability checks. This is event scarcity, not absent tones or a failed correction.',
                evidence_needed_to_reopen=['A predeclared supported interval with at least two eligible calibration and two validation families separated by at least3s, plus two distinct local final families in the same island; alternatively an explicitly validated independent measurement route. Existing historical coordinate counts alone are insufficient.']+needed_common)
        elif tape in ('A15_T510103','A15_T510108'):
            row['recommendation']='hold_numerical_lead_unqualified'
            if tape=='A15_T510103':
                row.update(dominant_limitation='I024 CH16 relative numerical gain misses absolute restoration on both validation and local final events and is not transport-authenticated. Off-event nuisance authentication is falsified; the fixed broadband pair and coded-clock routes supplied no qualified witness.',
                    numerical_lead=dict(iteration='I024_gate2',target_channel=16,reference_channel=8,passage_seconds=[22440,22740],supported_local_island_seconds=[60,180],
                        validation=dict(scope='Worst across3validation events (9margins); already consumed',**lead024['quality']['measurements']),
                        local_final=dict(scope='Worst across2local final families (6margins), consumed once; not untouched qualification',**final024['quality']['measurements']),
                        restoration_target_met=False,transport_authenticated=False,
                        final_outcomes_reusable_as_fresh=False,evidence=['iterations/I024_gate2/results.json','iterations/I024_reserved_audit/results.json']),
                    evidence_needed_to_reopen=['A distinct independently qualified witness or event-local nuisance bound for this transfer. The tested CH11/12 broadband pair failed availability and cannot be rescued by a new map; a different usable pair also needs documented pre-record delay and known-warp/null sensitivity. The two I024 final families remain consumed.']+needed_common,
                    additional_evidence=['iterations/I054_broadband_copy_availability/metrics.json','timecode_availability_summary.json','lines/donor_feasibility/assessment.md'])
            else:
                row.update(dominant_limitation='I016 CH10 is only a retrospective partial numerical lead; validation residual exceeds restoration goals. Only one local final family was assigned and its I016 outcome was not evaluated; a later unsupported map island is quarantined. Older delayed-copy claims remain withdrawn.',
                    numerical_lead=dict(iteration='I016_gate1R',target_channel=10,reference_channel=22,passage_seconds=[12060,12360],
                        validation=dict(scope='Worst across saved validation events; already consumed',**lead016['quality']['measurements']),
                        local_final=dict(status='not_evaluated_in_I016',assigned_families=1,minimum_required_families=2),
                        restoration_target_met=False,transport_authenticated=False,evidence=['iterations/I016_gate1R/results.json','evidence_limits_I016.json']),
                    evidence_needed_to_reopen=['An independently authenticated witness and a newly frozen exact map restricted to demonstrated support; at least two eligible distinct local final families within the same island, with exposure-ledger restrictions preserved. Do not open the single I016 final outcome to fit a model or reuse withdrawn copy-dependent results.']+needed_common)
        else:
            assert tape in ('A15_T510073','A15_T510086','A15_T510087','A15_T510091','A15_T510109')
            name=next(g['iteration'] for g in gates[tape] if g['statuses'].get('validation_failed'))
            row.update(recommendation='hold_validation_failed_in_sampled_passage',
                dominant_limitation=f'{name} evaluated one sampled target and failed validation. This is a local failed candidate, with no authenticated correction or whole-tape no-reference conclusion; most tape time remains unsearched.',
                evidence_needed_to_reopen=[f'A distinct source/transfer lead for {tape} and a frozen, source-bound candidate on a different eligible evidence block; do not tune against the consumed {name} validation outcomes or reopen final outcomes to choose a model.']+needed_common)
        if tape in ('A15_T510073','A15_T510087','A15_T510108','A15_T510103'):
            row.setdefault('additional_evidence',[]).append('timecode_availability_summary.json')
        output.append(row)
    counts=dict(collections.Counter(r['recommendation'] for r in output))
    assert counts==dict(hold_unsearched_in_this_campaign=30,hold_event_evidence_unavailable=2,
        hold_numerical_lead_unqualified=2,hold_validation_failed_in_sampled_passage=5)
    totals=dict(tapes=len(output),searched_tapes=sum(r['analyzed_source_seconds']>0 for r in output),
        unsearched_tapes=sum(r['analyzed_source_seconds']==0 for r in output),
        available_source_seconds=math.fsum(r['available_source_seconds'] for r in output),
        analyzed_source_seconds=math.fsum(r['analyzed_source_seconds'] for r in output),
        errored_source_seconds=math.fsum(r['errored_source_seconds'] for r in output),supported_production_seconds=0)
    for key in ('available_source_seconds','analyzed_source_seconds','errored_source_seconds'):
        assert math.isclose(totals[key],coverage[key],abs_tol=1e-5,rel_tol=1e-12),(key,totals[key],coverage[key])
    assert lead024['quality']['measurements']['events']==3 and final024['quality']['measurements']['events']==2
    assert not read(ROOT/'active_jobs.json')['jobs']
    result=dict(status='current_evidence_synthesis_complete',at=at,coverage_generation='Saved full-spectrum catalogs through I024; evidence interpretation through I054',
        reporting_only=True,new_experiments_run=False,source_search_expanded=False,summary_counts=counts,coverage_totals=totals,
        recommendations=output,reporting_completed='Current evidence and recommendations synthesized for all39tapes; no whole-tape investigation-complete claim',
        blocked_deliverables=['Authenticated general timing measurement','Independent real qualification','Perceptual speech benefit and human listening approval'],
        global_restrictions='All39v5tapes potentially historically exposed; consumed outcomes never fresh; global reserved mission blocks unused; zero supported production seconds; no promotion/upload/push',
        caveat='Sampled candidate failure/event scarcity/unsearched coverage are different states; none proves whole-tape absence of recoverable references.')
    write(ROOT/'per_tape_recommendations.json',result)
    coverage.update(reporting_reconciled_at=at,recommendation_evidence_generation='Through I054, reporting only',
        tape_count=39,searched_tape_count=9,unsearched_tape_count=30,recommendation_counts=counts,
        supported_production_seconds=0,coverage_fraction=totals['analyzed_source_seconds']/totals['available_source_seconds'],
        accounting_scope='Union of completed full-spectrum catalog intervals per channel/source-version; later diagnostic rereads are not added as new discovery coverage.',
        per_tape_recommendations='per_tape_recommendations.json')
    write(ROOT/'archive_coverage.json',coverage)
    table=['# Current recommendations for all39 tapes','',
        'Reporting synthesis is complete for the saved evidence through I054. Scientific qualification remains blocked. All tapes have zero supported production seconds. Coverage counts original and cached-denoised source versions separately; it is detector execution, not recall or physical tape duration. No new experiments or source searches were run.','',
        '| Tape | Current decision | Analyzed source-hours | Coverage | Dominant limitation / evidence needed |',
        '| --- | --- | ---: | ---: | --- |']
    labels={'hold_unsearched_in_this_campaign':'Unsearched in this campaign','hold_event_evidence_unavailable':'Sampled; event scarcity',
        'hold_numerical_lead_unqualified':'Numerical lead; unqualified','hold_validation_failed_in_sampled_passage':'Sampled candidate failed'}
    for r in output:
        category=r['recommendation']
        if category=='hold_unsearched_in_this_campaign':brief='No campaign discovery. Need a distinct source/provenance lead and predeclared sampling.'
        elif category=='hold_validation_failed_in_sampled_passage':
            gate=next(g['iteration'] for g in r['saved_gate_evidence'] if g['statuses'].get('validation_failed'))
            brief=f'{gate}: local validation failure. Need a distinct witness and a newly eligible evidence block.'
        elif category=='hold_event_evidence_unavailable':brief='I024: no eligible family set. Need2C/2V/2local-final families in one supported island, or an independently validated alternative.'
        elif r['tape']=='A15_T510103':brief='I024: V3worst14.26Hz/9.98c; localF2worst9.94Hz/7.07c. Need an orthogonal witness/event-local nuisance bound.'
        else:brief='I016: Vworst19.72Hz/13.93c; one local final family, unevaluated. Need a qualified witness and two eligible final families.'
        table.append(f"| {r['tape']} | {labels[category]} | {r['analyzed_source_seconds']/3600:.2f} | {100*r['analyzed_fraction_of_available']:.4f}% | {brief} |")
    table+=['','Exact intervals, saved gate references, numerical scopes, exposure restrictions and per-tape reopening conditions are in [per_tape_recommendations.json](per_tape_recommendations.json).',
        '', 'I024 validation: worst14.264744Hz/9.977118cents across3events. Local final: worst9.944281Hz/7.069397cents across2families. Both miss6Hz/5cent goals; neither authenticates transport. Local final outcomes were consumed once and are not fresh under any repaired contract.',
        '', 'Thirty unsearched tapes have no campaign-level negative finding. Five failed sampled candidates are local failures; two event-scarce gates do not establish absent tones. The two numerical leads remain quarantined from production.',
        '', 'Completed: saved research batches, failure synthesis, exposure reconciliation, current39-tape reporting and recoverable housekeeping. Blocked: authenticated measurement, independent qualification and perceptual validation. Ben has been asked separately about alternative transfers/provenance; no approval or experiment is pending here.']
    (ROOT/'PER_TAPE_RECOMMENDATIONS.md').write_text('\n'.join(table)+'\n',encoding='utf-8')
    checks=dict(at=at,reporting_only=True,all39unique_inventory_tapes=True,nine_searched_thirty_unsearched=True,
        recommendation_counts=counts,coverage_totals_match_saved_csv_and_json=True,all_supported_production_seconds_zero=True,
        I024_validation_events=3,I024_local_final_families=2,no_new_experiment_or_source_access=True,
        coverage_csv_sha256=baseline_csv_hash,coverage_csv_unchanged=digest(ROOT/'archive_coverage.csv')==baseline_csv_hash,
        output_files=['per_tape_recommendations.json','PER_TAPE_RECOMMENDATIONS.md','archive_coverage.json'],
        verified='Counts, numeric totals, gate mappings, role counts and no-active-job state; prose consistency checked separately')
    markdown=(ROOT/'PER_TAPE_RECOMMENDATIONS.md').read_text(encoding='utf-8')
    assert sum(line.startswith('| A15_') for line in markdown.splitlines())==39
    assert all(r['dominant_limitation'] and r['evidence_needed_to_reopen'] for r in output)
    assert all(r['supported_production_seconds']==0 and not r['production_accepted'] for r in output)
    assert math.isclose(math.fsum(r['not_searched_source_seconds'] for r in output)+totals['analyzed_source_seconds']+totals['errored_source_seconds'],totals['available_source_seconds'],abs_tol=1e-5,rel_tol=1e-12)
    prose_files=['DECISION_SUMMARY.md','INDEX.md','NEXT.md','CONTINUE.md','MILESTONE.md']
    for name in prose_files:
        text=(ROOT/name).read_text(encoding='utf-8')
        assert 'PER_TAPE_RECOMMENDATIONS.md' in text,name
    decision=(ROOT/'DECISION_SUMMARY.md').read_text(encoding='utf-8')
    assert 'three validation events' in decision and 'two local final families' in decision
    assert '14.264744 Hz / 9.977118 cents' in decision and '9.944281 Hz / 7.069397 cents' in decision
    assert 'no defensible completed per-tape recommendation' not in decision
    assert 'zero supported production seconds' in decision
    state=read(ROOT/'state.json')
    assert not state['running_jobs'] and not state['production_accepted'] and not state['campaign_complete']
    state.update(reporting_completed_at=at,reporting_status='current_evidence_synthesis_complete',
        reporting_tapes=39,reporting_only=True,
        blocked_scientific_deliverables=result['blocked_deliverables'],
        next='Independent source/transfer provenance evidence already requested from Ben; no experiment pending in reporting pass')
    write(ROOT/'state.json',state)
    jobs=read(ROOT/'active_jobs.json');assert not jobs['jobs']
    jobs.update(reporting_completed_at=at,analysis_in_progress='Existing-evidence report reconciliation complete; no computation active',
        reporting_status='current_evidence_synthesis_complete')
    write(ROOT/'active_jobs.json',jobs)
    checks.update(all39_markdown_rows_match_json=True,all_tapes_have_limitation_and_reopening_conditions=True,
        coverage_partition_conserved=True,I024_scope_and_prose_consistent=True,primary_report_links_present=True,
        completed_reporting_separate_from_blocked_scientific_deliverables=True,no_active_jobs=True,
        source_waveforms_read=0,new_research_jobs_started=0,
        output_files=checks['output_files']+prose_files+['state.json','active_jobs.json'],
        report_sha256={name:digest(ROOT/name) for name in checks['output_files']+prose_files},
        verified='Counts, totals, scope counts/numbers, per-tape evidence, Markdown rows, primary links, reporting/scientific status separation and no-active-job state')
    write(ROOT/'report_consistency_I054.json',checks)
    print(json.dumps(dict(reporting_status=result['status'],recommendation_counts=counts,coverage_totals=totals),indent=2))


if __name__=='__main__':run()
