"""Owner-only consolidation of isolated review provenance, no audio access."""
from deflutter_campaign import ROOT,read,write,sha,append,stamp


def contains_amplitude_audit(value):
    if isinstance(value,dict):return 'minimum_response_amplitude' in value or any(contains_amplitude_audit(v) for v in value.values())
    if isinstance(value,list):return any(contains_amplitude_audit(v) for v in value)
    return False


def run():
    merge_file=ROOT/'line_merges.json';merged=read(merge_file) if merge_file.exists() else []
    for path in sorted((ROOT/'lines').glob('*/line_exposure.json')):
        identity=dict(path=str(path.relative_to(ROOT)),sha256=sha(path.read_bytes()))
        if identity in merged:continue
        append(ROOT/'exposure_ledger.jsonl',dict(actor='campaign_owner',evidence='isolated_subagent_access_record',
            source_record=identity,details=read(path),restriction='Historical development/source features only; no fresh real qualification from review'))
        merged.append(identity)
    write(merge_file,merged)
    stages=[]
    for prefix in ('I039_','I040_','I041_','I042_','I043_','I044_','I045_','I047_','I048_'):
        for folder in sorted((ROOT/'iterations').glob(prefix+'*')):
            path=folder/'results.json'
            if not path.exists():continue
            rows=read(path)
            if not isinstance(rows,list):rows=[rows]
            stages.append(dict(iteration=folder.name,recorded_rows=len(rows),
                rows_proving_derived_response_amplitude_access=sum(contains_amplitude_audit(r) for r in rows),
                meaning='Per-row provenance; may include several attempted pilots. Zero is no recorded audit amplitude access, not proof of all feature independence.',
                restriction='Legacy composition allowed all-role amplitude features before selection. Any reached audit is feature-conditioned development; frequency-error consumption records unchanged.'))
    write(ROOT/'evidence_limits_feature_sealing_I050.json',dict(at=stamp(),
        defect='Legacy all-role waveform amplitudes could choose/withhold nuisance measurement and pilot before frozen V/F evaluation',
        affected_stages=stages,review='lines/contract_audit/affected_claims.json',
        preserved='Numerical facts/maps/source hashes and old frequency-error-consumption markers; never reopen consumed reservations',
        repair='V10 learns scalar only from separate finite C intervals, zeros V/F+25ms before eligibility, restores actual signal after fixed nuisance prediction',
        test='sealed_unit_I050.log: arbitrary held-out content/nearby impulse interventions preserve metadata/prediction; research_tests_I050.log50passed',
        limitation='Eligibility repair does not bound event-only contamination or authenticate historical transport',
        production_or_champion_upgraded=False))
    write(ROOT/'parallel_lines.json',dict(at=stamp(),
        owner='root; sole shared-ledger and heavy-compute owner',authorization='Ben2026-10-30T21:29UTCexplicit efficient subagents',
        lines=[dict(name='contract_audit',model='gpt-6.1-sol',effort='high',status='complete',
                    disposition='Sealing defect established; copy witness inadmissible for existing maps; event-local nuisance falsification recommended'),
               dict(name='timing_witness',model='gpt-6.1-sol',effort='high',status='complete',
                    disposition='No coded witness in24s;120ppscomb; catalog supports one frozen preprocessing attempt, not broad absence'),
               dict(name='donor_feasibility',model='gpt-6.1-sol',effort='medium',status='complete',
                    disposition='Distinct fixed-waveform FIR hypothesis; no certified independent donor; stop before waveform fitting')],
        no_astra=True,no_source_upload=True,no_production=True))
    print('Merged',len(merged),'isolated access records; characterized',len(stages),'legacy stages',flush=True)


if __name__=='__main__':run()
