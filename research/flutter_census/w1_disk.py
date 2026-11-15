"""W1: disk throughput from D: (orig_concatenated) on this Windows machine.

Compares the access patterns that matter for a full-archive flutter pass:
  A) long sequential reads, 30 channels round-robin in ~9.6 MB chunks
     (the fast block-scan pattern);
  B) per-window seek reads, ~272 kB per channel per 64 s window
     (the e5/e7 per-window pattern), same total volume;
  C) aggregate throughput with 1/2/4/6 concurrent worker processes, each
     sequentially reading a DIFFERENT tape (cache isolation between tests).
Research only; reads are opened read-only and closed.
"""
import json, os, sys, time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
import numpy as np
import soundfile as sf

sys.path.insert(0, str(Path(__file__).parent))
from common import ORIG, inventory, SR

os.environ.setdefault('OMP_NUM_THREADS', '1')


def chan_files(tape):
    inv = inventory(tape)
    return [inv[c]['path'] for c in sorted(inv)]


def seq_read(tape, seconds, chunk_s=120., frac=0.8):
    """Round-robin sequential reads of `seconds` from all 30 channels,
    starting at frac*frames (deep offset = cold cache)."""
    files = chan_files(tape)
    total = 0
    t0 = time.time()
    frames = [sf.info(p).frames for p in files]
    lhs = [(sf.SoundFile(p), int(fr * frac)) for p, fr in zip(files, frames)]
    try:
        done = 0
        while done < seconds:
            n = int(min(chunk_s, seconds - done) * SR)
            for f, off in lhs:
                f.seek(off + int(done * SR))
                buf = f.read(n, dtype='float32')
                total += buf.nbytes
                if len(buf) < n:
                    return total, time.time() - t0, False
            done += n / SR
    finally:
        for f, _ in lhs:
            f.close()
    return total, time.time() - t0, True


def seek_read(tape, seconds, win_s=68., frac=0.8):
    """Per-window seek reads: every window opens each channel, seeks, reads."""
    files = chan_files(tape)
    total = 0
    t0 = time.time()
    frames = [sf.info(p).frames for p in files]
    offs = [int(fr * frac) for fr in frames]
    off = 0
    while off < seconds:
        n = int(win_s * SR)
        for p, o in zip(files, offs):
            with sf.SoundFile(p) as f:
                f.seek(min(o + int(off * SR), f.frames - 1))
                buf = f.read(n, dtype='float32')
                total += buf.nbytes
        off += win_s
    return total, time.time() - t0, True


def _worker(args):
    tape, seconds, mode = args
    t0 = time.time()
    if mode == 'seq':
        total, _, ok = seq_read(tape, seconds, frac=0.8)
    else:
        total, _, ok = seek_read(tape, seconds, frac=0.8)
    return dict(tape=tape, mode=mode, mb=round(total / 1e6, 1),
                s=round(time.time() - t0, 2), mbps=round(total / 1e6 / (time.time() - t0), 1))


def main():
    out = {}
    # A/B at deep offsets (cold regions) on two different tapes
    mb, s, _ = seq_read('A15_T510077', 300.)
    out['A_seq_30ch_300s'] = dict(mb=round(mb / 1e6, 1), s=round(s, 2),
                                  mbps=round(mb / 1e6 / s, 1))
    mb, s, _ = seek_read('A15_T510084', 300.)
    out['B_seek_30ch_300s'] = dict(mb=round(mb / 1e6, 1), s=round(s, 2),
                                   mbps=round(mb / 1e6 / s, 1))
    # C: aggregate with N workers on distinct tapes, deep offsets
    tapes = [f'A15_T51007{k}' for k in (3, 5, 6, 7, 8, 9)]
    for n in (1, 2, 4, 6):
        t0 = time.time()
        with ProcessPoolExecutor(n) as ex:
            res = list(ex.map(_worker, [(t, 120., 'seq') for t in tapes[:n]]))
        wall = time.time() - t0
        tot = sum(r['mb'] for r in res)
        out[f'C_workers_{n}'] = dict(wall_s=round(wall, 2), total_mb=round(tot, 1),
                                     agg_mbps=round(tot / wall, 1),
                                     per_worker_mbps=round(np.mean([r['mbps'] for r in res]), 1))
    Path(__file__).with_name('w1_disk.json').write_text(json.dumps(out, indent=1))
    print(json.dumps(out, indent=1))


if __name__ == '__main__':
    main()
