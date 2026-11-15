"""E5: check-tone-free evaluation of one window (blind seed -> census ->
families -> non-circular leave-one-family-out validation).

Non-circularity: every family's FM is measured by wide-capture tracking on the
ORIGINAL audio (no dewarp), so its estimate does not contain the consensus.
Concentration controls (consensus vs wrong-sign vs 1.5 s shift vs none) add an
independent spectral witness.

usage: e5_window_eval.py TAPE T0 [T0 ...] --dur 64 --out DIR [--jobs N]
"""
import argparse, json, os, sys, time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
import numpy as np
from scipy import signal
sys.path.insert(0, str(Path(__file__).parent))
from common import RATE, SR, corr, demod, dewarp, frac_dev, inventory, read, exact_mains, remove_stationary
from e3_consensus import census_lines, families, concentration
from e4_blind_seed import run as blind


def raw_fm(x, hz, others_hz):
    nb = [abs(o - hz) for o in others_hz if abs(o - hz) > 1.]
    cap = float(np.clip(.04 * hz + 14., 8., min(120., hz - 20.)))
    if nb:
        cap = float(np.clip(min(cap, .8 * min(nb)), 4., 120.))
    amp, fr = demod(x, SR, hz, cap)
    e, ok, c = frac_dev(fr, amp, band=(2., 30.), capture=cap)
    hb = signal.sosfiltfilt(signal.butter(3, [35, 90], fs=RATE, btype='band', output='sos'), (fr - c) / c)
    return e, ok, float(hb[ok].std()) if ok.sum() > 100 else 1., cap


def evaluate(tape, t0, dur=64.):
    t = time.time()
    b = blind(tape, t0, dur)
    seed = b.pop('_seed', None)
    res = dict(tape=tape, t0=t0, dur=dur, blind_seconds=round(b['seconds'], 1),
               blind_partners=b.get('n_independent_partners', 0),
               blind_cluster_channels=b.get('cluster_channels', []))
    if seed is None:
        res.update(status='no_seed', seconds=round(time.time() - t, 1))
        return res
    xs = {ch: read(tape, ch, t0, dur) for ch in sorted(inventory(tape))}
    xs = {k: v for k, v in xs.items() if v.any()}
    clean = {ch: remove_stationary(x, exact_mains(x)) for ch, x in xs.items()}
    lines = census_lines(clean, seed)
    fam = families(lines)
    edge = slice(4 * RATE, -4 * RATE)
    F_est = []
    for F in fam:
        acc, n, mem = 0., 0, []
        for r in F[:6]:
            others = [l['hz'] for l in lines if l['ch'] == r['ch']]
            fam_hz = [m['hz'] for m in F]
            others = [o for o in others if all(abs(o - h) > 2.5 for h in fam_hz)]
            e, ok, noise, cap = raw_fm(clean[r['ch']], r['hz'], others)
            need = .6 * float(np.clip(.04 * r['hz'] + 14., 8., 120.))
            if ok.mean() < .6 or cap < need:
                continue
            acc = acc + e; n += 1; mem.append((r['ch'], round(r['hz'], 2), round(cap, 1)))
        if n:
            F_est.append(dict(F=F, e=acc / n, members=mem))
    val = []
    if len(F_est) >= 2:
        E = np.array([f['e'] for f in F_est])
        W = np.ones(len(F_est))
        for it in range(2):  # equal weights, then coherence weights from LOO r
            rs = []
            for i in range(len(F_est)):
                Wo = np.delete(W, i)
                pred = (Wo[:, None] * np.delete(E, i, 0)).sum(0) / max(Wo.sum(), 1e-12)
                rs.append(corr(E[i][edge], pred[edge]))
            rs = np.clip(np.array(rs), 0, .97)
            W = rs ** 2 / (1 - rs ** 2) + 1e-6
        for i, f in enumerate(F_est):
            Wo = np.delete(W, i); pred = (Wo[:, None] * np.delete(E, i, 0)).sum(0) / Wo.sum()
            y, p = f['e'][edge], pred[edge]
            g = float(np.dot(y, p) / max(np.dot(p, p), 1e-30))
            r0 = f['F'][0]
            x = clean[r0['ch']]
            q = {k: concentration(dewarp(x, c) if c is not None else x, r0['hz'])
                 for k, c in [('none', None), ('pred', pred), ('wrong_sign', -pred),
                              ('shift', np.roll(pred, int(1.5 * RATE)))]}
            cg = q['pred'] / max(q['none'], q['wrong_sign'], q['shift'], 1e-9)
            ok_conc = cg >= 2.5
            rr = corr(y, p)
            # accuracy: residual FM of the held-out line after LOO correction,
            # narrow capture (valid only when the line concentrates, ok_conc)
            amp_, fr_ = demod(dewarp(x, pred), SR, r0['hz'], 4.)
            res_, okr, _ = frac_dev(fr_, amp_, band=(2., 30.), capture=4.)
            val.append(dict(members=f['members'], weight=round(float(W[i] / W.sum()), 4),
                            fm_rms_pct=round(100 * float(y.std()), 3), r=round(rr, 3),
                            gain=round(g, 3),
                            residual_ratio=round(float((y - p).std() / max(y.std(), 1e-9)), 3),
                            conc={k: round(v, 4) for k, v in q.items()}, conc_gain=round(float(cg), 2),
                            conc_pass=bool(ok_conc), residual_after_pct=round(100 * float(res_[edge].std()), 3),
                            validated=bool(ok_conc and rr > .5)))
        cons = (W[:, None] * E).sum(0) / W.sum()
        res['_cons'] = cons
        res['consensus_rms_pct'] = round(100 * float(cons[edge].std()), 3)
        f_, pe = signal.welch(cons[edge], RATE, nperseg=1024)
        k = (f_ > 2) & (f_ < 30)
        res['flutter_peak_hz'] = round(float(f_[k][np.argmax(pe[k])]), 2)
        res['seed_vs_consensus_r'] = round(corr(seed[edge], cons[edge]), 3)
    validated = [v for v in val if v['validated']]
    res.update(status='ok', n_lines=len(lines), n_families=len(fam), n_measured_families=len(F_est),
               line_channels=sorted({l['ch'] for l in lines}),
               n_validated_families=len(validated),
               validated_channels=sorted({m[0] for v in validated for m in v['members']}),
               eligible=len(validated) >= 2, validation=sorted(val, key=lambda v: -v['weight']),
               seconds=round(time.time() - t, 1))
    return res


def _job(args):
    tape, t0, dur, out = args
    p = Path(out) / f'{tape}_{int(t0)}.json'
    if p.exists():
        return json.loads(p.read_text())
    try:
        r = evaluate(tape, t0, dur)
    except Exception as ex:  # keep failures explicit
        r = dict(tape=tape, t0=t0, status='error', error=repr(ex))
    cons = r.pop('_cons', None)
    if cons is not None:
        np.save(p.with_suffix('.npy'), cons.astype(np.float32))
    p.write_text(json.dumps(r, indent=1))
    return r


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('tape'); ap.add_argument('t0', type=float, nargs='+')
    ap.add_argument('--dur', type=float, default=64.); ap.add_argument('--out', required=True)
    ap.add_argument('--jobs', type=int, default=1)
    a = ap.parse_args()
    Path(a.out).mkdir(parents=True, exist_ok=True)
    jobs = [(a.tape, t0, a.dur, a.out) for t0 in a.t0]
    os.environ.setdefault('OMP_NUM_THREADS', '1')
    with ProcessPoolExecutor(a.jobs) as ex:
        for r in ex.map(_job, jobs):
            print(json.dumps({k: r.get(k) for k in ('tape', 't0', 'status', 'blind_partners', 'n_lines', 'n_families',
                                                     'n_validated_families', 'validated_channels', 'eligible',
                                                     'consensus_rms_pct', 'flutter_peak_hz', 'seconds')}))


if __name__ == '__main__':
    main()
