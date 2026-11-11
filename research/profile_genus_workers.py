"""Isolated, synchronized DSP concurrency benchmark; never writes production audio."""
import os
for name in ('OPENBLAS_NUM_THREADS', 'MKL_NUM_THREADS', 'OMP_NUM_THREADS',
             'BLIS_NUM_THREADS', 'NUMEXPR_NUM_THREADS'):
    os.environ[name] = '1'

import argparse
import hashlib
import json
import multiprocessing as mp
from pathlib import Path
import queue as queue_module
import sys
import time
import traceback

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from _bootstrap import install as _install
_install()
import numpy as np
import soundfile as sf
from denoise_v2 import model_window
from render_denoise_v2 import saved_seeds, gather_seeds


def worker(event, queue, repeats, backend='numpy'):
    try:
        render_model = model_window
        if backend == 'numba':
            import denoise_v2_numba as accelerated
            accelerated.warmup()
            render_model = accelerated.model_window
        base = Path('D:/Apollo_15_30-Track' if os.name == 'nt' else '/mnt/d/Apollo_15_30-Track')
        source = next((base/'orig_concatenated/A15_T510077').glob('*_CH3_*.wav'))
        a, b = 2048*8000, 2184*8000
        with sf.SoundFile(source) as stream:
            stream.seek(a)
            samples = stream.read(b-a, dtype='float64')
        hints = gather_seeds(saved_seeds(base/'denoised_v1/A15_T510077/CH03/cleanup.jsonl', a, b), a, b)
        queue.put(dict(ready=True, input_sha256=hashlib.sha256(samples.tobytes()).hexdigest()))
        event.wait()
        start, cpu = time.perf_counter(), time.process_time()
        durations = []
        for _ in range(repeats):
            tick = time.perf_counter()
            result, record = render_model(samples, hints, defer_compact=True)
            durations.append(time.perf_counter()-tick)
        queue.put(dict(wall_seconds=time.perf_counter()-start,
                       cpu_seconds=time.process_time()-cpu, iterations=durations,
                       components=len(record['components']),
                       output_sha256=hashlib.sha256(result.tobytes()).hexdigest()))
    except BaseException:
        queue.put(dict(error=traceback.format_exc()))
        raise


def temperatures():
    values = {}
    for folder in Path('/sys/class/hwmon').glob('hwmon*'):
        if (folder/'name').read_text().strip() != 'k10temp':
            continue
        for path in folder.glob('temp*_input'):
            label = path.with_name(path.name.replace('_input', '_label'))
            values[label.read_text().strip() if label.exists() else path.stem] = int(path.read_text())/1000
    return values


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--counts', type=int, nargs='+', default=[1, 4, 8, 12, 16, 24, 32])
    parser.add_argument('--repeats', type=int, default=2)
    parser.add_argument('--backend', choices=['numpy', 'numba'], default='numpy')
    parser.add_argument('--balanced-affinity', action='store_true',
                        help='Spread each worker count across both Genus cache groups')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error('Output already exists')
    ctx = mp.get_context('spawn')
    results = []
    for count in args.counts:
        original_affinity = os.sched_getaffinity(0) if hasattr(os, 'sched_getaffinity') else None
        if args.balanced_affinity:
            if original_affinity is None:
                parser.error('Balanced affinity is supported only on Genus/Linux')
            physical = [0,8,2,10,4,12,6,14,1,9,3,11,5,13,7,15]
            order = physical+[cpu+16 for cpu in physical]
            if not 1 <= count <= len(order):
                parser.error('Balanced Genus affinity requires 1–32 workers')
            os.sched_setaffinity(0, order[:count])
        event, queue = ctx.Event(), ctx.Queue()
        workers = [ctx.Process(target=worker, args=(event, queue, args.repeats, args.backend)) for _ in range(count)]
        try:
            for process in workers:
                process.start()
            ready = [queue.get(timeout=120) for _ in workers]
            if any('error' in row for row in ready):
                raise RuntimeError(ready)
            assert len({row['input_sha256'] for row in ready}) == 1
            print(json.dumps(dict(starting_workers=count, temperature=temperatures())), flush=True)
            tick = time.perf_counter()
            event.set()
            rows, thermal = [], []
            while len(rows) < count:
                thermal.append(temperatures())
                try:
                    rows.append(queue.get(timeout=2))
                except queue_module.Empty:
                    if any(p.exitcode not in (None, 0) for p in workers):
                        raise RuntimeError('Benchmark child failed')
            elapsed = time.perf_counter()-tick
            if any('error' in row for row in rows):
                raise RuntimeError(rows)
            assert len({row['output_sha256'] for row in rows}) == 1
            row = dict(workers=count, repeats=args.repeats, backend=args.backend,
                       cpu_affinity=sorted(os.sched_getaffinity(0)) if original_affinity is not None else None,
                       elapsed_seconds=elapsed,
                       model_audio_h_per_h=count*args.repeats*64/elapsed,
                       peak_temperature={key:max(v[key] for v in thermal) for key in thermal[0]},
                       measurements=rows, input_sha256=ready[0]['input_sha256'])
            results.append(row)
            args.output.write_text(json.dumps(results, indent=2))
            print(json.dumps({k:v for k,v in row.items() if k!='measurements'}), flush=True)
        finally:
            for process in workers:
                process.join(timeout=2)
                if process.is_alive():
                    process.terminate()
                    process.join()
            if original_affinity is not None:
                os.sched_setaffinity(0, original_affinity)


if __name__ == '__main__':
    main()
