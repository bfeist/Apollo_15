"""Read-only broad qualification; writes only a new experiment JSON report.

Runs both implementations locally, reuses saved cluster probe samples, and
requires exact local float64 models, float32 renders and complete diagnostics.
Existing platform probe samples are compared separately, using the established
cross-platform tolerance. No production audio or checkpoint is written.
"""
import os
for name in ('OPENBLAS_NUM_THREADS', 'OMP_NUM_THREADS', 'MKL_NUM_THREADS',
             'BLIS_NUM_THREADS', 'NUMEXPR_NUM_THREADS'):
    os.environ[name] = '1'

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path, PureWindowsPath
import platform
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
import numpy as np
import scipy
import soundfile as sf
import denoise_v2 as reference
import denoise_v2_numba as accelerated
from render_denoise_v2 import gather_seeds, saved_seeds


def digest(data):
    return hashlib.sha256(data).hexdigest()


def difference(a, b):
    d = a.astype('float64')-b.astype('float64')
    return dict(exact=bool(np.array_equal(a, b)), peak=float(np.max(abs(d), initial=0)),
                rms=float(np.sqrt(np.mean(d*d))) if d.size else 0.)


def summary(records):
    return dict(component_kinds=dict(Counter(c['kind'] for r in records for c in r['components'])),
                refinement_passes=sum(len(v.get('passes', [])) for r in records for v in r.get('refinement', [])),
                bypasses=[r['bypass'] for r in records if 'bypass' in r],
                saved_noncomb_candidates=sum(v.get('origin') == 'saved_noncomb'
                                            for r in records for v in r.get('refinement', [])))


def synthetic_cases():
    sr = 8000
    t = np.arange(48*sr)/sr
    rng = np.random.default_rng(815)
    noise = rng.normal(0, .0015, len(t))
    phase = .0015*np.sin(2*np.pi*.37*t)
    hum = sum(level*np.sin(2*np.pi*hz*t+(hz/60)*phase+hz/1200)
              for hz, level in [(60,.020),(120,.012),(180,.007),(420,.004),
                                (780,.005),(3180,.004),(3420,.003)])
    yield 'short_context', noise[:15*sr], ()
    yield 'silence', np.zeros(16*sr), ()
    pitch = 120+3*np.sin(2*np.pi*.31*t)
    voice_phase = 2*np.pi*np.cumsum(pitch)/sr
    voice = noise+.04*sum(np.sin(k*voice_phase)/k for k in range(1, 12))
    yield 'voice_no_comb', voice, ()
    outage = reference.rising_cosine((t-14)*2)*reference.rising_cosine((34-t)*2)
    yield 'comb_outage', noise+hum*(1-outage), ()
    changing = (.004+.003*reference.rising_cosine((t-20)/8))*np.sin(
        2*np.pi*(540.22*t+.055*np.sin(2*np.pi*.09*t)))
    yield 'tracked_refinement', noise+hum+changing, ()
    shifted = sum(.007*np.sin(2*np.pi*k*60.019*t) for k in (15,19,25,29,35,41))
    yield 'shifted_comb', noise+hum+shifted, ()
    noncomb = .009*np.sin(2*np.pi*837.23*t)+.004*np.sin(
        2*np.pi*(1234.2*t+.05*np.sin(2*np.pi*.11*t)))
    yield 'saved_noncomb', noise+hum+noncomb, (837., 1234.)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--base', type=Path, default=Path(
        'D:/Apollo_15_30-Track' if os.name == 'nt' else '/mnt/d/Apollo_15_30-Track'))
    parser.add_argument('--probe', type=Path)
    parser.add_argument('--synthetic-only', action='store_true')
    args = parser.parse_args()
    if args.output.exists():
        parser.error('Output already exists; preserve completed evidence')
    repo = Path(__file__).resolve().parents[1]
    report = dict(schema=1, backend=accelerated.backend_configuration(), platform=platform.platform(),
                  versions=dict(python=sys.version, numpy=np.__version__, scipy=scipy.__version__,
                                soundfile=sf.__version__, libsndfile=sf.__libsndfile_version__),
                  implementation_sha256={name:digest((repo/name).read_bytes()) for name in (
                      'scripts/denoise_v2.py', 'scripts/denoise_v2_refinement.py',
                      'scripts/denoise_v2_numba.py', 'research/qualify_denoise_numba.py')},
                  cases=[], passed=False)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    def save():
        args.output.write_text(json.dumps(report, indent=2)+'\n')
    def add(row):
        report['cases'].append(row)
        save()
        print(json.dumps(row), flush=True)
        if not row['passed']:
            raise RuntimeError('Equivalence failed: '+row['name'])
    accelerated.warmup()
    for name, samples, seeds in synthetic_cases():
        values, durations = [], []
        for dsp in (reference, accelerated):
            tick = time.perf_counter()
            values.append(dsp.model_window(samples, seeds, defer_compact=True))
            durations.append(time.perf_counter()-tick)
        (a, ra), (b, rb) = values
        diff = difference(a, b)
        add(dict(name=name, type='synthetic_model', input_sha256=digest(samples.tobytes()),
                 samples=len(samples), seeds=seeds, seconds=durations, model_difference=diff,
                 corrected_float32_difference=difference((samples-a).astype('<f4'), (samples-b).astype('<f4')),
                 records_equal=ra == rb, record_sha256=digest(json.dumps(ra, sort_keys=True).encode()),
                 coverage=summary([ra]), passed=diff['exact'] and ra == rb))
    if not args.synthetic_only:
        probe_path = args.probe
        if probe_path is None:
            candidates = sorted((args.base/'.denoise_v2_rollout').glob('*/cluster/probe.json'))
            if len(candidates) != 1:
                parser.error('Specify --probe; expected exactly one saved cluster probe')
            probe_path = candidates[0]
        probe = json.loads(probe_path.read_text())
        report['probe_sha256'] = digest(probe_path.read_bytes())
        source = args.base/'orig_concatenated'/Path(*PureWindowsPath(
            probe['job']['source']['path']).parts[-2:])
        hint = args.base/'denoised_v1'/probe['job']['tape']/f"CH{probe['job']['channel']:02d}"/'cleanup.jsonl'
        rows = saved_seeds(hint, 0, probe['job']['source']['frames'])
        intervals = [(*pair, index) for index, pair in enumerate(probe['intervals'])]
        # The saved last probe is near the end, but does not include the true
        # final partial step; qualify that boundary and an arbitrary crop too.
        frames = sf.info(source).frames
        intervals.append((frames-8000, frames, None))
        intervals.append((probe['intervals'][1][0]+123, probe['intervals'][1][0]+8123, None))
        # Exercise the production chunk path independently for every interval.
        for start, end, index in intervals:
            results, records, durations = [], [], []
            with sf.SoundFile(source) as stream:
                def read(a, n):
                    stream.seek(a)
                    return stream.read(n, dtype='float64')
                for dsp in (reference, accelerated):
                    tick = time.perf_counter()
                    chunks = list(dsp.corrected_chunks(read, frames, start, end,
                        lambda a,b:gather_seeds(rows, a,b)))
                    durations.append(time.perf_counter()-tick)
                    results.append(np.concatenate([v[2] for v in chunks]))
                    records.append([(v[3], v[4]) for v in chunks])
            diff = difference(*results)
            float32 = difference(results[0].astype('<f4'), results[1].astype('<f4'))
            row = dict(name=f'real_render_{index if index is not None else start}',
                       type='real_corrected_chunks', source=str(source), interval=[start,end],
                       seconds=durations, corrected_float64_difference=diff,
                       corrected_float32_difference=float32, records_equal=records[0] == records[1],
                       record_sha256=digest(json.dumps(records[0], sort_keys=True).encode()),
                       output_float32_sha256=digest(results[1].astype('<f4').tobytes()),
                       coverage=summary([r for pair in records[0] for r in pair]),
                       passed=diff['exact'] and float32['exact'] and records[0] == records[1])
            if index is not None:
                prefix = 'windows' if os.name == 'nt' else 'genus'
                saved = probe_path.with_name(f'{prefix}-{index}.npy')
                if not saved.exists():
                    saved = probe_path.with_name(f'windows-{index}.npy')
                expected = np.load(saved, allow_pickle=False)
                row['saved_probe'] = str(saved)
                row['saved_probe_file_sha256'] = digest(saved.read_bytes())
                row['saved_probe_difference'] = difference(expected, results[1].astype('<f4'))
                compare = row['saved_probe_difference']
                row['passed'] &= compare['peak'] <= 2**-22 and compare['rms'] <= 2**-25
                if prefix == 'windows':
                    row['passed'] &= row['saved_probe_file_sha256'] == probe['files'][index]['sha256']
            add(row)
    report['passed'] = all(v['passed'] for v in report['cases'])
    save()


if __name__ == '__main__':
    main()
