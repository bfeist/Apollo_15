"""E7: per-channel leave-one-channel-out agreement of blind (E4) candidates.

For each window: E4 candidates (stationary digitization lines removed first,
wide captures). Seed cluster = candidates agreeing with the best-connected one.
For every channel c with a cluster member: predict c's best member from the
coherence-weighted mean of members on OTHER channels; report r and gain.
A channel "agrees" if r >= 0.7 and 0.6 <= gain <= 1.6. Window eligible if >= 3
channels agree and the agreeing members span >= 2 frequency families (lines
>= 2.5 % apart in frequency and not harmonics of one another).
"""
import json, os, sys, time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
import numpy as np
sys.path.insert(0, str(Path(__file__).parent))
from common import RATE, SR, corr, inventory, read, demod, frac_dev, exact_mains, remove_stationary
from e4_blind_seed import candidates, track
from e3_consensus import same_source


def window(tape, t0, dur=64., per_ch=10, r_min=.6):
    t = time.time()
    C = []
    for ch in sorted(inventory(tape)):
        x = read(tape, ch, t0, dur)
        if not x.any():
            continue
        x = remove_stationary(x, exact_mains(x))
        for r in candidates(x, per_ch):
            e, ok, m = track(x, r['hz'])
            if m['support'] < .5:
                continue
            m.update(ch=ch); m['e'] = e
            C.append(m)
    edge = slice(4 * RATE, -4 * RATE)
    n = len(C)
    out = dict(tape=tape, t0=t0, dur=dur, n_candidates=n)
    if n < 2:
        out.update(eligible=False, agreeing_channels=[], seconds=time.time() - t)
        return out, None
    M = np.array([c['e'][edge] for c in C])
    Mn = (M - M.mean(1, keepdims=True)) / (M.std(1, keepdims=True) + 1e-12)
    R = Mn @ Mn.T / M.shape[1]
    np.fill_diagonal(R, 0)
    chs = np.array([c['ch'] for c in C])
    other = chs[:, None] != chs[None, :]
    # several agreement clusters; tape motion = the cluster with the LARGEST
    # common FM (post-tape common-mode combs carry ~0.1 % FM, tape ~2 %)
    alive = np.ones(n, bool)
    clusters = []
    for _ in range(4):
        deg = ((R > r_min) & other & alive[None, :] & alive[:, None]).sum(1)
        if deg.max() < 2:
            break
        s0 = int(np.argmax(deg))
        mem0 = [s0] + [j for j in range(n) if alive[j] and R[s0, j] > r_min]
        clusters.append((s0, mem0, float(np.median([C[j]['rms_pct'] for j in mem0]))))
        alive[mem0] = False
    out['clusters'] = [dict(seed_ch=int(chs[a]), seed_hz=round(C[a]['centre'], 2), size=len(m), median_rms_pct=round(r_, 3))
                       for a, m, r_ in clusters]
    if not clusters:
        out.update(eligible=False, agreeing_channels=[], seconds=time.time() - t)
        return out, None
    s, mem, _ = max(clusters, key=lambda c: c[2] if len(c[1]) >= 3 else -1)
    es = M[s]
    norm = {}
    for j in mem:
        g = 1. if j == s else float(M[j] @ es / (es @ es))
        rj = .95 if j == s else min(R[s, j], .95)
        if g > .3:
            norm[j] = (M[j] / g, rj ** 2 / (1 - rj ** 2))
    per = {}
    for c in sorted(set(chs[list(norm)])):
        own = [j for j in norm if chs[j] == c]
        oth = [j for j in norm if chs[j] != c]
        if not oth:
            continue
        pred = sum(norm[j][1] * norm[j][0] for j in oth) / sum(norm[j][1] for j in oth)
        best = None
        for j in own:
            rr = corr(M[j], pred); g = float(M[j] @ pred / (pred @ pred))
            if best is None or rr > best[0]:
                best = (rr, g, C[j]['centre'], C[j]['rms_pct'])
        per[int(c)] = dict(r=round(best[0], 3), gain=round(best[1], 3), hz=round(best[2], 2), rms_pct=round(best[3], 3))
    agree = [c for c, v in per.items() if v['r'] >= .7 and .6 <= v['gain'] <= 1.6]
    fams = []
    for c in agree:
        hz = per[c]['hz']
        if not any(same_source(hz, f) or abs(hz - f) < .025 * max(hz, f) for f in fams):
            fams.append(hz)
    full = sum(w * e for e, w in norm.values()) / sum(w for _, w in norm.values())
    full *= np.median([per[c]['rms_pct'] for c in agree]) / 100 / max(full.std(), 1e-12) if agree else 1.
    seed = np.zeros(int(dur * RATE)); seed[edge] = full
    out.update(seed_channel=int(chs[s]), seed_hz=round(C[s]['centre'], 2), cluster_size=len(mem),
               per_channel=per, agreeing_channels=agree, n_agreeing=len(agree), families=[round(f, 2) for f in fams],
               eligible=len(agree) >= 3 and len(fams) >= 2,
               consensus_rms_pct=round(100 * float(full.std()), 3), seconds=round(time.time() - t, 1))
    return out, seed


def _job(args):
    tape, t0, outdir = args
    p = Path(outdir) / f'{tape}_{int(t0)}.json'
    if p.exists():
        return json.loads(p.read_text())
    try:
        o, seed = window(tape, t0)
        if seed is not None:
            np.save(p.with_suffix('.npy'), seed.astype(np.float32))
    except Exception as ex:
        o = dict(tape=tape, t0=t0, error=repr(ex), eligible=False)
    p.write_text(json.dumps(o, indent=1))
    return o


if __name__ == '__main__':
    outdir = sys.argv[1]; tapes = sys.argv[2].split(','); n = int(sys.argv[3]); jobs = int(sys.argv[4])
    Path(outdir).mkdir(parents=True, exist_ok=True)
    work = []
    for tape in tapes:
        D = max(c['frames'] for c in inventory(tape).values()) / SR
        work += [(tape, float(round(D * (i + .5) / n)), outdir) for i in range(n)]
    os.environ['OMP_NUM_THREADS'] = '1'
    with ProcessPoolExecutor(jobs) as ex:
        for o in ex.map(_job, work):
            print(o['tape'], o['t0'], 'eligible', o.get('eligible'), 'n_agree', o.get('n_agreeing'),
                  'fams', o.get('families'), 'rms', o.get('consensus_rms_pct'), o.get('seconds'), flush=True)
