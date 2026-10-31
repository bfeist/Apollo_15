"""Read-only audit of frozen v5 discovery; never tune against final outcomes.

Run: .venv/Scripts/python.exe research/v5_discovery_audit.py
Creates a separate evidence JSON; does not change plans, sources, or policy.
The larger candidate list uses the SAME detector with only its output cap lifted.
Additional spectral candidates are not qualified timing references.
"""
import os
for name in ('OPENBLAS_NUM_THREADS', 'OMP_NUM_THREADS', 'MKL_NUM_THREADS'):
    os.environ.setdefault(name, '1')
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path

import numpy as np
import soundfile as sf

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO/'scripts'))
from flutter_tracking import discover
from flutter_policy import implementation_digest

ROOT = Path('D:/Apollo_15_30-Track/deflutter_planning_v5_allchannel')
OUTPUT = REPO/'reports/deflutter_discovery_audit_20261030/evidence.json'
# Chosen by archive position, not correction outcome: first, late resumed, last.
TAPES = ('A15_T510073', 'A15_T510100', 'A15_T510112')
# Timing source, early speech channel, middle speech channel, late speech channel.
CHANNELS = (1, 3, 14, 22)
FRACTIONS = (0., .5, .9)


def exact_mains(hz):
    return abs(hz/60-round(hz/60))*60 <= .5


def read_excerpt(row, a, b):
    with sf.SoundFile(row['path']) as stream:
        stream.seek(round((a-row['start_seconds'])*row['sample_rate']))
        return stream.read(round((b-a)*row['sample_rate']), dtype='float64')


def synthetic():
    sr = 8000
    t = np.arange(64*sr)/sr
    rng = np.random.default_rng(31)
    noise = rng.normal(0, .0001, len(t))
    useful = [317., 457., 617., 757., 917., 1057., 1217., 1357.,
              1517., 1657., 1817., 1957.]
    hum = [120., 240., 360., 480., 600., 720., 840., 960.]
    mixed = noise.copy()
    for hz in useful+hum:
        mixed += .025*np.cos(2*np.pi*hz*t)
    capped = discover(mixed, sr, 8)
    all_rows = discover(mixed, sr, 4096)
    intermittent = noise + .1*np.cos(2*np.pi*777*t)*(t < 20)
    adjacent = noise + .1*np.cos(2*np.pi*120*t) + .03*np.cos(2*np.pi*137*t)
    return dict(
        quota=dict(injected_nonmains=useful, injected_mains=hum,
                   cap8=capped, uncapped=all_rows,
                   cap8_surviving_mains_exclusion=[c['carrier_hz'] for c in capped if not exact_mains(c['carrier_hz'])]),
        intermittent=dict(carrier_hz=777., on_seconds=20, total_seconds=64,
                          discovered=discover(intermittent, sr, 4096)),
        adjacent=dict(injected_hz=[120.,137.], discovered=discover(adjacent, sr, 4096)))


def audit_tape(tape):
    base = ROOT/tape
    manifest_path = base/'survey_manifest.json'
    manifest = json.loads(manifest_path.read_text())
    plan = json.loads((base/'plan/plan.json').read_text())
    counts = Counter()
    frequencies = Counter()
    for window in manifest['windows']:
        for item in window['channels'].values():
            counts['channel_contexts'] += 1
            counts['context_status_'+item['status']] += 1
            for version in ('original','denoised'):
                candidates = [c for c in item['candidates'] if version in c.get('provenance',[])]
                counts[version+'_candidates'] += len(candidates)
                counts[version+'_contexts_at_cap8'] += len(candidates) == 8
                counts[version+'_mains_excluded'] += sum(c['tracking_status']=='excluded_digitization_mains' for c in candidates)
            for c in item['candidates']:
                counts['candidate_status_'+c['tracking_status']] += 1
                frequencies[str(c['carrier_hz'])] += 1
    # Fixed temporal/channel sampling; no final event measurements are read.
    excerpts = []
    inventory = {row['channel']:row for row in manifest['inventory']['channels']}
    for fraction in FRACTIONS:
        window = manifest['windows'][int(fraction*(len(manifest['windows'])-1))]
        for ch in CHANNELS:
            row = inventory[ch]
            item = window['channels'][str(ch)]
            if item['status'] != 'analyzed':
                continue
            a, b = item['context_start'], item['context_end']
            original = read_excerpt(row, a, b)
            versions = {'original':original}
            clean_path = Path(manifest['denoised_root'])/f'CH{ch:02d}'/'denoised.wav'
            if item.get('discovery_sources',{}).get('denoised',{}).get('status') == 'analyzed':
                versions['denoised'] = read_excerpt(dict(row,path=str(clean_path)),a,b)
            for version, x in versions.items():
                decoded_hash = hashlib.sha256(x.tobytes()).hexdigest()
                saved_hash = (item['source_sha256'] if version == 'original' else
                              item['discovery_sources']['denoised']['decoded_excerpt_sha256'])
                if decoded_hash != saved_hash:
                    raise ValueError(f'Decoded source changed: {tape} CH{ch} {a} {version}')
                limited = discover(x,row['sample_rate'],8)
                uncapped = discover(x,row['sample_rate'],4096)
                chosen = {c['carrier_hz'] for c in limited}
                saved = {c['carrier_hz'] for c in item['candidates'] if version in c.get('provenance',[])}
                extra = [c for c in uncapped if c['carrier_hz'] not in chosen and not exact_mains(c['carrier_hz'])]
                excerpts.append(dict(channel=ch,version=version,start=a,end=b,
                    decoded_sha256=decoded_hash, matches_saved_decoded_hash=True,
                    capped_count=len(limited),uncapped_count=len(uncapped),
                    capped_nonmains_count=sum(not exact_mains(c['carrier_hz']) for c in limited),
                    additional_nonmains_count=len(extra), additional_nonmains=extra,
                    capped_frequencies=sorted(chosen), saved_frequencies=sorted(saved),
                    reproduces_saved_frequencies=chosen==saved))
    detail = json.loads((base/'plan'/plan['regions'][0]['detail_file']).read_text())
    budgets = Counter()
    eligible = []
    for ref in detail['monitor_records']:
        record = json.loads((base/'plan'/ref['file']).read_text())
        budget = record['search_budget']
        budgets['monitor_windows'] += 1
        if 'eligible_candidates' in budget:
            budgets['acquisition_calls'] += 1
            budgets['budget_exhausted_calls'] += bool(budget['exhausted'])
            budgets['untried_candidate_instances'] += len(budget['untried_candidates'])
            eligible.append(budget['eligible_candidates'])
        else:
            budgets[budget.get('reason','unknown')] += 1
    return dict(tape=tape, survey_complete=manifest['complete'],
        all_recorded_sources_covered=manifest['all_recorded_sources_covered'],
        saved_implementation=plan['implementation_sha256'],
        matches_current_implementation=plan['implementation_sha256']==implementation_digest(),
        counts=counts, common_frequencies=frequencies.most_common(12),
        search_budget=budgets, eligible_candidates_per_call=eligible, excerpts=excerpts)


def archive_proposed_carriers():
    totals, frequencies = Counter(), Counter()
    for base in sorted(ROOT.glob('A15_T*')):
        plan = json.loads((base/'plan/plan.json').read_text())
        detail = json.loads((base/'plan'/plan['regions'][0]['detail_file']).read_text())
        variants = {v['key']:v for v in detail['variants']}
        totals['tapes'] += 1
        for target in detail['targets']:
            for span in target['reference_spans']:
                if not span.get('selected'):
                    continue
                variant = variants.get(span['selected'],span)
                hz = variant.get('carrier_hz',span.get('carrier_hz'))
                seconds = span['end_seconds']-span['start_seconds']
                totals['proposed_channel_seconds'] += seconds
                if hz is None:
                    totals['missing_frequency_channel_seconds'] += seconds
                    continue
                frequencies[str(hz)] += seconds
                distance = abs(hz/60-round(hz/60))*60
                if distance <= 5:
                    totals['within5hz_mains_channel_seconds'] += seconds
                if distance <= .5:
                    totals['within_half_hz_mains_channel_seconds'] += seconds
    return dict(totals=totals,top_carriers_by_channel_seconds=frequencies.most_common(15),
        caveat='Within 5 Hz is a proximity diagnostic, not proof of mains origin. Proposed time is not reserved-check improvement time.')


def main():
    result = dict(scope='Three preselected tapes; full saved surveys and monitor records; fixed 36 original contexts plus available denoised counterparts.',
                  limitation='Uncapping this detector is not an exhaustive spectral search and does not validate any added candidate.',
                  implementation_sha256=implementation_digest(),synthetic=synthetic(),tapes=[],
                  archive_proposed_carriers=archive_proposed_carriers())
    for tape in TAPES:
        row = audit_tape(tape)
        result['tapes'].append(row)
        print(tape, json.dumps(dict(counts=row['counts'],search_budget=row['search_budget'])),flush=True)
    excerpts = [e for row in result['tapes'] for e in row['excerpts']]
    for row in result['tapes']:
        by_context = {(e['channel'],e['start'],e['version']):e for e in row['excerpts']}
        for e in row['excerpts']:
            if e['version'] != 'original':
                continue
            clean = by_context.get((e['channel'],e['start'],'denoised'),{})
            combined = set(e['capped_frequencies']) | set(clean.get('capped_frequencies',[]))
            e['additional_nonmains_missing_from_both_versions'] = [c for c in e['additional_nonmains']
                if c['carrier_hz'] not in combined]
    combined_counts = sum((Counter(row['counts']) for row in result['tapes']),Counter())
    combined_budgets = sum((Counter(row['search_budget']) for row in result['tapes']),Counter())
    result['combined_counts'] = combined_counts
    result['combined_budgets'] = combined_budgets
    result['sample_summary'] = dict(excerpts=len(excerpts),
        capped_at8=sum(e['capped_count']==8 for e in excerpts),
        with_additional_nonmains=sum(e['additional_nonmains_count']>0 for e in excerpts),
        additional_nonmains_instances=sum(e['additional_nonmains_count'] for e in excerpts),
        missing_from_both_versions_instances=sum(len(e.get('additional_nonmains_missing_from_both_versions',[])) for e in excerpts),
        original_contexts_with_candidates_missing_from_both_versions=sum(bool(e.get('additional_nonmains_missing_from_both_versions',[])) for e in excerpts),
        reproduces_saved_frequencies=sum(e['reproduces_saved_frequencies'] for e in excerpts),
        per_version={version:dict(excerpts=sum(e['version']==version for e in excerpts),
            with_additional_nonmains=sum(e['version']==version and e['additional_nonmains_count']>0 for e in excerpts),
            additional_nonmains_instances=sum(e['additional_nonmains_count'] for e in excerpts if e['version']==version))
            for version in ('original','denoised')})
    OUTPUT.parent.mkdir(parents=True,exist_ok=True)
    OUTPUT.write_text(json.dumps(result,indent=2)+'\n')
    print('SUMMARY',json.dumps(result['sample_summary']),flush=True)
    print('EVIDENCE',OUTPUT,flush=True)


if __name__ == '__main__':
    main()
