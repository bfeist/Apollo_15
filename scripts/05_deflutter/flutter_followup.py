"""Pass 3: bounded follow-up analysis of queued census intervals.

Queued windows of one tape are merged into regions with context and each
region is scheduled once for all channels. A job rereads only that region of
the canonical originals, retracks every carrier with the census code (whose
implementation must match the census run), and saves what the census does not
retain: every competing cluster's curve, family/feed stability, mirror
explanations of sign conflicts, leave-group-out and non-member concentration
of tones above 200 Hz, and per-beep timing statistics against the census curve.

Results are content-addressed under ``deflutter_analysis/<run-id>/followup_cache``
by job, census evidence, settings and this file's hash, so a new policy (new
analysis ID) reuses them. They are measurements; ``qualification_policy``
turns them into decisions. The census is never modified.
"""
import concurrent.futures
import math
import multiprocessing
import os
from pathlib import Path
import platform
import shutil
import time
import traceback

import numpy as np

import flutter_analysis as analysis
import flutter_census as census
import flutter_detection as detection
from flutter_tracking import burst_track

RATE = census.RATE
SETTINGS = dict(merge_gap_seconds=30., context_seconds=12., max_region_seconds=480.,
                hf_min_hz=200., hf_min_fm_percent=.5, hf_max_lines=4,
                beep_core_margin_seconds=.03, beep_pad_seconds=.15, beep_lag_samples=60,
                mirror_tolerance_hz=.6, mains_adjacent_hz=1.5)
LAGS = list(range(-SETTINGS['beep_lag_samples'], SETTINGS['beep_lag_samples']+1))
MAX_WORKERS = detection.WINDOWS_MAX_WORKERS
FILES = ('result.json', 'curves.npy')

sha256, digest, read_json, atomic_json = detection.sha256, detection.digest, detection.read_json, detection.atomic_json


def implementation_sha256():
    return sha256(Path(__file__).resolve())


# ---------------------------------------------------------------- cluster hypotheses

def screen_pool(members, groups, settings):
    """The census agreement graph and clusters, keeping every cluster."""
    w = settings['window']
    edge = int(w['edge_seconds']*RATE)
    pool = [(m, e[edge:len(e)-edge].astype(np.float64)) for m, e, ok in members
            if ok.mean() >= w['min_window_support']]
    if len(pool) < 2:
        return None
    M = np.array([e for _, e in pool])
    rms = 100*M.std(1)
    Mn = (M-M.mean(1, keepdims=True))/(M.std(1, keepdims=True)+1e-12)
    R = Mn @ Mn.T/M.shape[1]
    np.fill_diagonal(R, 0)
    group = np.array([groups[m['channel']] for m, _ in pool])
    hz = np.array([m['centre'] for m, _ in pool])
    graph = ((R > w['r_edge']) & (group[:, None] != group[None, :])
             & (abs(hz[:, None]-hz[None, :]) > w['family_separation']*np.maximum(hz[:, None], hz[None, :])))
    alive = np.ones(len(pool), bool)
    clusters = []
    for _ in range(int(w['max_clusters'])):
        degree = (graph & alive[None, :] & alive[:, None]).sum(1)
        if degree.max() < 2:
            break
        seed = int(np.argmax(degree))
        chosen = [seed]+[j for j in range(len(pool)) if alive[j] and j != seed and R[seed, j] > w['r_edge']]
        clusters.append((seed, chosen, float(np.median(rms[chosen]))))
        alive[chosen] = False
    usable = [k for k, c in enumerate(clusters) if len(c[1]) >= w['min_cluster']]
    chosen, comparable = None, []
    if usable:
        top = max(clusters[k][2] for k in usable)
        comparable = [k for k in usable if clusters[k][2] >= (1-w['comparable_fm_fraction'])*top]
        chosen = max(comparable, key=lambda k: (len({int(group[j]) for j in clusters[k][1]}), clusters[k][2]))
    return dict(pool=pool, M=M, R=R, rms=rms, group=group, hz=hz, clusters=clusters,
                chosen=chosen, comparable=comparable)


def normalized(P, k, settings):
    """Gain-normalised members and census weights for cluster ``k``."""
    w = settings['window']
    seed, chosen, _ = P['clusters'][k]
    M, R = P['M'], P['R']
    reference = M[seed]
    out = {}
    for j in chosen:
        gain = 1. if j == seed else float(M[j] @ reference/max(reference @ reference, 1e-30))
        r = w['r_cap'] if j == seed else min(R[seed, j], w['r_cap'])
        if gain > w['gain_floor']:
            out[j] = (M[j]/gain, r*r/(1-r*r))
    return out


def weighted(members, keys):
    keys = list(keys)
    weight = sum(members[j][1] for j in keys)
    return sum(members[j][1]*members[j][0] for j in keys)/weight


def family_keys(frequencies, settings):
    separation = settings['window']['family_separation']
    keys = []
    for f in sorted(frequencies):
        if not any(census.same_source(f, k) or abs(f-k) < separation*max(f, k) for k in keys):
            keys.append(f)
    return keys


def family_of(f, keys, settings):
    separation = settings['window']['family_separation']
    return next(k for k in keys if census.same_source(f, k) or abs(f-k) < separation*max(f, k))


def hypotheses(P, reference, settings, curves):
    bound = settings['gates']['speed_bound']
    rows = []
    for k, (seed, members, fm) in enumerate(P['clusters']):
        norm = normalized(P, k, settings)
        if not norm:
            continue
        curve = np.clip(weighted(norm, norm), -bound, bound)
        chosen_seed = P['clusters'][P['chosen']][0] if P['chosen'] is not None else None
        rows.append(dict(index=k, seed_channel=int(P['pool'][seed][0]['channel']), seed_hz=round(float(P['hz'][seed]), 2),
                         size=len(members), groups=len({int(P['group'][j]) for j in norm}),
                         families=[round(f, 2) for f in family_keys([float(P['hz'][j]) for j in norm], settings)],
                         median_fm_percent=round(fm, 4), consensus_fm_percent=round(100*float(curve.std()), 4),
                         chosen=k == P['chosen'], comparable=k in P['comparable'],
                         r_to_chosen_seed=None if chosen_seed is None else round(float(P['R'][seed, chosen_seed]), 4),
                         r_to_census=None if reference is None else round(census.correlation(curve, reference), 4),
                         curve_index=len(curves), amplitude='unscaled weighted mean'))
        curves.append(curve.astype(np.float32))
    return rows


def family_stability(P, settings):
    norm = normalized(P, P['chosen'], settings)
    keys = family_keys([float(P['hz'][j]) for j in norm], settings)
    rows = []
    for key in keys:
        own = [j for j in norm if family_of(float(P['hz'][j]), keys, settings) == key]
        rest = [j for j in norm if j not in own]
        r = census.correlation(weighted(norm, own), weighted(norm, rest)) if own and rest else None
        rows.append(dict(hz=round(key, 2), members=len(own), groups=len({int(P['group'][j]) for j in own}),
                         hum=census.is_hum(key), r_to_rest=None if r is None else round(r, 4)))
    total = sum(v for _, v in norm.values())
    shares = {}
    for j, (_, v) in norm.items():
        shares[int(P['group'][j])] = shares.get(int(P['group'][j]), 0.)+v/total
    values = [r['r_to_rest'] for r in rows if r['r_to_rest'] is not None]
    return dict(families=rows, min_r=min(values) if len(rows) >= 2 and values else None,
                groups=len(shares), max_group_share=round(max(shares.values()), 4))


def sign_conflict(P, settings, fs):
    """Is an anti-correlated comparable cluster a mirror/mains-adjacent product?"""
    w = settings['window']
    seed = P['clusters'][P['chosen']][0]
    chosen_hz = [float(P['hz'][j]) for j in P['clusters'][P['chosen']][1]]
    competing = [k for k in P['comparable'] if k != P['chosen'] and P['R'][P['clusters'][k][0], seed] < -w['r_edge']]
    if not competing:
        return dict(reproduced=False, competing_clusters=0, explained_fraction=0.)
    rows = []
    for k in competing:
        for j in P['clusters'][k][1]:
            h = float(P['hz'][j])
            mirror = any(abs(h+f-120*round((h+f)/120)) <= fs['mirror_tolerance_hz'] and round((h+f)/120) >= 1
                         for f in chosen_hz)
            adjacent = abs(h-60*round(h/60)) <= fs['mains_adjacent_hz']
            rows.append(mirror or adjacent)
    return dict(reproduced=True, competing_clusters=len(competing), competing_members=len(rows),
                explained_fraction=round(float(np.mean(rows)), 4),
                note='Explained: a mirror image of a chosen carrier about 60n Hz, or adjacent to an exact mains line')


def full_window(curve, settings):
    w = settings['window']
    length = int(round(w['seconds']*RATE))
    edge = int(w['edge_seconds']*RATE)
    out = np.zeros(length)
    out[edge:length-edge] = curve
    return out


def hf_witness(P, reference, cleans, first, settings, fs, low_rate):
    """Concentration of >200 Hz tones; a member line is dewarped leave-group-out."""
    w, a = settings['window'], settings['audit']
    norm = normalized(P, P['chosen'], settings)
    seed, chosen, _ = P['clusters'][P['chosen']]
    band = lambda j: (fs['hf_min_hz'] <= P['hz'][j] < settings['fast_max_hz'] and P['pool'][j][0]['path'] == 'fast')
    best = {}
    for j, (_, weight) in norm.items():
        g = int(P['group'][j])
        if band(j) and (g not in best or weight > norm[best[g]][1]):
            best[g] = j
    lines = []
    for g, j in sorted(best.items(), key=lambda item: -norm[item[1]][1])[:fs['hf_max_lines']]:
        others = [k for k in norm if int(P['group'][k]) != g]
        if not others:
            continue
        loo = weighted(norm, others)
        loo = loo*float(reference.std())/max(float(loo.std()), 1e-12)
        lines.append(('member_leave_group_out', j, loo))
    outside = [j for j in range(len(P['pool'])) if j not in chosen and band(j)
               and P['rms'][j] >= fs['hf_min_fm_percent'] and abs(P['R'][seed, j]) < w['r_edge']]
    for j in sorted(outside, key=lambda j: -P['rms'][j])[:fs['hf_max_lines']]:
        lines.append(('non_member', j, reference))
    n = int(w['seconds']*low_rate)
    out = []
    for kind, j, curve in lines:
        m = P['pool'][j][0]
        x = cleans[m['channel']][first:first+n].astype(np.float64)
        if len(x) < n:
            continue
        e = full_window(curve, settings)
        values = {k: census.concentration(census.dewarp(x, c, low_rate) if c is not None else x, m['centre'], low_rate)
                  for k, c in (('none', None), ('curve', e), ('wrong_sign', -e),
                               ('shift', census.shifted(e, a['shift_seconds'])))}
        out.append(dict(kind=kind, channel=int(m['channel']), group=int(P['group'][j]), hz=round(float(m['centre']), 2),
                        fm_percent=round(float(P['rms'][j]), 4), **{k: round(v, 5) for k, v in values.items()},
                        concentrates=bool(values['curve'] > max(values['none'], values['wrong_sign'], values['shift'])),
                        reversed_better=bool(values['wrong_sign'] > values['curve'])))
    return out


def beep_statistics(beeps, window_start, region_start, reference, settings, fs, sr):
    """Per-beep sufficient statistics of beep FM against the census curve at lags."""
    w, a = settings['window'], settings['audit']
    e = full_window(reference, settings)
    length = len(e)
    margin = a['beep_margin_seconds']
    out = []
    for channel, items in sorted(beeps.items()):
        for event, audio, offset in items:
            start, end = region_start+event['start'], region_start+event['end']
            if not (window_start+margin < start and end < window_start+w['seconds']-margin):
                continue
            _, freq = burst_track(audio, sr)
            t = region_start+offset+np.arange(len(freq))/1000
            core = (t > start+fs['beep_core_margin_seconds']) & (t < end-fs['beep_core_margin_seconds'])
            f, tt = freq[core], t[core]
            count = len(f)//5
            if count < 8:
                continue
            f = f[:5*count].reshape(count, 5).mean(1)
            tt = tt[:5*count].reshape(count, 5).mean(1)
            d = f/np.median(f)-1
            d = d-d.mean()
            index = np.rint((tt-window_start)*RATE).astype(int)
            cc, dc = [], []
            for lag in LAGS:
                k = np.clip(index+lag, 0, length-1)
                c = e[k]-e[k].mean()
                cc.append(round(float(c @ c), 12))
                dc.append(round(float(d @ c), 12))
            zero = LAGS.index(0)
            out.append(dict(channel=int(channel), start=round(start, 3), end=round(end, 3), n=count,
                            dd=round(float(d @ d), 12), cc=cc, dc=dc,
                            r0=round(dc[zero]/math.sqrt(max(d @ d*cc[zero], 1e-30)), 4),
                            deviation_percent=round(100*float(d.std()), 4)))
    return out


# ---------------------------------------------------------------- one region job

def analyse_window(w, region_start, tracks, groups, cleans, beeps, census_curve, settings, fs, sr, curves):
    length = int(round(settings['window']['seconds']*RATE))
    low_rate = sr/int(settings['decimation'])
    i0 = int(round((w['start_seconds']-region_start)*RATE))
    members = [(m, m['e'][i0:i0+length], m['ok'][i0:i0+length]) for ch in tracks for m in tracks[ch]
               if len(m['e']) >= i0+length]
    record, consensus = census.screen_window(members, groups, settings)
    P = screen_pool(members, groups, settings)
    result = dict(status='complete', window=w['id'], eligible=w['eligible'],
                  reproduced=dict(eligible=record['eligible'], reasons=record.get('reasons', []),
                                  sign_conflict=record.get('sign_conflict'),
                                  agreeing_groups=len(record.get('agreeing_groups', []))),
                  reproduction_r=(None if consensus is None or census_curve is None
                                  else round(census.correlation(consensus, census_curve), 4)),
                  hypotheses=[] if P is None else hypotheses(P, census_curve, settings, curves),
                  family=dict(min_r=None, max_group_share=None, families=[], groups=0),
                  sign_conflict=None, hf=[], beeps=[])
    g = settings['gates']
    result['additional_evidence'] = any(
        h['groups'] >= g['min_agreeing'] and h['median_fm_percent'] >= g['cluster_min_fm_percent']
        and g['consensus_fm_percent'][0] <= h['consensus_fm_percent'] <= g['consensus_fm_percent'][1]
        for h in result['hypotheses'])
    if census_curve is not None and P is not None and P['chosen'] is not None:
        result['family'] = family_stability(P, settings)
        result['sign_conflict'] = sign_conflict(P, settings, fs)
        result['hf'] = hf_witness(P, census_curve, cleans, int(round((w['start_seconds']-region_start)*low_rate)),
                                  settings, fs, low_rate)
        result['beeps'] = beep_statistics(beeps, w['start_seconds'], region_start, census_curve, settings, fs, sr)
    return result


def compute_job(census_dir, job, fs=SETTINGS):
    census_dir = Path(census_dir)
    config = detection.load_run(census_dir)
    detection.check_code(config)
    settings = detection.settings_of(config)
    tape = read_json(census_dir/job['tape']/'inventory.json')
    if (tape['tape'] != job['tape'] or tape['inventory_sha256'] != job['inventory_sha256']
            or digest(detection.source_identity(tape['inventory'])) != job['inventory_sha256']
            or sha256(census_dir/job['tape']/'evidence.json') != job['evidence_sha256']):
        raise detection.IdentityError('Follow-up job and census evidence disagree')
    sr = int(settings['sample_rate'])
    low_rate = sr/int(settings['decimation'])
    a, b = job['read_start'], job['read_end']
    read, stats = detection.read_block(tape['inventory']['channels'], a, b, sr)
    started, cpu = time.perf_counter(), time.process_time()
    tracks, prints, cleans, beeps = {}, {}, {}, {}
    pad = fs['beep_pad_seconds']
    for row in tape['inventory']['channels']:
        channel = row['channel']
        x, coverage = read(channel)
        if coverage < .5 or not np.any(x):
            continue
        found, _, clean, events = census.channel_tracks(x, sr, settings)
        for m in found:
            m['channel'] = channel
        tracks[channel] = found
        prints[channel] = census.fingerprint(clean, low_rate, settings)
        cleans[channel] = clean.astype(np.float32)
        beeps[channel] = []
        for event in events:
            lo, hi = max(0, int((event['start']-pad)*sr)), min(len(x), int((event['end']+pad)*sr))
            beeps[channel].append((event, np.array(x[lo:hi], np.float64), lo/sr))
        del x, clean
    groups = census.duplicate_groups(prints, settings['gates']['duplicate_r'], tape['duplicate_audit']['duplicate_groups'])
    stored = analysis.Curves(census_dir/job['tape'])
    curves, windows = [], {}
    for w in job['windows']:
        reference = stored(w) if w['curve_index'] is not None else None
        windows[w['id']] = analyse_window(w, a, tracks, groups, cleans, beeps, reference, settings, fs, sr, curves)
    width = int(round((settings['window']['seconds']-2*settings['window']['edge_seconds'])*RATE))
    array = np.array(curves, np.float32).reshape(len(curves), width)
    execution = dict(wall_seconds=round(time.perf_counter()-started+stats['read_seconds'], 3),
                     read_seconds=round(stats['read_seconds'], 3), cpu_seconds=round(time.process_time()-cpu, 3),
                     hostname=platform.node())
    return windows, array, execution


def job_key(job, census_config, fs=SETTINGS):
    return digest(dict(job=job, census_run_id=census_config['run_id'], settings=fs, lags=LAGS,
                       implementation=implementation_sha256()))[:24]


def verify_result(folder, key):
    folder = Path(folder)
    if not folder.exists():
        return None
    if not folder.is_dir() or {p.name for p in folder.iterdir()} != {*FILES, 'result.sha256'}:
        raise analysis.AnalysisError(f'Incomplete follow-up cache entry: {folder}')
    if (folder/'result.sha256').read_text().strip() != sha256(folder/'result.json'):
        raise analysis.AnalysisError(f'Follow-up measurements changed: {folder}')
    result = read_json(folder/'result.json')
    if (result.get('kind') != 'followup_result' or result.get('key') != key
            or sha256(folder/'curves.npy') != result['curves']['sha256']):
        raise analysis.AnalysisError(f'Follow-up result changed: {folder}')
    return result


def execute(census_dir, job, key, cache_dir, fs=SETTINGS):
    """Compute one region and publish it atomically into the shared cache."""
    final = Path(cache_dir)/key
    if verify_result(final, key) is not None:
        return dict(key=key, status='complete', reused=True)
    pending = final.with_name(f'{key}.{os.getpid()}.partial')
    if pending.exists():
        shutil.rmtree(pending)
    pending.mkdir(parents=True)
    windows, curves, execution = compute_job(census_dir, job, fs)
    np.save(pending/'curves.npy', curves.astype('<f4'), allow_pickle=False)
    result = dict(schema=1, kind='followup_result', key=key, job=job, settings=fs, lags=LAGS,
                  implementation_sha256=implementation_sha256(), execution=execution,
                  curves=dict(file='curves.npy', sha256=sha256(pending/'curves.npy'), shape=list(curves.shape)),
                  windows=windows,
                  interpretation='Follow-up measurements; decisions belong to the qualification policy')
    atomic_json(pending/'result.json', result)
    (pending/'result.sha256').write_text(sha256(pending/'result.json'))
    try:
        pending.rename(final)
    except OSError:
        if verify_result(final, key) is None:
            raise
        shutil.rmtree(pending)
    return dict(key=key, status='complete', reused=False, execution=execution)


def _execute_safely(census_dir, job, key, cache_dir, fs=SETTINGS):
    try:
        return execute(census_dir, job, key, cache_dir, fs)
    except Exception as exc:  # Reported per job; other regions continue.
        return dict(key=key, status='failed', message=f'{type(exc).__name__}: {exc}', traceback=traceback.format_exc())


def _initialise_worker():
    for name in detection.THREAD_VARIABLES:
        os.environ[name] = '1'
    detection.lower_priority()


# ---------------------------------------------------------------- scheduling

def plan_jobs(bundle, fs=SETTINGS):
    """Merge each tape's queued windows into regions read once for all channels."""
    rows = {(r['tape'], r['id']): r for r in analysis.window_rows(bundle)}
    tapes = analysis.tape_rows(bundle)
    manifest = {t['tape']: t for t in read_json(bundle['dir']/'input_manifest.json')['tapes']}
    by_tape = {}
    for item in analysis.queue_items(bundle):
        by_tape.setdefault(item['tape'], set()).add(item['window'])
    jobs = []
    for tape in sorted(by_tape):
        duration = tapes[tape]['duration_seconds']
        windows = sorted((rows[(tape, wid)] for wid in by_tape[tape]),
                         key=lambda row: (row['start_seconds'], row['id']))
        regions = []
        for row in windows:
            s, e = row['start_seconds'], row['end_seconds']
            if regions and s-regions[-1]['end'] <= fs['merge_gap_seconds'] and \
                    max(e, regions[-1]['end'])-regions[-1]['start'] <= fs['max_region_seconds']:
                regions[-1]['end'] = max(regions[-1]['end'], e)
                regions[-1]['windows'].append(row)
            else:
                regions.append(dict(start=s, end=e, windows=[row]))
        for region in regions:
            read_start = math.floor(max(0., region['start']-fs['context_seconds'])*RATE+1e-9)/RATE
            read_end = min(duration, region['end']+fs['context_seconds'])
            jobs.append(dict(tape=tape, read_start=read_start, read_end=round(read_end, 6),
                             inventory_sha256=manifest[tape]['inventory_sha256'],
                             evidence_sha256=manifest[tape]['evidence_sha256'],
                             windows=[dict(id=row['id'], block=row['block'], start_seconds=row['start_seconds'],
                                           curve_index=row['curve_index'], eligible=row['eligible'])
                                      for row in region['windows']]))
    return jobs


@analysis.bundle_writer
def run_followup(analysis_dir, *, workers=1, progress=print):
    """Run (or resume) the one defined follow-up pass of this analysis."""
    bundle = analysis.open_analysis(analysis_dir)
    analysis.check_code(bundle['config'])
    census_config = detection.load_run(bundle['census_dir'])
    if not 1 <= workers <= MAX_WORKERS:
        raise ValueError(f'Follow-up workers must be 1-{MAX_WORKERS}')
    folder = bundle['dir']/'followup'
    folder.mkdir(exist_ok=True)
    jobs = plan_jobs(bundle)
    keyed = [dict(key=job_key(j, census_config), job=j) for j in jobs]
    plan_path = folder/'jobs.json'
    if plan_path.is_file():
        if read_json(plan_path)['jobs'] != keyed:
            raise analysis.AnalysisError('Follow-up job plan changed for this analysis ID')
    else:
        atomic_json(plan_path, dict(analysis_id=bundle['config']['analysis_id'], settings=SETTINGS,
                                    implementation_sha256=implementation_sha256(), jobs=keyed))
    cache = bundle['dir'].parent/'followup_cache'
    cache.mkdir(exist_ok=True)
    statuses = {}
    pending = []
    for item in keyed:
        if verify_result(cache/item['key'], item['key']) is not None:
            statuses[item['key']] = dict(status='complete', reused=True)
        else:
            pending.append(item)
    progress(f'Follow-up: {len(keyed)} region jobs, {len(pending)} to compute')
    census_dir = str(bundle['census_dir'])

    def record(outcome):
        statuses[outcome['key']] = {k: v for k, v in outcome.items() if k != 'key'}
        done = sum(v['status'] == 'complete' for v in statuses.values())
        message = outcome.get('message', '')
        progress(f'{done}/{len(keyed)} complete; {outcome["key"]} {outcome["status"]} {message}'.rstrip())
    try:
        if workers == 1:
            for item in pending:
                record(_execute_safely(census_dir, item['job'], item['key'], str(cache)))
        else:
            context = multiprocessing.get_context('spawn')
            with concurrent.futures.ProcessPoolExecutor(max_workers=workers, mp_context=context,
                                                        initializer=_initialise_worker) as pool:
                futures = [pool.submit(_execute_safely, census_dir, item['job'], item['key'], str(cache))
                           for item in pending]
                for future in concurrent.futures.as_completed(futures):
                    record(future.result())
    finally:
        status = dict(analysis_id=bundle['config']['analysis_id'], jobs=len(keyed),
                      complete=all(statuses.get(i['key'], {}).get('status') == 'complete' for i in keyed),
                      counts={s: sum(v['status'] == s for v in statuses.values()) for s in ('complete', 'failed')},
                      statuses=statuses, updated=time.time())
        if status['complete']:
            status['summary'] = summarise(value for _, value in iter_results(bundle, require_complete=True))
        atomic_json(folder/'status.json', status)
    return status


def iter_results(bundle, require_complete=False, tape=None):
    """Stream cached jobs using tape-scoped window IDs."""
    plan_path = bundle['dir']/'followup'/'jobs.json'
    if not plan_path.is_file():
        if require_complete:
            raise analysis.AnalysisError('Follow-up pass has not been planned or run')
        return
    cache = bundle['dir'].parent/'followup_cache'
    for item in read_json(plan_path)['jobs']:
        if tape is not None and item['job']['tape'] != tape:
            continue
        result = verify_result(cache/item['key'], item['key'])
        if result is None:
            if require_complete:
                raise analysis.AnalysisError(f'Follow-up job {item["key"]} is not complete')
            for w in item['job']['windows']:
                yield (item['job']['tape'], w['id']), dict(status='missing', job_key=item['key'])
            continue
        if result['job'] != item['job'] or set(result['windows']) != {w['id'] for w in item['job']['windows']}:
            raise analysis.AnalysisError(f'Follow-up measurements disagree with job {item["key"]}')
        for wid, value in result['windows'].items():
            yield (item['job']['tape'], wid), dict(value, job_key=item['key'], tape=item['job']['tape'])


def load_results(bundle, require_complete=False, tape=None):
    return dict(iter_results(bundle, require_complete, tape))


def summarise(results):
    """Yield of the follow-up pass: measurements, not decisions."""
    from qualification_policy import POLICY, pooled_beeps
    from collections import Counter
    totals = dict(windows=0, complete=0, eligible=0, reproduced_eligibility=0, hf_lines=0,
                  hf_concentrating=0, hf_reversed=0, unsupported_with_additional_evidence=0, unsupported_analysed=0)
    correlations, states = [], Counter()
    for row in results:
        totals['windows'] += 1
        if row['status'] != 'complete':
            continue
        totals['complete'] += 1
        totals['reproduced_eligibility'] += row['reproduced']['eligible'] == row['eligible']
        if row['eligible']:
            totals['eligible'] += 1
            correlations.append(row['reproduction_r'])
            states[pooled_beeps(row['beeps'], LAGS, POLICY)['state']] += 1
        else:
            totals['unsupported_analysed'] += 1
            totals['unsupported_with_additional_evidence'] += row['additional_evidence']
        totals['hf_lines'] += len(row['hf'])
        totals['hf_concentrating'] += sum(v['concentrates'] for v in row['hf'])
        totals['hf_reversed'] += sum(v['reversed_better'] for v in row['hf'])
    return dict(**totals, reproduction_r=analysis.percentiles(correlations), beep_states=dict(states),
                note='All beeps pooled here; qualification uses only decision-half events')
