"""Render documentation charts from local reports; never write or copy audio.

From the repository root: uv run --with matplotlib python docs/render_readme_charts.py
Requires the ignored local reports named below. Only PNGs are exported.
"""
import json
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np
import soundfile as sf
from scipy.signal import welch

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'docs/images'
plt.rcParams.update({'font.size': 11, 'axes.spines.top': False,
                     'axes.spines.right': False, 'figure.facecolor': 'white'})


def reference_search():
    report = ROOT / 'reports/regional_deflutter_T510083_audit/r00000.json'
    data = json.loads(report.read_text(encoding='utf-8'))
    candidates = data['candidates']
    fig, ax = plt.subplots(figsize=(12, 5.5), layout='constrained')
    points = ax.scatter([c['channel'] for c in candidates],
                        [c['carrier_hz'] for c in candidates],
                        c=[c['contrast_db'] for c in candidates],
                        cmap='viridis', s=48, alpha=.85)
    refs = sorted({(v['reference_channel'], v['carrier_hz']) for v in data['variants']})
    ax.scatter([r[0] for r in refs], [r[1] for r in refs],
               facecolors='none', edgecolors='#da5828', s=190, linewidths=2,
               label='References evaluated with correction variants')
    ax.set(xticks=range(1, 31), xlim=(.5, 30.5), yscale='log',
           xlabel='Original channel (CH01 inventoried separately as IRIG)',
           ylabel='Candidate carrier frequency (Hz; log scale)',
           title='Searching across channels for reference tones\n'
                 'T510083 | 27,090–27,150 s | 116 candidates from 29 scanned channels')
    ax.grid(alpha=.18)
    ax.legend(loc='upper left', fontsize=9)
    fig.colorbar(points, ax=ax, label='Spectral contrast (dB)')
    fig.savefig(OUT / 'reference-tone-search.png', dpi=160)
    plt.close(fig)


def denoising():
    folder = ROOT / 'reports/noise_review_20261017_speech'
    report = json.loads((folder / 'results.json').read_text(encoding='utf-8'))
    sample = next(s for s in report['samples'] if s['id'] == '01_A15_T510073_CH6')
    spectra = []
    for key in ['off_b0', 'full_b0', 'full_b3']:
        audio, rate = sf.read(folder / sample['files'][key]['audio'])
        assert rate == sample['sample_rate'] and len(audio) == sample['frames']
        freq, power = welch(audio, rate, nperseg=rate * 2)
        spectra.append(10 * np.log10(np.maximum(power, 1e-20)))
    fig, axes = plt.subplots(2, 1, figsize=(12, 8))
    fig.subplots_adjust(left=.09, right=.98, top=.89, bottom=.08, hspace=.46)
    for power, label, color in zip(spectra,
            ['Original', 'Tones + hum removed', 'Then broadband reduction (3 dB cap)'],
            ['#89939f', '#c77815', '#177b60']):
        axes[0].plot(freq, power, color=color, lw=.8, label=label)
    axes[0].set(xlim=(0, 2000), ylabel='Power density (dBFS/Hz)',
                title='Denoising in two steps: narrow peaks first, then broadband noise\n'
                      'T510073 CH06 | 30-second review excerpt | identical playback gain')
    axes[0].legend(fontsize=9, loc='lower right')
    reduction = spectra[1] - spectra[2]
    reduction[spectra[1] < -90] = np.nan
    axes[1].plot(freq, reduction, color='#177b60', lw=.9)
    axes[1].axhline(3, color='#7d8590', ls='--', lw=1, label='Configured 3 dB cap')
    axes[1].set(xlim=(0, 4000), xlabel='Audio frequency (Hz)',
                ylabel='Additional reduction (dB)',
                title='Broadband step alone: tonal-only spectrum minus final spectrum\n'
                      'Bins below -90 dBFS/Hz after tonal cleanup omitted (unstable ratios)')
    axes[1].legend(fontsize=9)
    for ax in axes:
        ax.grid(alpha=.18)
    fig.savefig(OUT / 'denoising-spectrum.png', dpi=160)
    plt.close(fig)


if __name__ == '__main__':
    OUT.mkdir(parents=True, exist_ok=True)
    reference_search()
    denoising()
