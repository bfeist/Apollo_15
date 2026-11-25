"""Generate the README methodology figures from published archive artifacts.

Read-only with respect to the archive: reads originals, stage-03/04/05b WAVs,
the qualified maps and analysis records under D:/Apollo_15_30-Track, and
writes PNGs to docs/images/. Detector figures call the production census code
(scripts/30-track/05_deflutter/flutter_census.py) unchanged.

    uv run --with matplotlib python research/readme_figures/collect_stats.py
    uv run --with matplotlib python research/readme_figures/make_figures.py [names...]
"""
import json
import sys
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy import signal

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO/'scripts'))
from _bootstrap import install  # noqa: E402
install()
import flutter_census as census  # noqa: E402

import matplotlib  # noqa: E402
matplotlib.use('Agg')
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch, Patch  # noqa: E402

ROOT = Path('D:/Apollo_15_30-Track')
ANALYSIS = ROOT/'stage_evidence/archive/deflutter_analysis'
IMAGES = REPO/'docs/images'
STATS = json.loads((Path(__file__).with_name('stats.json')).read_text(encoding='utf-8'))
SR = 8000

C_ORIG, C_DEN, C_REM, C_FLUT, C_MAP = '#8c8c8c', '#1f5fa8', '#d9822b', '#b2263a', '#2a9d5c'
plt.rcParams.update({'font.size': 9, 'axes.titlesize': 10, 'axes.labelsize': 9, 'figure.dpi': 150,
                     'axes.spines.top': False, 'axes.spines.right': False, 'savefig.bbox': 'tight',
                     'savefig.pad_inches': .08, 'font.family': 'DejaVu Sans'})


def save(fig, name):
    IMAGES.mkdir(parents=True, exist_ok=True)
    fig.savefig(IMAGES/name, dpi=150, facecolor='white')
    plt.close(fig)
    print('wrote', IMAGES/name)


def plan(tape):
    return json.loads((ROOT/'deflutter_maps'/tape/'plan.json').read_text(encoding='utf-8'))


def channel_row(tape, channel):
    return next(r for r in plan(tape)['inventory']['channels'] if r['channel'] == channel)


def read_wav(path, file_start, t0, seconds):
    """Read [t0, t0+seconds) canonical seconds from a WAV starting at file_start."""
    a = int(round((t0-file_start)*SR))
    x, sr = sf.read(path, start=a, frames=int(round(seconds*SR)), dtype='float64')
    assert sr == SR
    return x


def original(tape, ch, t0, seconds):
    r = channel_row(tape, ch)
    return read_wav(r['path'], r['start_seconds'], t0, seconds)


def stage_audio(stage, tape, ch, t0, seconds):
    """Stage-03 audio divided by its constant gain, or stage-04/05b audio."""
    r = channel_row(tape, ch)
    folder = ROOT/stage/tape/f'CH{ch:02d}'
    if stage == 'denoised':
        gain = json.loads((folder/'denoised.json').read_text(encoding='utf-8'))['gain']
        return read_wav(folder/f'CH{ch:02d}.wav', r['start_seconds'], t0, seconds)/gain
    # Stages 04 and 05b retain the stage-03 gain; undo it so every trace is at original scale.
    name = dict(crosstalk_removed='crosstalk.wav', defluttered_shared='deflutter.wav')[stage]
    gain = json.loads((ROOT/'denoised'/tape/f'CH{ch:02d}'/'denoised.json').read_text(encoding='utf-8'))['gain']
    return read_wav(folder/name, r['start_seconds'], t0, seconds)/gain


def psd_db(x, seconds=4.):
    f, p = signal.welch(x, SR, nperseg=int(seconds*SR), noverlap=int(seconds*SR)//2)
    return f, 10*np.log10(p+1e-20)


# ---------------------------------------------------------------- Figure 1 (recordings)

def fig_recordings():
    inv = json.loads(Path(__file__).with_name('inventory.json').read_text(encoding='utf-8'))
    launch, splash = inv['launch'], inv['splashdown']
    day0 = 207*86400  # 26 July 1971 00:00 UTC
    lanes = {'HR1U': 3.2, 'HR1L': 2.4, 'HR2U': 1.2, 'HR2L': 0.4}
    style = dict(delivered=dict(fc='#2f6fb3', ec='white', hatch=None),
                 unplaced=dict(fc='#9cbfe3', ec='#2f6fb3', hatch='///'),
                 no_signal=dict(fc='#b2263a', ec='white', hatch=None),
                 undelivered=dict(fc='white', ec='#b2263a', hatch='xx'))
    fig, (ax, bx) = plt.subplots(1, 2, figsize=(11, 3.9), gridspec_kw=dict(width_ratios=[3.2, 1], wspace=.22))
    for t in inv['tapes']:
        a, b = t.get('inferred_start', t['start']), t.get('inferred_end', t['end'])
        y = lanes[t['head']]
        s = style[t['status']]
        ax.barh(y, (b-a)/86400, left=(a-day0)/86400, height=.62, color=s['fc'], edgecolor=s['ec'],
                hatch=s['hatch'], lw=.8)
        ax.text((a+b)/2/86400-day0/86400, y, t['tape'][-3:], ha='center', va='center', fontsize=5.6,
                color='white' if t['status'] in ('delivered', 'no_signal') else '#222222')
    for x, label in ((launch, 'launch'), (splash, 'splashdown')):
        ax.axvline((x-day0)/86400, color='k', lw=.8, ls='--')
        ax.text((x-day0)/86400, 3.75, label, ha='center', fontsize=7)
    ax.set_yticks(list(lanes.values()), list(lanes.keys()))
    ax.set_ylim(0, 4)
    ax.set_xlim(-.05, 13.2)
    ticks = np.arange(0, 14, 1)
    ax.set_xticks(ticks, [f'{int(207+d)}' for d in ticks], fontsize=7)
    ax.set_xlabel('1971 day of year (UTC; 207 = 26 July)')
    ax.axhline(1.8, color='#999999', lw=.6)
    for y, label in ((2.8, 'HR1'), (.8, 'HR2')):
        ax.text(-.95, y, f'recorder\n{label}', ha='center', va='center', fontsize=7, color='#555555',
                rotation=90, transform=ax.get_yaxis_transform() if False else ax.transData)
    s = inv['summary']
    ax.set_title(f'40 reels on two side-by-side 30-track recorders. Mission coverage: HR1 '
                 f'{100*s["HR1"]["mission_covered_fraction"]:.0f} %, HR2 '
                 f'{100*s["HR2"]["mission_covered_fraction"]:.0f} % of {inv["mission_hours"]:.0f} h',
                 loc='left', fontsize=9)
    handles = [Patch(facecolor=style[k]['fc'], edgecolor=style[k]['ec'], hatch=style[k]['hatch'], label=l)
               for k, l in (('delivered', 'delivered'), ('unplaced', 'mis-dated box; placement inferred'),
                            ('no_signal', 'no signal (T510096)'), ('undelivered', 'not delivered (T510074)'))]
    ax.legend(handles=handles, frameon=False, fontsize=7, ncol=4, loc='upper left', bbox_to_anchor=(0, -.2))
    for head, color in zip(lanes, ('#1f5fa8', '#6fa0d8', '#d9822b', '#f0b27a')):
        v = [100*(1-t['hum_hz']/60) for t in inv['tapes'] if t['head'] == head and t['hum_hz']]
        y = lanes[head]+np.linspace(-.18, .18, len(v))
        bx.scatter(v, y, s=14, color=color, lw=0)
        bx.plot([np.median(v)]*2, [lanes[head]-.3, lanes[head]+.3], color='k', lw=1.2)
        bx.text(np.median(v), lanes[head]+.36, f'{np.median(v):.2f} %', ha='center', fontsize=6.8)
    bx.set_yticks(list(lanes.values()), list(lanes.keys()))
    bx.set_ylim(0, 4)
    bx.set_xlim(1, 2.8)
    bx.set_xlabel('Playback slow by (%)')
    bx.set_title('Playback speed from the 1971 hum', loc='left', fontsize=9)
    save(fig, 'readme_fig01_recordings.png')


# ---------------------------------------------------------------- Figure 2 (pipeline)

def fig_pipeline():
    fig, ax = plt.subplots(figsize=(10.5, 4.6))
    ax.set_xlim(0, 105)
    ax.set_ylim(0, 46)
    ax.axis('off')

    def box(x, y, w, h, title, body, color, done=True):
        p = FancyBboxPatch((x, y), w, h, boxstyle='round,pad=0.4,rounding_size=1.2',
                           fc=color if done else 'white', ec='#333333' if done else '#999999',
                           lw=1.0, ls='-' if done else '--', alpha=1)
        ax.add_patch(p)
        ax.text(x+w/2, y+h-2.2, title, ha='center', va='top', fontsize=8.6, weight='bold',
                color='#111111' if done else '#777777')
        ax.text(x+w/2, y+h-6.0, body, ha='center', va='top', fontsize=7.0, linespacing=1.25,
                color='#222222' if done else '#888888')

    def arrow(a, b, color='#444444', style='-|>', ls='-', rad=0.):
        ax.add_patch(FancyArrowPatch(a, b, arrowstyle=style, mutation_scale=10, lw=1.1, color=color,
                                     ls=ls, connectionstyle=f'arc3,rad={rad}'))

    audio, timing, later = '#dbe7f5', '#f6e3cf', '#ffffff'
    ax.text(1, 44.5, 'Audio path (what is published)', fontsize=8.5, color=C_DEN, weight='bold')
    ax.text(1, 19.5, 'Timing-evidence path (measured on unprocessed originals)', fontsize=8.5,
            color=C_REM, weight='bold')
    box(1, 26, 14.5, 16, '01 Ingest', 'Verify fragment order,\nconcatenate 39 tapes\n× 30 tracks, 8 kHz\nZIP originals (01a)', audio)
    box(19.5, 26, 15, 16, '03 Denoise', 'Model-based hum\nsubtraction v2.16:\n60·n Hz comb, low\nenvelope, tones', audio)
    box(38.5, 26, 15, 16, '04 Crosstalk', 'Two-path subtraction\nof the weaker ~1.37 s\nrepeat; fixed direction\nper tape/channel', audio)
    box(57.5, 26, 15, 16, '05b Apply map', 'One inverse timing\nmap per tape, same\nfor all channels;\n8× FIR + cubic', audio)
    box(76.5, 26, 13, 16, '06a Transcribe', 'Interim timing\ntranscript of\n05b audio\n(in progress)', later, done=False)
    box(92.5, 26, 12, 16, '06b · 07', 'Retime to the\nmission clock;\nspeech level\n(planned)', later, done=False)
    box(19.5, 2, 15, 15, '02 Capture align', 'Whitened cross-corr.\nat 6 epochs, ±16 s;\nfixed WAV offsets on\nthe tape clock', timing)
    box(38.5, 2, 15, 15, '05 Census', 'Blind cross-channel\ntape-motion tracking\nof on-tape carriers\n(1971 mains hum)', timing)
    box(57.5, 2, 15, 15, '05 Qualify', 'Contradiction flags,\nfollow-up, held-out\nQuindar-tone validation', timing)
    box(76.5, 2, 13, 15, '05a Compile', 'Select, join and\nintegrate once per\ntape: tape_map.npy', timing)
    for x0, x1 in ((15.9, 19.1), (34.9, 38.1), (53.9, 57.1)):
        arrow((x0, 34), (x1, 34))
    arrow((72.9, 34), (76.1, 34), color='#999999', ls='--')
    arrow((89.9, 34), (92.1, 34), color='#999999', ls='--')
    arrow((8, 25.6), (19.1, 9.5), color=C_REM, rad=.25)
    for x0, x1 in ((34.9, 38.1), (53.9, 57.1), (72.9, 76.1)):
        arrow((x0, 9.5), (x1, 9.5), color=C_REM)
    arrow((82.7, 17.4), (68.5, 25.6), color=C_REM, rad=-.15)
    arrow((27, 17.4), (46, 25.6), color=C_REM, ls=':', rad=-.15)
    ax.text(29.6, 22.6, 'offsets', fontsize=6.8, color=C_REM, rotation=0)
    ax.text(77.5, 21.0, 'shared map', fontsize=6.8, color=C_REM)
    save(fig, 'readme_fig02_pipeline.png')


# ---------------------------------------------------------------- Figure 2

def fig_offsets():
    tapes = sorted(t for t, v in STATS['tapes'].items() if 'offsets' in v)
    m = np.full((len(tapes), 30), np.nan)
    for i, t in enumerate(tapes):
        for c, o in STATS['tapes'][t]['offsets'].items():
            m[i, int(c)-1] = o
    fig, ax = plt.subplots(figsize=(10, 4.2))
    shown = np.where(abs(m) > 1e-6, m, np.nan)
    ax.imshow(np.where(np.isnan(m), np.nan, 0), aspect='auto', cmap='Greys', vmin=-1, vmax=4,
              interpolation='nearest')
    im = ax.imshow(shown, aspect='auto', cmap='viridis', interpolation='nearest', vmin=0, vmax=12)
    for i, j in zip(*np.nonzero(~np.isnan(shown))):
        ax.text(j, i, f'{shown[i, j]:.2f}', ha='center', va='center', fontsize=4.6, color='white')
    ax.set_yticks(range(len(tapes)), [t.replace('A15_', '') for t in tapes], fontsize=5.6)
    ax.set_xticks(range(30), [str(c) for c in range(1, 31)], fontsize=6.5)
    ax.set_xlabel('Track (channel)')
    n = int(np.sum(~np.isnan(shown)))
    k = int(np.sum(np.any(~np.isnan(shown), axis=1)))
    ax.set_title(f'Stage 02: measured capture offsets (s). {n} tracks on {k} of {len(tapes)} mapped tapes are '
                 'offset from their filename time.\nGrey = aligned; white = no delivered track', loc='left')
    fig.colorbar(im, ax=ax, fraction=.025, pad=.01, label='offset (s)')
    save(fig, 'readme_fig03_capture_offsets.png')


# ---------------------------------------------------------------- Figure 3

def fig_denoise(tape='A15_T510083', ch=20, t0=14725., seconds=60.):
    x = original(tape, ch, t0, seconds)
    y = stage_audio('denoised', tape, ch, t0, seconds)
    removed = x-y
    fig = plt.figure(figsize=(10.5, 7.2))
    gs = fig.add_gridspec(3, 2, height_ratios=[1, 1, 1.15], hspace=.45, wspace=.18)
    for k, (sig, label) in enumerate(((x, 'Original'), (y, 'Stage 03 (hum subtracted)'))):
        ax = fig.add_subplot(gs[0, k])
        f, t, s = signal.spectrogram(sig, SR, nperseg=4096, noverlap=3072, window='hann')
        keep = f <= 1000
        ax.pcolormesh(t, f[keep], 10*np.log10(s[keep]+1e-14), shading='auto', cmap='magma', vmin=-110, vmax=-45,
                      rasterized=True)
        ax.set_title(f'({"ab"[k]}) {label}, 0–1 kHz', loc='left')
        ax.set_ylabel('Hz')
        ax.set_xlabel(f's after canonical {t0:.0f} s')
    f, a = psd_db(x)
    _, b = psd_db(y)
    _, r = psd_db(removed)
    ax = fig.add_subplot(gs[1, :])
    ax.plot(f, a, color=C_ORIG, lw=.7, label='original')
    ax.plot(f, b, color=C_DEN, lw=.7, label='stage 03')
    ax.plot(f, r, color=C_REM, lw=.5, alpha=.8, label='subtracted model (original − stage 03)')
    ax.set_xlim(0, 4000)
    ax.set_ylim(-130, -40)
    ax.set_ylabel('PSD (dB/Hz)')
    ax.set_xlabel('Hz')
    ax.legend(loc='upper right', frameon=False, ncol=3, fontsize=7.5)
    ax.set_title('(c) Long-term spectrum, 4 s Welch: the subtracted model is a line spectrum on the 60 Hz grid',
                 loc='left')
    for k, (lo, hi) in enumerate(((40, 520), (2900, 3400))):
        ax = fig.add_subplot(gs[2, k])
        sel = (f >= lo) & (f <= hi)
        ax.plot(f[sel], a[sel], color=C_ORIG, lw=.8)
        ax.plot(f[sel], b[sel], color=C_DEN, lw=.8)
        for n in range(int(np.ceil(lo/60)), int(hi//60)+1):
            ax.axvline(60*n, color='#cccccc', lw=.5, zorder=0)
        ax.set_xlim(lo, hi)
        ax.set_xlabel('Hz (grey: exact 60·n Hz)')
        ax.set_ylabel('PSD (dB/Hz)')
        ax.set_title(f'({"de"[k]}) Zoom {lo}–{hi} Hz', loc='left')
    grid = np.array([60*n for n in range(1, 66)])
    idx = [np.argmin(abs(f-g)) for g in grid]
    drop = a[idx]-b[idx]
    fig.suptitle(f'{tape} CH{ch:02d}, canonical {t0:.0f}–{t0+seconds:.0f} s. Median drop at the 60·n Hz '
                 f'grid: {np.median(drop):.1f} dB; off-grid median change: '
                 f'{np.median(np.delete(a-b, idx)):.2f} dB', x=.06, ha='left', y=.995, fontsize=10)
    save(fig, 'readme_fig04_denoise.png')


# ---------------------------------------------------------------- Figure 4

def fig_two_hums(tape='A15_T510083', ch=20, t0=14170., seconds=64.):
    x = original(tape, ch, t0, seconds)
    dec = census.SETTINGS['decimation']
    low = SR/dec
    xd = signal.resample_poly(x, 1, dec)
    lines = census.exact_mains(xd, low, census.SETTINGS, fmax=1250.)
    clean = census.remove_stationary(xd, lines, low, census.SETTINGS['mains']['block_seconds'])
    nper = int(16*low)
    f, p0 = signal.welch(xd, low, nperseg=nper, noverlap=nper//2)
    _, p1 = signal.welch(clean, low, nperseg=nper, noverlap=nper//2)
    fig, axes = plt.subplots(1, 3, figsize=(10.5, 3.3), sharey=True)
    for ax, n in zip(axes, (1, 2, 3)):
        sel = (f > 60*n-4) & (f < 60*n+1.5)
        ax.plot(f[sel], 10*np.log10(p0[sel]), color=C_ORIG, lw=.9, label='original')
        ax.plot(f[sel], 10*np.log10(p1[sel]), color=C_FLUT, lw=.9, label='after exact-line removal')
        ax.axvline(60*n, color='k', lw=.6, ls=':')
        ax.axvspan(60*n*.98, 60*n*.995, color=C_FLUT, alpha=.08)
        ax.set_title(f'{n}×: 60·{n} = {60*n} Hz vs 60·ρ·{n}', loc='left')
        ax.set_xlabel('Hz')
    axes[0].set_ylabel('PSD (dB/Hz), 16 s Welch')
    axes[0].legend(frameon=False, fontsize=7.5, loc='upper left')
    fig.suptitle(f'Two hums on {tape} CH{ch:02d} (canonical {t0:.0f} s, 64 s): a stationary digitization line at '
                 'exactly 60·n Hz (dotted),\nand the 1971 on-tape hum near 60·ρ·n Hz with ρ ≈ 0.987 (shaded band '
                 '0.980–0.995), slowed and FM-spread by flutter', x=.06, ha='left', y=1.08, fontsize=9.5)
    save(fig, 'readme_fig06_two_hums.png')


# ---------------------------------------------------------------- Figure 5

def window_members(tape, read_start, read_end, win_start):
    """Production census stages A–C for one window, returning members, record, consensus."""
    s = census.SETTINGS
    rows = plan(tape)['inventory']['channels']
    tracks, prints = {}, {}
    for r in rows:
        x = read_wav(r['path'], r['start_seconds'], read_start, read_end-read_start).astype(np.float32)
        if not np.any(x):
            continue
        found, _, clean, _ = census.channel_tracks(x, SR, s)
        for m in found:
            m['channel'] = r['channel']
        tracks[r['channel']] = found
        prints[r['channel']] = census.fingerprint(clean, SR/s['decimation'], s)
    groups = census.duplicate_groups(prints, s['gates']['duplicate_r'])
    length = int(round(s['window']['seconds']*census.RATE))
    i0 = int(round((win_start-read_start)*census.RATE))
    members = [(m, m['e'][i0:i0+length], m['ok'][i0:i0+length]) for ch in tracks for m in tracks[ch]]
    record, consensus = census.screen_window(members, groups, s)
    return members, record, consensus


def fig_trace(tape='A15_T510083', win=14170.):
    s = census.SETTINGS
    edge = s['window']['edge_seconds']
    members, record, consensus = window_members(tape, win-8, win+72, win)
    agree = set(record['agreeing_channels'])
    t = win+edge+np.arange(len(consensus))/census.RATE
    data = np.load(ROOT/'deflutter_maps'/tape/'tape_map.npy', mmap_mode='r')
    lo, hi = int(round(t[0]*census.RATE)), int(round(t[-1]*census.RATE))+1
    published = np.array(data[lo:hi, 2])
    fig = plt.figure(figsize=(10.5, 7.6))
    gs = fig.add_gridspec(3, 3, height_ratios=[1.05, 1.1, 1], hspace=.62, wspace=.3)
    # (a) carrier inventory
    ax = fig.add_subplot(gs[0, :2])
    for m, e, ok in members:
        on = m['channel'] in agree and ok.mean() > .5
        ax.scatter(m['centre'], m['channel'], s=10+30*min(1, m['rms_percent']/2.5),
                   color=C_FLUT if on else '#bbbbbb', alpha=.85, lw=0)
    ax.set_xscale('log')
    ax.set_xlabel('Tracked carrier frequency (Hz, log)')
    ax.set_ylabel('Track')
    ax.set_title(f'(a) Carriers tracked in one 64 s window (red: agreeing groups; size ∝ FM)',
                 loc='left')
    ax.axvspan(2150, 2850, color='#999999', alpha=.12)
    ax.text(2450, 15, 'Quindar\nband held\nout', ha='center', fontsize=6.5, color='#555555')
    ax.set_ylim(0, 33)
    # (b) correlation of members against consensus
    ax = fig.add_subplot(gs[0, 2])
    rs = []
    full = np.r_[np.zeros(int(edge*census.RATE)), consensus, np.zeros(int(edge*census.RATE))]
    for m, e, ok in members:
        if ok.mean() > .5:
            rs.append((m['centre'], census.correlation(e[int(edge*census.RATE):len(e)-int(edge*census.RATE)], consensus),
                       m['channel'] in agree))
    rs = np.array(rs, dtype=object)
    for hz, r, on in rs:
        ax.scatter(r, hz, s=9, color=C_FLUT if on else '#bbbbbb', lw=0)
    ax.axvline(.6, color='k', ls=':', lw=.7)
    ax.set_yscale('log')
    ax.set_xlabel('r with consensus')
    ax.set_ylabel('Hz')
    ax.set_title('(b) Each carrier vs consensus', loc='left')
    # (c) overlay of agreeing members, zoom 6 s
    ax = fig.add_subplot(gs[1, :])
    z0, z1 = 20., 26.
    zsel = (t-win >= z0) & (t-win <= z1)
    shown = 0
    for m, e, ok in members:
        if m['channel'] not in agree or ok.mean() < .9:
            continue
        core = e[int(edge*census.RATE):len(e)-int(edge*census.RATE)]
        g = float(core @ consensus/max(consensus @ consensus, 1e-30))
        if census.correlation(core, consensus) < .6 or g < .3:
            continue
        ax.plot(t[zsel]-win, 100*core[zsel]/g, color='#e8a0a8', lw=.5, alpha=.7)
        shown += 1
    ax.plot(t[zsel]-win, 100*consensus[zsel], color=C_FLUT, lw=1.6, label='census consensus')
    ax.plot(t[zsel]-win, 100*published[zsel[:len(published)]] if len(published) >= len(zsel) else [],
            color='k', lw=.8, ls='--', label='published tape map (after qualification, join, DC removal)')
    ax.set_xlim(z0, z1)
    ax.set_ylabel('Speed error (%)')
    ax.set_xlabel(f's after canonical {win:.0f} s')
    ax.legend(frameon=False, loc='upper right', fontsize=7.5)
    ax.set_title(f'(c) {shown} independently demodulated carriers (pink, gain-normalised) on '
                 f'{record["agreeing_groups"] if isinstance(record["agreeing_groups"], int) else len(record["agreeing_groups"])}'
                 ' channel groups agree at zero lag', loc='left')
    # (d) leave-one-channel-group-out validation (the detector's agreement test)
    ax = fig.add_subplot(gs[2, :2])
    loo = record['leave_one_out']
    keys = sorted(loo, key=int)
    r = [loo[k]['r'] for k in keys]
    ok = [k in [str(g) for g in record['agreeing_groups']] for k in keys]
    ax.bar(range(len(keys)), r, color=[C_FLUT if o else '#bbbbbb' for o in ok], width=.75)
    ax.axhline(s['window']['loo_r'], color='k', ls=':', lw=.7)
    ax.set_xticks(range(len(keys)), ['+'.join(str(c) for c in loo[k]['channels']) for k in keys],
                  fontsize=6, rotation=90)
    ax.set_ylim(0, 1)
    ax.set_xlabel('Channel group (duplicate feeds counted once)')
    ax.set_ylabel('r')
    ax.set_title(f'(d) Leave-one-group-out r vs all other groups (gate ≥ {s["window"]["loo_r"]}). '
                 f'RMS {record["consensus_fm_percent"]:.2f} %, peak {record["peak_speed_percent"]:.1f} %',
                 loc='left')
    ax = fig.add_subplot(gs[2, 2])
    f, p = signal.welch(consensus, census.RATE, nperseg=4096)
    db = 10*np.log10(p/p.max())
    ax.plot(f, db, color=C_FLUT, lw=.8)
    ax.set_xlim(0, 35)
    ax.set_ylim(-60, 3)
    ax.axvspan(2, 30, color='#999999', alpha=.12)
    peak = float(f[np.argmax(p)])
    ax.annotate(f'{peak:.1f} Hz', (peak, 0), (peak+4, -8), fontsize=7, arrowprops=dict(arrowstyle='-', lw=.6))
    ax.set_xlabel('Modulation frequency (Hz)')
    ax.set_ylabel('dB')
    ax.set_title('(e) Flutter spectrum', loc='left')
    gates = record['gates']
    fig.suptitle(f'Tracing flutter on {tape}, window {win:.0f}–{win+64:.0f} s: families '
                 f'{", ".join(f"{h:.2f}" for h in record["families"])} Hz; gates '
                 + ', '.join(f'{k} {"✓" if v else "✗"}' for k, v in gates.items()),
                 x=.06, ha='left', y=.995, fontsize=9.5)
    save(fig, 'readme_fig07_trace.png')
    return dict(peak_hz=peak, groups=len(record['agreeing_groups']), carriers=shown,
                families=record['families'], fm=record['consensus_fm_percent'])


# ---------------------------------------------------------------- Figure 6

def inst_freq(x, lo=2200, hi=2800):
    sos = signal.butter(6, [lo, hi], btype='bandpass', fs=SR, output='sos')
    z = signal.hilbert(signal.sosfiltfilt(sos, x))
    f = np.gradient(np.unwrap(np.angle(z)))*SR/(2*np.pi)
    return abs(z), f


def fig_beeps(tape='A15_T510083', ch=20, beeps=(14190.191, 14212.068, 14220.241)):
    data = np.load(ROOT/'deflutter_maps'/tape/'tape_map.npy', mmap_mode='r')
    fig, axes = plt.subplots(3, len(beeps), figsize=(10.5, 6.6), sharey='row',
                             gridspec_kw=dict(height_ratios=[1, 1, .9], hspace=.45))
    for k, b in enumerate(beeps):
        i = int(round(b*census.RATE))
        out = float(data[i, 1])  # output time of this source instant
        pre = stage_audio('crosstalk_removed', tape, ch, b-.15, .6)
        post = stage_audio('defluttered_shared', tape, ch, out-.15, .6)
        for row, (x, label) in enumerate(((pre, 'stage 04 (before)'), (post, 'stage 05b (after)'))):
            ax = axes[row, k]
            f, t, s = signal.spectrogram(x, SR, nperseg=256, noverlap=240, nfft=2048, window='hann')
            sel = (f > 2250) & (f < 2750)
            ax.pcolormesh(t*1000, f[sel], 10*np.log10(s[sel]+1e-14), shading='auto', cmap='magma',
                          vmin=-100, vmax=-40, rasterized=True)
            if row == 0:
                ax.set_title(f'Quindar tone at {b:.2f} s\n{label}', loc='left', fontsize=8.5)
            else:
                ax.set_title(label, loc='left', fontsize=8.5)
            if k == 0:
                ax.set_ylabel('Hz')
        ax = axes[2, k]
        for x, c, label in ((pre, C_ORIG, 'before'), (post, C_MAP, 'after')):
            amp, f = inst_freq(x)
            on = amp > .35*np.percentile(amp, 99)
            tt = np.arange(len(x))/SR*1000
            ff = np.where(on, f, np.nan)
            ff = np.where(abs(ff-np.nanmedian(ff)) < 200, ff, np.nan)
            core = ff[np.isfinite(ff)]
            core = core[len(core)//8:-len(core)//8] if len(core) > 16 else core
            ax.plot(tt, ff, color=c, lw=.9, label=f'{label}: RMS dev {np.std(core):.0f} Hz')
        ax.legend(frameon=False, fontsize=7, loc='lower center')
        ax.set_xlabel('ms')
        if k == 0:
            ax.set_ylabel('Instantaneous Hz')
    fig.suptitle(f'Held-out Quindar tones on {tape} CH{ch:02d}. The tones never enter the census; the same tape map '
                 'removes the frequency wobble', x=.06, ha='left', y=.995, fontsize=9.5)
    save(fig, 'readme_fig09_quindar.png')


# ---------------------------------------------------------------- Figure 7

def current_events():
    """Held-out physical beep events from the analyses bound to the published maps."""
    rows = []
    for tape, v in STATS['tapes'].items():
        if 'analysis_id' not in v or not v['analysis_id']:
            continue
        a = ANALYSIS/v['run_id']/v['analysis_id']
        dec = json.loads((a/'qualification'/tape/'decisions.json').read_text(encoding='utf-8'))
        applied = np.array([[s['start_seconds'], s['end_seconds']] for s in dec['segments']
                            if s['decision'] == 'apply'])
        with open(a/'events.jsonl', encoding='utf-8') as f:
            for line in f:
                if f'"{tape}@' not in line:
                    continue
                e = json.loads(line)
                if e['tape'] != tape or e['partition'] != 'held_out' or e.get('ratio') is None:
                    continue
                inside = len(applied) and np.any((applied[:, 0] <= e['start']) & (e['start'] <= applied[:, 1]))
                rows.append((e['identity_hz'], e['curve_hz'], e['ratio'], e.get('wrong_sign_ratio'),
                             e.get('shift_ratio'), bool(inside), v.get('partition')))
    return rows


def fig_validation():
    rows = current_events()
    ok = [r for r in rows if r[5]]
    ident = np.array([r[0] for r in ok])
    curve = np.array([r[1] for r in ok])
    ratio = np.array([r[2] for r in ok])
    wrong = np.array([r[3] for r in ok if r[3] is not None])
    shift = np.array([r[4] for r in ok if r[4] is not None])
    fig, axes = plt.subplots(1, 2, figsize=(10.5, 3.9), gridspec_kw=dict(wspace=.28))
    ax = axes[0]
    bins = np.logspace(-1.6, 1.0, 70)
    ax.hist(ratio, bins, color=C_MAP, alpha=.85, label=f'tape map  (median {np.median(ratio):.2f})')
    ax.hist(shift, bins, histtype='step', color='#555555', lw=1.1,
            label=f'map shifted 1.5 s  ({np.median(shift):.2f})')
    ax.hist(wrong, bins, histtype='step', color=C_FLUT, lw=1.1,
            label=f'map sign-reversed  ({np.median(wrong):.2f})')
    ax.axvline(1, color='k', lw=.7, ls=':')
    ax.set_xscale('log')
    ax.set_xlabel('Quindar-tone FM after / before (log)')
    ax.set_ylabel('Held-out physical events')
    ax.legend(frameon=False, fontsize=7.5, loc='upper left')
    ax.set_title(f'(a) {len(ok):,} held-out events in applied intervals; '
                 f'{100*np.mean(ratio > 1):.1f} % worse', loc='left')
    ax = axes[1]
    h = ax.hexbin(ident, curve, gridsize=60, extent=(0, 140, 0, 140), bins='log', cmap='Greens', mincnt=1)
    ax.plot([0, 140], [0, 140], color='k', lw=.7, ls=':')
    ax.set_xlabel('RMS tone frequency deviation, uncorrected (Hz)')
    ax.set_ylabel('… after the tape map (Hz)')
    ax.set_title(f'(b) Median {np.median(ident):.1f} Hz → {np.median(curve):.1f} Hz', loc='left')
    fig.colorbar(h, ax=ax, fraction=.04, pad=.01, label='events (log)')
    fig.suptitle('Validation on Quindar tones the detector never saw (2525/2475 Hz, on any channel)',
                 x=.06, ha='left', y=1.0, fontsize=9.5)
    save(fig, 'readme_fig10_validation.png')
    return len(ok), float(np.median(ratio)), float(np.mean(ratio > 1)), float(np.median(ident)), float(np.median(curve))


# ---------------------------------------------------------------- Figure 8

def fig_coverage():
    tapes = sorted(t for t, v in STATS['tapes'].items() if 'selection_seconds' in v)
    kinds = [('census', 'qualified census', C_FLUT), ('saved', 'saved tone evidence (regional / v5)', C_REM),
             ('excluded', 'bypass: unresolved contradiction', '#9a9a9a'), ('unsupported', 'bypass: no evidence', '#dddddd')]
    fig, ax = plt.subplots(figsize=(10.5, 4.3))
    left = np.zeros(len(tapes))
    for key, label, color in kinds:
        v = np.array([STATS['tapes'][t]['selection_seconds'][key]/3600 for t in tapes])
        ax.bar(range(len(tapes)), v, bottom=left, color=color, label=label, width=.82)
        left += v
    for i, t in enumerate(tapes):
        ax.text(i, left[i]+.15, f'{100*STATS["tapes"][t]["coverage"]:.0f}', ha='center', fontsize=5.6)
    ax.set_xticks(range(len(tapes)), [t.replace('A15_T5100', '') .replace('A15_T5101', '1') for t in tapes],
                  fontsize=6.3)
    ax.set_xlabel('Tape (last digits of A15_T5100xx / T5101xx; T510096 excluded as a duplicate feed); '
                  'number above bar = corrected %')
    ax.set_ylabel('Tape hours')
    tot = sum(STATS['tapes'][t]['duration'] for t in tapes)
    sup = sum(STATS['tapes'][t]['supported'] for t in tapes)
    ax.set_title(f'Shared-map coverage: {sup/3600:.0f} of {tot/3600:.0f} tape-hours corrected '
                 f'({100*sup/tot:.1f} %, union of tape time) on {len(tapes)} tapes', loc='left')
    ax.legend(frameon=False, fontsize=7.5, ncol=4, loc='upper left', bbox_to_anchor=(0, -.17))
    save(fig, 'readme_fig11_coverage.png')


# ---------------------------------------------------------------- Figure 9

def fig_map(tape='A15_T510083', zoom=(14190., 14200.)):
    data = np.load(ROOT/'deflutter_maps'/tape/'tape_map.npy', mmap_mode='r')
    step = 200  # 1 s
    t = np.array(data[::step, 0])/3600
    disp = (np.array(data[::step, 1])-np.array(data[::step, 0]))*1000
    speed = np.array(data[:, 2])
    support = np.array(data[::step, 3])
    n = len(speed)//(60*200)
    rms = np.sqrt(np.mean(speed[:n*60*200].reshape(n, -1)**2, axis=1))*100
    fig, axes = plt.subplots(3, 1, figsize=(10.5, 6.4), gridspec_kw=dict(hspace=.55))
    ax = axes[0]
    ax.fill_between(t, 0, support, color=C_MAP, alpha=.25, step='mid', lw=0)
    ax.plot((np.arange(n)+.5)/60, rms, color=C_FLUT, lw=.8)
    ax.set_ylabel('RMS speed corr. (%)\n per minute')
    ax.set_xlim(0, t[-1])
    ax.set_title(f'(a) {tape}: correction strength; green = support (coverage '
                 f'{100*STATS["tapes"][tape]["coverage"]:.1f} %), gaps bypass with zero local speed change',
                 loc='left')
    ax = axes[1]
    ax.plot(t, disp, color='k', lw=.6)
    ax.set_xlim(0, t[-1])
    ax.set_ylabel('Displacement (ms)')
    ax.set_xlabel('Canonical tape hours')
    ax.set_title('(b) Integrated once over the whole tape: nominal − tape time; DC speed removed so '
                 'endpoints stay anchored', loc='left')
    ax = axes[2]
    a, b = (int(z*200) for z in zoom)
    tz = np.array(data[a:b, 0])
    ax.plot(tz-zoom[0], 100*np.array(data[a:b, 2]), color=C_FLUT, lw=.8, label='speed error (%)')
    ax2 = ax.twinx()
    ax2.plot(tz-zoom[0], 1000*(np.array(data[a:b, 1])-tz), color='k', lw=.8, label='displacement (ms)')
    ax2.spines['right'].set_visible(True)
    ax2.set_ylabel('Displacement (ms)')
    ax.set_ylabel('Speed (%)', color=C_FLUT)
    ax.set_xlabel(f's after canonical {zoom[0]:.0f} s')
    ax.set_title('(c) Ten-second detail at 200 Hz: speed error and its integral (the timing map)', loc='left')
    save(fig, 'readme_fig08_tape_map.png')


# ---------------------------------------------------------------- Figure 10

def fig_crosstalk(tape='A15_T510088', ch=24, event_start=23165.75):
    d = json.loads((ROOT/'crosstalk_removed'/tape/f'CH{ch:02d}'/'crosstalk.json').read_text(encoding='utf-8'))
    events = sorted((e for e in d['events'] if e['status'] == 'applied'), key=lambda e: e['application_interval'][0])
    first = min(events, key=lambda e: abs(e['application_interval'][0]-event_start))
    run = [first]
    for e in events:
        if e['application_interval'][0] > run[-1]['application_interval'][0] and                 e['application_interval'][0]-run[-1]['application_interval'][1] <= .3 and e['donor'] == first['donor']:
            run.append(e)
    a, b = run[0]['application_interval'][0], run[-1]['application_interval'][1]
    lag = float(np.median([e['echo_to_main_seconds'] for e in run]))
    donor_at = a+float(np.median([np.median(e['echo_tracking']['donor_minus_target_seconds']) for e in run]))
    sign = 1 if d['direction'] == 'earlier' else -1  # earlier: the unwanted copy precedes the main utterance
    main = (a+sign*lag, b+sign*lag)
    t0 = min(a, main[0], donor_at)-.6
    span = max(b, main[1], donor_at+b-a)-t0+.6
    donor = stage_audio('denoised', tape, first['donor'], t0, span)
    before = stage_audio('denoised', tape, ch, t0, span)
    after = stage_audio('crosstalk_removed', tape, ch, t0, span)
    tt = np.arange(len(before))/SR
    fig = plt.figure(figsize=(10.5, 7.0))
    gs = fig.add_gridspec(4, 2, width_ratios=[2.3, 1], height_ratios=[1, 1, 1.1, 1.1], hspace=.75, wspace=.25)

    def mark(ax, labels=False, lim=None):
        ax.axvspan(a-t0, b-t0, color=C_REM, alpha=.15)
        ax.axvspan(main[0]-t0, main[1]-t0, color=C_DEN, alpha=.12)
        if labels:
            ax.text((a+b)/2-t0, lim*1.02, 'weaker copy: subtract', ha='center', va='bottom', fontsize=7, color=C_REM)
            ax.text(sum(main)/2-t0, lim*1.02, 'main utterance: keep', ha='center', va='bottom', fontsize=7, color=C_DEN)

    ax = fig.add_subplot(gs[0, 0])
    ax.plot(tt, donor, color=C_ORIG, lw=.4)
    ax.axvspan(donor_at-t0, donor_at+b-a-t0, color=C_ORIG, alpha=.2)
    ax.set_xlim(0, tt[-1])
    ax.set_title(f'(a) Donor CH{first["donor"]:02d} (stage 03); shaded: speech matched to both target occurrences',
                 loc='left', fontsize=8.5)
    ax = fig.add_subplot(gs[1, 0])
    lim = 1.15*np.max(abs(before))
    ax.plot(tt, before, color=C_DEN, lw=.4)
    ax.set_ylim(-lim, lim)
    mark(ax, True, lim)
    ax.set_xlim(0, tt[-1])
    ax.set_title(f'(b) Target CH{ch:02d} before (stage 03)', loc='left', fontsize=8.5, pad=12)
    ax = fig.add_subplot(gs[2, 0])
    z = (tt >= a-t0-.15) & (tt <= b-t0+.15)
    zl = 1.15*np.max(abs(before[z]))
    ax.plot(tt[z], before[z]+zl, color=C_DEN, lw=.5, label='before (offset up)')
    ax.plot(tt[z], after[z]-zl, color=C_MAP, lw=.5, label='after, stage 04 (offset down)')
    ax.set_ylim(-2.2*zl, 2.2*zl)
    ax.set_yticks([])
    ax.axvspan(a-t0, b-t0, color=C_REM, alpha=.10)
    inside = (tt >= a-t0) & (tt <= b-t0)
    change = 10*np.log10(np.sum(after[inside]**2)/np.sum(before[inside]**2))
    ax.text(tt[z][0], 1.75*zl, 'before', fontsize=7, color=C_DEN)
    ax.text(tt[z][0], -1.95*zl, 'after (stage 04)', fontsize=7, color=C_MAP)
    ax.set_title(f'(c) Zoom on the subtracted interval: {change:+.1f} dB energy change (80 ms fades at ends)',
                 loc='left', fontsize=8.5)
    ax = fig.add_subplot(gs[3, 0])
    f, t, s0 = signal.spectrogram(before, SR, nperseg=256, noverlap=192)
    _, _, s1 = signal.spectrogram(after, SR, nperseg=256, noverlap=192)
    diff = 10*np.log10((s0+1e-14)/(s1+1e-14))
    im = ax.pcolormesh(t, f, diff, shading='auto', cmap='RdBu_r', vmin=-12, vmax=12, rasterized=True)
    mark(ax)
    ax.set_xlim(0, tt[-1])
    ax.set_ylabel('Hz')
    ax.set_xlabel(f's after canonical {t0:.2f} s')
    ax.set_title('(d) Energy change, dB (red: removed, blue: added); exactly zero outside the interval',
                 loc='left', fontsize=8.5)
    fig.colorbar(im, ax=ax, fraction=.02, pad=.01)
    ax = fig.add_subplot(gs[0:2, 1])
    delays = {'earlier': [], 'later': []}
    for r in STATS['crosstalk']:
        if r['direction'] in delays:
            delays[r['direction']] += [abs(x) for x in r['delays'] if x is not None]
    bins = np.linspace(1.33, 1.42, 60)
    ax.hist(delays['earlier'], bins, color=C_DEN, alpha=.7, label=f'remove earlier ({len(delays["earlier"]):,})')
    ax.hist(delays['later'], bins, color=C_REM, alpha=.7, label=f'remove later ({len(delays["later"]):,})')
    ax.set_xlabel('|copy − main| separation (s)')
    ax.set_ylabel('applied events')
    ax.legend(frameon=False, fontsize=7)
    ax.set_title('(e) Measured separations, archive', loc='left', fontsize=8.5)
    ax = fig.add_subplot(gs[2:4, 1])
    rows = [r for r in STATS['crosstalk'] if r['applied_events']]
    v = np.sort([r['applied'] for r in rows])[::-1]
    ax.bar(range(len(v)), v/60, color=C_MAP, width=1.)
    ax.set_yscale('log')
    ax.set_xlabel('channels with applied events (ranked)')
    ax.set_ylabel('minutes subtracted')
    ax.set_title(f'(f) {len(rows)} of {len(STATS["crosstalk"])} channels, {sum(v)/3600:.1f} h total',
                 loc='left', fontsize=8.5)
    fig.suptitle(f'Stage 04 on {tape} CH{ch:02d}: direction "{d["direction"]}", copy {lag:.3f} s from the main '
                 f'utterance, {len(run)} consecutive evidenced events', x=.06, ha='left', y=.995, fontsize=9.5)
    save(fig, 'readme_fig05_crosstalk.png')


FIGURES = dict(recordings=fig_recordings, pipeline=fig_pipeline, offsets=fig_offsets, denoise=fig_denoise, hums=fig_two_hums,
               trace=fig_trace, beeps=fig_beeps, validation=fig_validation, coverage=fig_coverage,
               map=fig_map, crosstalk=fig_crosstalk)

if __name__ == '__main__':
    for name in sys.argv[1:] or FIGURES:
        print(name, FIGURES[name]())
