"""Create a reproducible post-deflutter Quindar assessment and listening clips.

Run with `uv run --with matplotlib python scripts/assess_quindar_speed.py --help`.
This is an experimental assessment, not an automatically enabled pipeline stage.
"""
import argparse
import csv
import hashlib
import html
import json
from pathlib import Path

import numpy as np
import soundfile as sf

from quindar_speed import (fit_events, robust_fit, corrected_time, render_excerpt,
                           baseband, measure)


def hms(seconds):
    return f'{int(seconds//3600):02d}:{int(seconds%3600//60):02d}:{seconds%60:06.3f}'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--inventory', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True, help='New or empty assessment directory')
    args = parser.parse_args()
    if args.output.exists() and any(args.output.iterdir()):
        raise ValueError('Assessment output must be new or empty')
    inventory = json.loads(args.inventory.read_text())
    source = Path(inventory['source'])
    source_label = f'{source.parent.parent.name} {source.parent.name}' if source.name == 'deflutter.wav' else source.stem
    stat = source.stat()
    if (stat.st_size, stat.st_mtime_ns) != (inventory['bytes'], inventory['mtime_ns']):
        raise ValueError('Inventory source changed; rescan')
    if inventory['scan_start'] != 0 or abs(inventory['scan_end']-inventory['duration_seconds']) > .001:
        raise ValueError('Whole-channel assessment requires a full-duration scan')
    result = fit_events(inventory['events'])
    out = args.output
    out.mkdir(parents=True, exist_ok=True)
    events = result['events']
    times = np.array([(e['start_seconds']+e['end_seconds'])/2 for e in events])
    nominal = np.array([e['nominal_hz'] for e in events])
    freq = np.array([e['frequency_hz'] for e in events])
    design = np.column_stack([np.ones(len(events)), times/3600])
    a, b = result['linear_coefficients']
    c = result['constant_speed_ratio']
    duration = inventory['duration_seconds']
    result.update(status='experimental_assessment_only', source=inventory['source'],
                  source_bytes=stat.st_size, source_mtime_ns=stat.st_mtime_ns,
                  source_sample_rate=inventory['sample_rate'], source_frames=inventory['frames'],
                  candidates=len(inventory['events']), accepted_bursts=len(events),
                  inventory_sha256=hashlib.sha256(args.inventory.read_bytes()).hexdigest(),
                  analysis_implementation_sha256=hashlib.sha256(Path(__file__).read_bytes()+Path(__file__).with_name('quindar_speed.py').read_bytes()).hexdigest(),
                  nominal_source='https://apollojournals.org/alsj/quindar.html',
                  generator_tolerance='Not established from the retrieved historical source',
                  duration_seconds=duration,
                  constant_effect=dict(playback_multiplier=1/c, pitch_cents=float(1200*np.log2(1/c)),
                                       new_duration_seconds=duration*c, seconds_removed=duration*(1-c)),
                  linear_effect=dict(start_playback_multiplier=1/a, end_playback_multiplier=1/(a+b*duration/3600),
                                     new_duration_seconds=float(corrected_time(duration, [a, b])),
                                     seconds_removed=float(duration-corrected_time(duration, [a, b]))))
    sensitivity = {}
    for name, values in [('trim20', [e['trim_frequencies_hz'][0] for e in events]),
                         ('trim60', [e['trim_frequencies_hz'][2] for e in events]),
                         ('mean_phase_frequency', [e['mean_phase_frequency_hz'] for e in events])]:
        sensitivity[name] = robust_fit(design, np.array(values)/nominal).tolist()
    family_model = robust_fit(np.column_stack([design, nominal == 2525]), freq/nominal)
    sensitivity['joint_model_with_start_tone_offset'] = family_model.tolist()
    sensitivity['start_family_offset_equivalent_hz_at_2500'] = float(family_model[2]*2500)
    result['sensitivity'] = sensitivity
    result['hourly'] = []
    for hour in range(int(np.ceil(duration/3600))):
        mask = (times >= hour*3600)&(times < (hour+1)*3600)
        result['hourly'].append(dict(hour=hour, count=int(mask.sum()),
                                    median_speed_error_percent=float(np.median(freq[mask]/nominal[mask]-1)*100) if mask.any() else None))

    # Verify the rendered output by measuring real bursts again, using the map
    # to transform their boundaries. This checks rendering, not model generalization.
    checks = []
    for i in np.linspace(0, len(events)-1, 24).astype(int):
        e = events[i]
        left = max(0., e['start_seconds']-.4)
        length = e['end_seconds']-left+.4
        row = dict(input_seconds=e['start_seconds'], nominal_hz=e['nominal_hz'], before_hz=e['frequency_hz'])
        for name, coef in [('constant', [c, 0.]), ('linear', [a, b])]:
            y, sr = render_excerpt(source, left, length, coef)
            lo = round(float(corrected_time(e['start_seconds'], coef)-corrected_time(left, coef))*1000)
            hi = round(float(corrected_time(e['end_seconds'], coef)-corrected_time(left, coef))*1000)
            row[name+'_hz'] = measure(baseband(y, sr), lo, hi)['frequency_hz']
        checks.append(row)
    result['render_checks'] = checks
    result['render_check_mae_hz'] = {name: float(np.mean([abs(e[name+'_hz']-e['nominal_hz']) for e in checks]))
                                   for name in ('before', 'constant', 'linear')}

    # Three start/end examples spread over the tape, retaining intervening speech.
    pairs = [(x, y) for x, y in zip(events, events[1:])
             if x['kind'] == 'start' and y['kind'] == 'end' and 2 < y['start_seconds']-x['start_seconds'] < 22]
    clips = []
    for name, fraction in [('early', .12), ('middle', .5), ('late', .92)]:
        if not pairs:
            break
        x, y = min(pairs, key=lambda pair: abs(pair[0]['start_seconds']-duration*fraction))
        start = max(0, x['start_seconds']-2)
        length = y['end_seconds']+2-start
        with sf.SoundFile(source) as f:
            f.seek(round(start*f.samplerate))
            before = f.read(round(length*f.samplerate))
            sr = f.samplerate
        sf.write(out/f'{name}_before.wav', before, sr, subtype='FLOAT')
        for label, coef in [('constant', [c, 0.]), ('linear', [a, b])]:
            audio, sr = render_excerpt(source, start, length, coef)
            sf.write(out/f'{name}_{label}.wav', audio, sr, subtype='FLOAT')
        clips.append(dict(name=name, input_start_seconds=start, input_duration_seconds=length,
                          start_tone_hz=x['frequency_hz'], end_tone_hz=y['frequency_hz']))
    result['clips'] = clips
    result['render_method'] = '255-tap Kaiser antialias filter, existing 8x FIR/cubic resampler; fixed gain 1, FLOAT WAV; duration changes; clips only'
    result['limitations'] = [
        'Nominal generator frequencies are not a certified clock; oscillator error and recording-chain frequency shifts are confounded with tape speed.',
        'The historical description also mentions 2474 Hz once in a later paragraph; 2475 Hz is used as the nominal outro. A 1 Hz difference is about 0.04% speed.',
        'Tone families have a small residual disagreement; bootstrap intervals exclude systematic error.',
        'Detector recall is not established; accepted bursts are a quality-selected subset and include substantial residual flutter.',
        f'The maximum clean-burst gap is {result["support"]["maximum_gap_seconds"]/60:.1f} minutes. A linear model interpolates through gaps and extrapolates at the ends.',
        'Held-out block validation supports slow drift on this channel but cannot establish the true clock or arbitrary faster drift.',
        'Any transfer to other channels requires common recording-speed and timeline provenance; experimental deflutter transfer is retained from the source.',
        'Only comparison clips are rendered; the source and production pipeline are unchanged.'
    ]
    (out/'assessment.json').write_text(json.dumps(result, indent=2, allow_nan=False), encoding='utf-8')
    proposal = dict(status='experimental_not_applied_to_full_channel', stage_order='after 05b deflutter',
                    input_source=inventory['source'], input_frames=inventory['frames'],
                    sample_rate=inventory['sample_rate'],
                    forward_map='output_seconds = a * input_seconds + b * input_seconds**2 / 7200',
                    constant=dict(a=c, b=0., output_frames=round(duration*c*inventory['sample_rate'])),
                    linear=dict(a=a, b=b, output_frames=round(float(corrected_time(duration, [a, b]))*inventory['sample_rate'])),
                    support=result['support'],
                    warning='Unverified oscillator accuracy; interpolation/extrapolation is modeled, not measured; validate common speed history before other-channel transfer')
    (out/'proposed_map.json').write_text(json.dumps(proposal, indent=2, allow_nan=False), encoding='utf-8')
    selected = {e['start_seconds']: e for e in events}
    fields = ['start_seconds', 'end_seconds', 'frequency_hz', 'duration_seconds', 'band_power_fraction',
              'amplitude_ratio', 'trim_range_hz', 'residual_fm_sd_hz', 'accepted', 'kind', 'nominal_hz', 'linear_residual_hz']
    with (out/'events.csv').open('w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fields, extrasaction='ignore')
        writer.writeheader()
        for e in inventory['events']:
            writer.writerow(dict(e, **{k: v for k, v in selected.get(e['start_seconds'], {}).items() if k not in e},
                                 accepted=e['start_seconds'] in selected))

    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    fig, axes = plt.subplots(3, 1, figsize=(12, 10), constrained_layout=True)
    grid = np.linspace(0, duration/3600, 300)
    for nominal_hz, color, label in [(2525, '#1769a8', 'Start'), (2475, '#b35b15', 'End')]:
        mask = nominal == nominal_hz
        axes[0].scatter(times[mask]/3600, freq[mask], s=12, alpha=.6, color=color, label=f'{label}: {nominal_hz} Hz nominal')
        axes[0].plot(grid, nominal_hz*(a+b*grid), color=color)
        axes[1].scatter(times[mask]/3600, 100*(freq[mask]/nominal_hz-1), s=12, alpha=.5, color=color)
    axes[0].set(ylabel='Measured frequency (Hz)', title=f'{source_label}: accepted Quindar bursts after defluttering')
    axes[0].legend(loc='upper right')
    axes[1].plot(grid, 100*(a+b*grid-1), color='black', label='Linear drift model')
    axes[1].axhline(100*(c-1), color='purple', linestyle='--', label='Constant model')
    axes[1].set(ylabel='Recorded speed error (%)', title='Both tone families normalized to their own nominal frequency')
    axes[1].legend()
    axes[2].plot(grid, 60*grid-corrected_time(grid*3600, [a, b])/60, label='Linear model', color='black')
    axes[2].plot(grid, 60*grid*(1-c), label='Constant model', linestyle='--', color='purple')
    axes[2].set(ylabel='Time removed (minutes)', xlabel='Input time (hours)', title='Cumulative timing change with the start anchored')
    axes[2].legend()
    for ax in axes:
        ax.set_xlim(0, duration/3600)
        ax.grid(alpha=.2)
    fig.savefig(out/'assessment.svg')
    fig.savefig(out/'assessment.png', dpi=130)
    plt.close(fig)

    constant = result['constant_effect']
    linear = result['linear_effect']
    validations = result['validation']
    rmse_const = np.sqrt(np.mean([x['constant_rmse_hz']**2 for x in validations]))
    rmse_linear = np.sqrt(np.mean([x['linear_rmse_hz']**2 for x in validations]))
    drift_supported = all(v['linear_rmse_hz'] < v['constant_rmse_hz'] for v in validations)
    direction = 'slow' if c < 1 else 'fast'
    drift_verdict = ('A gradual whole-channel correction predicts held-out time blocks better than one fixed multiplier.'
                     if drift_supported else 'The held-out comparison does not consistently favor a gradual correction over one fixed multiplier.')
    family_direction = ('Both families independently show negative drift' if all(f['linear_coefficients'][1] < 0 for f in result['families'])
                        else 'Separate-family drift fits are included in the measurements')
    recommendation = 'the gradual model' if drift_supported else 'the constant model'
    rows = ''.join(f'<tr><td>{html.escape(x["kind"])}</td><td>{x["count"]}</td><td>{x["median_frequency_hz"]:.3f} Hz</td></tr>' for x in result['families'])
    audio_rows = ''.join(f'<section><h3>{clip["name"].title()} — input {hms(clip["input_start_seconds"])}</h3>'+
                         ''.join(f'<label>{label}<audio controls preload="none" src="{clip["name"]}_{name}.wav"></audio></label>'
                                 for name, label in [('before', 'Defluttered source'), ('constant', 'Constant speed correction'), ('linear', 'Gradual speed correction')])+'</section>' for clip in clips)
    limitations = ''.join('<li>'+html.escape(x)+'</li>' for x in result['limitations'])
    page = f'''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Quindar speed assessment · {html.escape(source_label)}</title>
<style>body{{font:17px/1.55 system-ui;max-width:1120px;margin:40px auto;padding:0 24px;color:#192b3c;background:#f7f9fc}}h1,h2{{line-height:1.2}}table{{border-collapse:collapse;width:100%;background:white}}td,th{{padding:12px;text-align:left;border-bottom:1px solid #ccd5df}}img{{width:100%;background:white}}section{{background:white;padding:18px;margin:20px 0}}label{{display:inline-block;margin:8px;vertical-align:top;font-size:14px}}audio{{display:block;width:310px;margin-top:8px}}code{{background:#e7ecf2;padding:2px 5px}}.lede{{font-size:21px}}a{{color:#155d98}}</style>
<h1>Quindar speed assessment · {html.escape(source_label)}</h1>
<p class="lede">The post-deflutter recording is about {abs(100*(c-1)):.2f}% {direction} relative to the published nominal tones.
{drift_verdict} These measurements support an experimental assessment; they do not establish an absolute historical clock.</p>
<p>Analyzed all {hms(duration)} of <code>{html.escape(str(source))}</code>.
Found {result['candidates']} candidate bursts; {len(events)} passed fixed quality gates. The source remains untouched.</p>
<h2>Specification and identification</h2><p>The <a href="https://apollojournals.org/alsj/quindar.html">Apollo Lunar Surface Journal</a> describes a 2,525 Hz start tone and a 2,475 Hz end tone, both 250 ms long, used to key ground transmitters. The journal is linked from <a href="https://www.nasa.gov/history/alsj/quindar.html">NASA’s history site</a>. These are nominal values; the retrieved account does not specify generator accuracy. Its later 2474 Hz mention is an internal inconsistency, not an alternate calibrated standard.</p>
<p>Identify the two frequency groups jointly, then fit their ratios to their respective nominal frequencies. Nearest-nominal classification alone would mislabel some starts on this slow recording. Pair order and the intervening audio in the clips are useful checks; no speech recognition was needed or used.</p>
<table><tr><th>Family</th><th>Accepted bursts</th><th>Observed median</th></tr>{rows}</table>
<p>Raw family medians sample different times and should not be compared as equal-time estimates. A model allowing a separate start-tone offset leaves approximately {abs(family_model[2]*2500):.2f} Hz of disagreement at 2.5 kHz. Generator error, residual flutter and estimator bias remain possible explanations.</p>
<h2>Effect on this channel</h2><table><tr><th></th><th>One fixed correction</th><th>Gradual correction</th></tr>
<tr><td>Playback rate</td><td>{1/c:.8f}× (+{100*(1/c-1):.3f}%)</td><td>+{100*(1/a-1):.3f}% at start → +{100*(linear['end_playback_multiplier']-1):.3f}% at end</td></tr>
<tr><td>Resulting duration</td><td>{hms(duration*c)}</td><td>{hms(linear['new_duration_seconds'])}</td></tr>
<tr><td>Time removed</td><td>{constant['seconds_removed']:.2f} s</td><td>{linear['seconds_removed']:.2f} s</td></tr>
<tr><td>Held-out tone RMSE</td><td>{rmse_const:.2f} Hz</td><td>{rmse_linear:.2f} Hz</td></tr></table>
<p>The constant correction changes pitch by {constant['pitch_cents']:+.2f} cents ({constant['pitch_cents']/100:.2f} semitone) and scales pauses and speech proportionally. The gradual model changes it by approximately {1200*np.log2(1/a):+.1f} to {1200*np.log2(linear['end_playback_multiplier']):+.1f} cents. This is ordinary varispeed: pitch and duration both change. Keeping the existing duration would defeat absolute speed calibration.</p>
<img src="assessment.svg" alt="Frequency measurements, normalized speed error, and cumulative timing changes across the channel">
<h2>Validation and uncertainty</h2><p>Fit on alternating occupied ten-minute blocks and predict the other blocks, then reverse the split. RMSE changes from {validations[0]['constant_rmse_hz']:.2f} to {validations[0]['linear_rmse_hz']:.2f} Hz in one direction and {validations[1]['constant_rmse_hz']:.2f} to {validations[1]['linear_rmse_hz']:.2f} Hz in the other. This is about {100*(1-rmse_linear/rmse_const):.0f}% lower RMSE. {family_direction}. Alternate phase-window fits are retained for sensitivity checks.</p>
<p>The constant playback multiplier has a statistical 95% block-bootstrap interval of {1/result['bootstrap_95_percent']['constant_speed_ratio'][1]:.6f}–{1/result['bootstrap_95_percent']['constant_speed_ratio'][0]:.6f}×. This does not include original oscillator error. The common fitted slope is {b*1e6:.1f} ppm/hour, with a bootstrap interval of {result['bootstrap_95_percent']['linear_slope_per_hour'][0]*1e6:.1f} to {result['bootstrap_95_percent']['linear_slope_per_hour'][1]*1e6:.1f} ppm/hour.</p>
<p>On 24 real burst renders, mean absolute nominal-frequency error changes from {result['render_check_mae_hz']['before']:.2f} Hz to {result['render_check_mae_hz']['constant']:.2f} Hz (constant) and {result['render_check_mae_hz']['linear']:.2f} Hz (gradual). These are renderer checks on fitted data; the held-out results above assess prediction. Synthetic controls verify frequency recovery, correction direction, duration, noise/carrier rejection and single-family refusal.</p>
<h2>Listen to the effect</h2><p>Each comparison covers the same source passage with its own corrected duration. Gain is unchanged. The source is already denoised and defluttered; these clips isolate the additional speed correction. Listening approval remains pending.</p>{audio_rows}
<h2>Proposed post-deflutter process</h2><ol><li>Scan the complete defluttered channel in bounded blocks. Detect quarter-second, concentrated tone bursts over a broad frequency range.</li><li>Measure central phase slope, retain several edge trims, and reject unstable, impure or amplitude-modulated candidates.</li><li>Fit both tone families jointly; compare constant and slow-drift models with held-out time blocks and separate-family fits.</li><li>Export the model and coverage gaps. Apply a single continuous map to the entire aligned channel, including speech between tones. Do not force each individual noisy beep exactly onto nominal.</li><li>For recorded speed <code>s(t) = a + b t/3600</code>, use <code>output_time = a t + b t²/7200</code> and invert this map during resampling. Anchor the start; allow the end to move. Retain the map for transcript/marker alignment.</li><li>Use the same mapping for other channels only after confirming shared speed history. An independent timing reference would test the absolute calibration.</li></ol>
<p>Recommended trial: {recommendation}, after stage 05b. The normal pipeline has not been changed. A future production stage should preserve provenance and write a new file, resample all aligned targets with the same map, transform markers, then normalize after final resampling.</p>
<h2>Limits</h2><ul>{limitations}</ul>
<p>First accepted burst: {hms(times.min())}; last: {hms(times.max())}; largest interior gap: {result['support']['maximum_gap_seconds']/60:.1f} minutes. Tone duration is only a detection aid here: thresholds, filtering, original timer accuracy and residual flutter prevent using it as a precise clock.</p>
<p><a href="assessment.json">Measurements, model and provenance (JSON)</a> · <a href="proposed_map.json">Proposed whole-channel maps (JSON)</a> · <a href="events.csv">All candidate decisions (CSV)</a> · <a href="assessment.png">Exportable chart (PNG)</a></p></html>'''
    (out/'index.html').write_text(page, encoding='utf-8')
    if (source.stat().st_size, source.stat().st_mtime_ns) != (stat.st_size, stat.st_mtime_ns):
        raise ValueError('Source changed during assessment')
    print(json.dumps({k: result[k] for k in ['accepted_bursts', 'constant_effect', 'linear_effect', 'render_check_mae_hz']}, indent=2))
    print(out/'index.html')


if __name__ == '__main__':
    main()
