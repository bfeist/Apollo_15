"""Summarize saved nested-modulation experiments, without rerunning audio analysis."""
from pathlib import Path
import ast, hashlib, html, json
import numpy as np

ROOT=Path(__file__).resolve().parents[2]
DEST=ROOT/'reports/nested_tonefree_20261017'

def read(name):return json.loads((DEST/name).read_text(),parse_constant=lambda x:(_ for _ in ()).throw(ValueError(x)))
def table(headers,rows):
    return '<table><tr>'+''.join('<th>'+html.escape(str(x))+'</th>' for x in headers)+'</tr>'+''.join('<tr>'+''.join('<td>'+html.escape(str(x))+'</td>' for x in row)+'</tr>' for row in rows)+'</table>'

def run():
    synth=read('synthetic.json');real=read('real.json');transfer=read('transfer.json');leak=read('leakage.json');pitch=read('pitch_real.json')
    st=[]
    for r in synth:
        if r['tracks']==8 and r['band']==[4,10]:st.append([r['kind'],r['peak_hz'],r['correlation'],r['signed_gain'],r['phase_error_deg'],round(r['displacement_rmse_us'],1),round(100*r['estimated_speed_rms'],3)])
    rt=[]
    for w in real:
        c=next(c for c in w['cases'] if c['group']=='all_independent' and c['band']==[4,10])
        rt.append(['T510077',w['start'],len(c['channels']),round(c['peak_hz'],3),round(c['correlation'],3),round(c['signed_gain'],4)])
    c=transfer[0];rt.append(['T510076',c['start'],len(c['channels']),round(c['peak_hz'],3),round(c['correlation'],3),round(c['signed_gain'],4)])
    text='''<!doctype html><html><meta charset="utf-8"><title>Tone-free nested modulation research</title><style>body{font:17px/1.55 system-ui;max-width:1120px;margin:40px auto;padding:0 24px;color:#182738}h1,h2{line-height:1.2}table{border-collapse:collapse;font-size:14px;width:100%;margin:20px 0}th,td{text-align:left;padding:8px;border:1px solid #ccd5dd}th{background:#eaf0f5}code{background:#edf1f4}a{color:#135d9d}.finding{padding:20px;background:#eaf2f7}</style><body>
<h1>Can nested spectral analysis remove flutter without a continuous tone?</h1><p>2026-10-17. Isolated research; no production integration and no new listening-approved correction.</p>
<p class="finding">A cyclic rate can be detected without feeding the known reference tone into the estimator. Recovering its signed timing curve is harder: controlled synthetic mixtures recover phase but attenuate amplitude; real multitrack estimates recover only a small fraction of the benchmark motion and do not reliably improve held-out beeps. This is evidence against these estimators as ready corrections, not a proof that tone-free recovery is impossible.</p>
<h2>What was actually estimated</h2>
<p>Route A takes 40 ms Hann spectra at 200 frames/second, remaps them onto log frequency, removes the broad spectral envelope, and estimates signed spectral translation between adjacent frames using the spectral derivative. Accumulation yields a fractional-frequency-motion trajectory. A second, complex STFT (4 second windows, 0.5 second hops) retains phase while selecting 4–10 Hz or 2–30 Hz modulation; inverse STFT reconstructs the signed curve. The 2–30 Hz branch admits harmonics. Nothing forces a 6.5 Hz sinusoidal phase.</p>
<p>Route B tracks the pitch/harmonic-family trajectory with normalized short-time autocorrelation, continuity and voiced-frame gates, then analyzes log-pitch modulation with the same complex second stage. This is an F0-family estimator, not a complete tracker of individually labeled partials. It failed on the real excerpts.</p>
<p>Each track gets one vote. An alternative coherence gate accepts a time-frequency bin only when squared coherent sum divided by N times summed energy exceeds 0.45. This rejects incoherent voices but also discards weak true motion. It is cross-track modulation coherence, not a full spectral-correlation/cyclostationary estimator. AM is not used as a timing curve.</p>
<h2>Controlled recovery and negative controls</h2>
<p>Eight independently generated formant-shaped voices have separate F0 trajectories, changing vowels, syllabic envelopes and consonant noise. These are speech-like signals, not high-fidelity human voices. A known common 2.5% speed modulation is injected through explicit time resampling. The drifting case sweeps from 6 to 7.2 Hz and adds a 0.7% second harmonic. Negative controls include unwarped content, common 65% AM at 6.5 Hz, and independent F0-only modulation near 6.5 Hz with fixed formant timing. Signed gain is projection onto the known speed error, with 1 ideal and 0 no recovery; displacement error integrates the difference and removes a constant offset. Analysis excludes 3 seconds at either end.</p>
'''
    text+=table(['Synthetic case, 8 tracks','Peak Hz','Correlation','Signed gain','Phase error degrees','Displacement RMSE µs','Estimated speed RMS %'],st)
    text+='''<p>For the common 6.5 Hz case, 8 tracks improve correlation to 0.925, versus 0.462 for one. Phase error is about -1.9 degrees, but gain is only 0.597. The residual speed error is 47% of the planted error; this is partial recovery, not an accurate full correction. Natural content still causes nonzero false corrections.</p>
<p>Eight additional non-simultaneous archive excerpts from CH12, 13, 14, 15, 18, 20, 27 and 30 provide real recording content. They retain unknown original flutter and are not certified eight distinct active speakers. Trials apply the same common warp to these different excerpts, with a ninth held-out tone used only for correction measurements. Both raw-input and prefiltered-input variants are retained. In raw-input trials, spectral-motion gain is about 0.259 (0.222 with coherence gating). Coherence gating limits false speed RMS to roughly 0.003% on unwarped/AM controls and 0.008% on independent per-track warps. The archive JSON label <code>independent_pitch</code> denotes independently time-warped whole tracks, a stronger proxy control, not F0-only synthesis; the synthetic-voice control above really changes F0 independently.</p>
<p>Prefiltering before injecting a warp can create moving spectral edges that act as artificial references. The raw-input variant avoids those manufactured edges but can retain unidentified original tones. Consequently neither archive injection variant alone proves tone-free speech recovery. The synthetic voiced-content experiment supplies the cleanest known-ground-truth test. No universal amplitude calibration follows: measured response depends strongly on content.</p>
<h2>Real simultaneous tracks: frozen estimator</h2>
<p>All 30 tracks were read for each of two 60-second T510077 benchmark spans and one T510076 span. T510077 estimation excludes CH1 (clock), CH30 (known reference), CH15 (held-out target), and CH20 (known event replica). T510076 additionally holds out CH12. Strong zero-lag envelope duplicates are removed before averaging; this does not establish independence of all remaining feeds or remove delayed replicas. The two T510077 windows contain 22 remaining candidates each; their metadata identify the same HR2U head. No reference-fitted gain or lag is applied.</p>
'''
    text+=table(['Tape','Window start seconds','Candidate tracks','Peak Hz','Correlation with reference','Signed gain versus reference'],rt)
    text+='''<p>The benchmark reference is tracked only after candidate construction, for evaluation. Held-out target beeps are excluded spectrally and by target channel. On T510077, ordinary spectral averaging leaves raw beep RMS largely at its original 47–66 Hz range; occasional tiny improvements are mixed with regressions. Pitch averaging is much worse on some events (approximately 62 to 131 Hz and 66 to 135 Hz), while pitch coherence gating returns zero correction. These raw-window measurements must not be compared directly with the earlier cleaned listening-file measurements.</p>
<h2>Leakage, phase and identifiability</h2>
<p>The estimator excludes frequencies below 300 Hz, 800–1200 Hz around the known reference, 2150–2850 Hz around Quindars, and detected persistent spectral lines. A stricter test additionally excludes neighborhoods of every 50/60 Hz harmonic. Even these masks cannot certify absence of all electrical interference, FM sidebands or bleed-through speech. The real spectral peak therefore remains a candidate common motion signature, not proof of speech-only transport recovery.</p>
'''
    text+=table(['T510077 window','Strict-mask correlation','Strict-mask gain','Shuffles at least as strong / 40'],[[r['start'],round(r['correlation'],3),round(r['signed_gain'],4),r['phase_shuffle_exceedances']] for r in leak])
    text+='''<p>Deterministic per-track time shifts provide 40 phase-shuffle controls for 6.3–6.8 Hz power. Excess power supports shared phase structure, but shared electrical signals or duplicate feeds can also produce it. This is a small diagnostic null test with a prespecified benchmark band, not independent confirmation of transport provenance.</p>
<p>A second FFT magnitude gives a rate but loses sign and phase. Retaining complex coefficients solves that loss only for the trajectory already measured. It cannot distinguish genuine source pitch/articulation from recording speed without further assumptions. Formally, recordings xᵢ(t)=sᵢ(φ(t)) admit alternative common time coordinates if the unknown sources are reparameterized accordingly. Independent-content priors, sufficiently informative overlapping speech, known stable features or external references are needed to resolve this ambiguity. Thirty tracks are valuable evidence, but thirty arbitrary signals do not automatically identify φ.</p>
<p>The concrete blockers here are content-dependent gain, unreliable pitch tracking amid interference, weak independent phase support, and unverified provenance of small real common-mode peaks. More gain applied to these curves would amplify estimation errors; calibrating that gain to the held-out tone would cease to be the requested tone-free test. A useful next experiment would validate individual speech partial tracks and within-voice harmonic agreement, form a common latent motion model with uncertainty, and demonstrate response calibration on independent speech before another real listening test. No claim of a general impossibility is warranted.</p>
<h2>Reproduction, scope and artifacts</h2>
<p>Run with the repository virtual environment and <code>python -B</code>: <code>nested_tonefree.py</code>, <code>nested_controls.py</code>, <code>nested_controls.run(clean=False)</code>, <code>nested_pitch.py</code>, <code>nested_transfer.py</code>, <code>nested_leakage.py</code>, then <code>nested_report.py</code>, all under <code>research/irig_free_20261016/</code>. Fixed random seeds, parameters, excerpt paths/hashes, scalar results and signed/complex curves are saved. Source files are checked against the established canonical inventory and rechecked for size/mtime stability. An initial inclusive-STFT-endpoint error was fixed before real evaluation; synthetic outputs were regenerated. Undefined zero-output correlations are null. No downloads or production processing were performed.</p>
<p>Coverage is three minute-long simultaneous windows on two tapes, plus eight 24-second nonsimultaneous injection inputs; no full-tape or all-mission inference. The prior two listening-approved continuous-tone examples and the rejection of the older eight examples remain unchanged. No new audio comparison was rendered because no real tone-free candidate showed sufficient consistent improvement.</p><ul>
'''
    for p in sorted(DEST.iterdir()):
        if p.suffix in ['.json','.npz']:text+=f'<li><a href="{p.name}">{p.name}</a></li>'
    text+='</ul></body></html>';(DEST/'index.html').write_text(text,encoding='utf-8')
    sources=[]
    for name in ['sources.json','pitch_sources.json','transfer_sources.json','leakage_sources.json']:sources+=read(name)
    for name in ['archive_injection.json','archive_raw_injection.json']:sources+=read(name)['sources']
    unique={(m['path'],round(m['start_seconds'],3),m['duration_seconds']):m for m in sources}
    checks=[dict(path=m['path'],same_size_mtime=Path(m['path']).stat().st_size==m['bytes'] and Path(m['path']).stat().st_mtime_ns==m['mtime_ns']) for m in unique.values()]
    baseline=json.loads((ROOT/'reports/cyclic_flutter_20261017/baseline.json').read_text())
    production={name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest()==h for name,h in baseline.items() if name!='IRIG_FREE_FLUTTER_RESEARCH.md'}
    scripts=list((ROOT/'research/irig_free_20261016').glob('nested_*.py'))
    for p in scripts:ast.parse(p.read_text())
    parsed=[]
    for p in DEST.glob('*.json'):read(p.name);parsed.append(p.name)
    for p in DEST.glob('*.npz'):
        with np.load(p) as d:
            assert all(np.isfinite(d[k]).all() for k in d.files),p
    result=dict(unique_excerpt_reads=len(unique),unique_source_files=len({m['path'] for m in unique.values()}),source_checks=checks,production_matches_prior_baseline=production,parsed_scripts=[p.name for p in scripts],strict_json_files=parsed,all_saved_arrays_finite=True)
    assert all(x['same_size_mtime'] for x in checks)
    (DEST/'verification.json').write_text(json.dumps(result,indent=2));print(json.dumps({k:v for k,v in result.items() if k!='source_checks'},indent=2))

if __name__=='__main__':run()
