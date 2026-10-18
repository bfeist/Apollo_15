"""Plot a saved Quindar anchor curve and update the existing assessment page."""
import argparse
import json
from pathlib import Path

import numpy as np
from scipy.interpolate import PchipInterpolator


def plot_curve(model, output):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    output = Path(output)
    anchors = model['anchors']
    t = np.array([e['input_seconds'] for e in anchors])
    measured = np.array([e['measured_speed_ratio'] for e in anchors])
    smoothed = np.array([e['smoothed_speed_ratio'] for e in anchors])
    nominal = np.array([e['nominal_hz'] for e in anchors])
    duration = model['input_duration_seconds']
    curve = PchipInterpolator(model['input_seconds'], model['recorded_speed_ratio'])
    integral = curve.antiderivative()
    grid = np.linspace(0, duration, 1500)
    a, b = model['linear_coefficients']
    linear = a+b*grid/3600
    fig, axes = plt.subplots(3, 1, figsize=(13, 11), constrained_layout=True)
    for hz, color, label in [(2525., '#1769a8', 'Start'), (2475., '#b35b15', 'End')]:
        mask = nominal == hz
        axes[0].scatter(t[mask]/3600, measured[mask]*hz, s=15, color=color, alpha=.32, label=f'{label}: measured beeps')
        axes[0].plot(grid/3600, curve(grid)*hz, color=color, linewidth=2)
        axes[0].scatter(t[mask]/3600, smoothed[mask]*hz, s=19, facecolor='white', edgecolor=color, linewidth=.9, label=f'{label}: smoothed anchors')
    axes[0].set(title='Quindar measurements and smoothed anchors at each accepted occurrence', ylabel='Frequency (Hz)')
    axes[0].legend(ncol=2, fontsize=9, loc='upper right')
    axes[1].scatter(t/3600, (1/measured-1)*100, s=13, color='#8a939f', alpha=.32, label='Raw single-beep estimates (residual flutter)')
    axes[1].plot(grid/3600, (1/curve(grid)-1)*100, color='#13704a', linewidth=2.5, label='Applied curve through smoothed anchors')
    axes[1].scatter(t/3600, (1/smoothed-1)*100, s=18, facecolor='white', edgecolor='#13704a', linewidth=.8, zorder=3, label=f'{len(t)} anchor locations')
    axes[1].plot(grid/3600, (1/linear-1)*100, color='#8d429c', linestyle='--', linewidth=1.5, label='Earlier global linear-speed fit')
    axes[1].set(title='Stage 05c: varying playback correction, smoothed across neighbouring tones', ylabel='Playback speed increase (%)')
    axes[1].legend(fontsize=9, loc='upper left')
    axes[2].plot(grid/3600, (grid-(integral(grid)-integral(0)))/60, color='#13704a', linewidth=2.5, label='Anchored curve')
    axes[2].plot(grid/3600, (grid-a*grid-b*grid*grid/7200)/60, color='#8d429c', linestyle='--', label='Earlier global linear-speed fit')
    axes[2].set(title='Resulting cumulative timing change (start remains anchored)', xlabel='Defluttered input time (hours)', ylabel='Time removed (minutes)')
    axes[2].legend()
    gaps = [(0., t[0]), (t[-1], duration)]+[(x, y) for x, y in zip(t, t[1:]) if y-x > 1800]
    for ax in axes:
        for left, right in gaps:
            ax.axvspan(left/3600, right/3600, color='#dce1e8', alpha=.35, zorder=0)
        ax.grid(alpha=.18)
        ax.set_xlim(0, duration/3600)
    fig.suptitle('Quindar-anchored retiming: raw frequencies are smoothed before setting speed', fontsize=14)
    fig.savefig(output/'anchored_curve.svg')
    fig.savefig(output/'anchored_curve.png', dpi=130)
    plt.close(fig)


def update_report(page, model):
    text = page.read_text(encoding='utf-8')
    start = '<!-- anchored-curve-start -->'
    end = '<!-- anchored-curve-end -->'
    values = np.asarray(model['recorded_speed_ratio'])
    removed = model['input_duration_seconds']-model['output_duration_seconds']
    replacement = f'''{start}<section><h2>Updated approach: a curve anchored at the Quindar occurrences</h2>
<p><strong>Stage 05c uses {len(model['anchors'])} anchor locations</strong>, one at each accepted tone. Each anchor's speed is estimated by robustly pooling neighbouring start/end measurements after dividing by their respective nominal frequencies. A shape-preserving cubic curve joins the smoothed speed anchors. Integrating that curve gives the audio timing map.</p>
<p>Open circles below are the actual smoothed anchors; faint points are raw beep estimates. The dashed purple line is the earlier global linear-speed fit. Grey shading marks gaps longer than 30 minutes and the unsupported edges. Speed is held at the nearest smoothed value beyond the first and last anchors.</p>
<p>Held-out ten-minute-block comparison selected a {model['kernel_sigma_seconds']/60:.0f}-minute Gaussian smoothing scale (not spacing between anchors). This broad smoothing reflects the substantial residual flutter; the data do not justify following each raw frequency wiggle. The anchored curve's validation RMSE is {model['chosen_validation']['rmse_hz']:.2f} Hz, similar to the earlier linear baseline.</p>
<p>The current curve applies approximately +{100*(1/max(values)-1):.3f}% to +{100*(1/min(values)-1):.3f}% playback correction and removes {removed:.2f} seconds ({removed/60:.2f} minutes). The constant/linear figures and listening clips elsewhere on this page are retained as earlier comparisons; they are not measurements of the new anchored render.</p>
<img src="anchored_curve.svg" alt="Raw Quindar frequencies, smoothed anchors at each tone, a varying speed correction curve and cumulative timing change">
<p><a href="anchored_curve.json">All anchor values and validation (JSON)</a> · <a href="anchored_curve.png">Updated chart (PNG)</a></p></section>{end}'''
    if start in text:
        lo, hi = text.index(start), text.index(end)+len(end)
        text = text[:lo]+replacement+text[hi:]
    else:
        image = '<img src="assessment.svg" alt="Frequency measurements, normalized speed error, and cumulative timing changes across the channel">'
        if image not in text:
            raise ValueError('Expected assessment graph not found')
        text = text.replace(image, replacement)
        text = text.replace('<h2>Effect on this channel</h2>', '<h2>Earlier constant and linear comparison</h2>')
        text = text.replace('<h2>Proposed post-deflutter process</h2>', '<h2>Earlier linear-model process (superseded by the anchored curve above)</h2>')
        text = text.replace('Recommended trial: the gradual model, after stage 05b.', 'Current trial: the Quindar-anchored curve in stage 05c, after stage 05b.')
        text = text.replace('<h1>Quindar speed assessment', '<p><strong>Updated: stage 05c now uses a smoothed curve anchored at each accepted Quindar occurrence; see the updated graph below.</strong></p><h1>Quindar speed assessment')
    page.write_text(text, encoding='utf-8')


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--model', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--update-report', type=Path)
    args = p.parse_args()
    model = json.loads(args.model.read_text())
    plot_curve(model, args.output)
    if args.update_report:
        update_report(args.update_report, model)


if __name__ == '__main__':
    main()
