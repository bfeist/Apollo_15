"""NARA step 4: respeed every file of a flight tape to mission rate on one shared timeline.

Inputs per tape: the alignment (``nara_align.py``: each file's ``start_s`` on the tape axis)
and the timing map (``nara_tape_map.py``: D(tau) on the tape axis; output seconds
u = tau + D(tau) run at mission rate; GET = u + segment constant). Each output sample at u is
taken from every file at file seconds = tau(u) - start_s, with the windowed-sinc resampler of
``sg_respeed.py`` (64-tap Kaiser, 2,048-phase table; numba). Every MP3 is decoded sequentially
from its first sample (ffmpeg), the origin the survey and the alignment measured on.

Outputs under ``F:/Apollo_15_data/processing/sg_nara/respeed/<tape>/`` (``--output-root``):

* ``<tape>_<track>.wav``   one per file, mono, 44.1 kHz, 24-bit, all the same length and on
  the same timeline (Adobe Audition can stack them directly); silence where a file has no
  audio (its later start or earlier end)
* ``<tape>_mix.wav``       the transcription input: the equal-weight mean of the files, each
  gated out (``CROSSFADE_S`` ramps) wherever it carries timecode (frame candidates within
  ``GATE_PAD_S``), so the 1 kHz IRIG-B does not reach the recognizer. Where every file
  carries timecode the plain mean is used.
* ``<tape>.json``          provenance: inputs and their checksums, per-file placement (and
  whether it was measured or assumed), the GET placement of the output (segments, with
  GET = output seconds + constant), render statistics and decoded checksums

A file the alignment could not place is rendered at the reference file's start and marked
``start_assumed``: the rate correction holds, its GET placement is provisional.

Outputs whose provenance matches the current inputs (by checksum) are kept; other outputs are
never overwritten (``--replace`` moves them aside). A tape being rendered holds
``<output-root>/<tape>.lock``, so several render processes, on this machine and on genus, can
share one tape list: each takes the next tape nobody holds. Examples:

    uv run python scripts/run.py nara_respeed.py --tape all --workers 4

and on genus (``/mnt/f`` is this machine's F:; its MP3 decode was checked bit-identical), from
the other end of the list:

    ssh bfeist@192.168.0.3 'cd /mnt/f/_repos/Apollo_15 && \
      A15_NARA_AUDIO_ROOT=/mnt/f/Apollo_15_data/raw/audio/NARA \
      A15_PROCESSING_ROOT=/mnt/f/Apollo_15_data/processing NUMBA_CACHE_DIR=$HOME/.cache/numba \
      ~/.local/share/apollo15-nara-venv/bin/python scripts/run.py nara_respeed.py \
      --tape all --workers 2 --reverse'

A stale lock (a render that was killed) must be removed by hand after checking that process.
"""
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))
from _bootstrap import install as _install
_install()

import argparse
import hashlib
import json
import os
import socket
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import soundfile as sf

import nara_irig
from nara_align import ALIGN_ROOT
from nara_irig_survey import IRIG_ROOT, load as load_survey
from nara_sources import DATA_ROOT, NARA_ROOT, by_tape, tape_short, tracks
from nara_tape_map import MAP_ROOT
from sg_respeed import BLOCK_S, HALO, kernel_table, sinc_at

OUTPUT_ROOT = DATA_ROOT / 'respeed'
VERSION = 'nara-respeed-v1'
SR = 44100
FULL = 2 ** 23
GATE_BIN_S = 1.0       # timecode gating resolution
GATE_PAD_S = 3.0       # a file is gated this far around each of its frame candidates
CROSSFADE_S = 1.0


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as f:
        while chunk := f.read(16 << 20):
            h.update(chunk)
    return h.hexdigest()


class Stream:
    """Sequentially decoded samples of one file, addressable by absolute sample index.

    Indices before 0 or past the end read as zeros. Requests must move forward; samples
    before the last request's left edge are discarded.
    """

    def __init__(self, track):
        self.proc = nara_irig.ffmpeg_reader(track.path, track.channel, SR)
        self.base, self.buf, self.eof = 0, np.empty(0, np.float32), False
        self.read_frames = 0

    def _fill(self, upto: int):
        while not self.eof and self.base + len(self.buf) < upto:
            raw = self.proc.stdout.read(SR * 4 * 30)
            if not raw:
                self.eof = True
                self.proc.wait()
                if self.proc.returncode:
                    raise RuntimeError(f'ffmpeg exited with {self.proc.returncode}')
                break
            x = np.frombuffer(raw[:len(raw) // 4 * 4], np.float32)
            self.buf = np.r_[self.buf, x]
            self.read_frames += len(x)

    def get(self, left: int, right: int) -> np.ndarray:
        self._fill(right)
        drop = max(0, min(left, self.base + len(self.buf)) - self.base)
        if drop:
            self.buf, self.base = self.buf[drop:], self.base + drop
        out = np.zeros(right - left, np.float32)
        a, b = max(left, self.base), min(right, self.base + len(self.buf))
        if b > a:
            out[a - left:b - left] = self.buf[a - self.base:b - self.base]
        return out

    def finish(self):
        self._fill(1 << 62)
        return self.read_frames


def gate_weights(track, start_s: float, irig_root: Path, n_bins: int) -> np.ndarray:
    """1 where the file carries no timecode, 0 where it does (per GATE_BIN_S of tape time)."""
    j = load_survey(irig_root, track)
    w = np.ones(n_bins)
    if j and j['frames']:
        t = np.array(j['frames'], float).reshape(-1, 9)[:, 0] + start_s
        pad = int(np.ceil(GATE_PAD_S / GATE_BIN_S))
        for b in np.unique((t // GATE_BIN_S).astype(int)):
            w[max(0, b - pad):max(0, min(n_bins, b + pad + 1))] = 0
    # crossfade: moving average over CROSSFADE_S
    k = max(1, int(round(CROSSFADE_S / GATE_BIN_S)))
    return np.convolve(w, np.ones(k) / k, mode='same') if k > 1 else w


def provenance(tape, align_path, map_path, npz_path, items) -> dict:
    return dict(tape=tape, version=VERSION,
                alignment=dict(path=str(align_path), sha256=sha256_file(align_path)),
                map=dict(path=str(map_path), sha256=sha256_file(map_path)),
                grid=dict(path=str(npz_path), sha256=sha256_file(npz_path)),
                sources={t.name: t.sha256 for t in items})


def render_tape(tape, items, align_root, map_root, irig_root, out_root, replace) -> str:
    short = tape_short(tape)
    align_path, map_path = align_root / f'{tape}.json', map_root / f'{tape}.json'
    if not map_path.is_file():
        return f'{tape}: SKIPPED, no timing map'
    m = json.loads(map_path.read_text(encoding='utf-8'))
    if m['status'] != 'mapped':
        return f'{tape}: SKIPPED, {m["status"]} ({m.get("note", "")}); no correction applied'
    npz_path = map_root / m['grid']['file']
    align = json.loads(align_path.read_text(encoding='utf-8'))
    folder = out_root / tape
    meta_path = folder / f'{short}.json'
    want = provenance(tape, align_path, map_path, npz_path, items)
    if current(meta_path, want):
        return f'{tape}: up to date'
    lock = out_root / f'{tape}.lock'
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return f'{tape}: in progress elsewhere ({lock.read_text(encoding="utf-8").strip()})'
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        f.write(f'{socket.gethostname()} pid {os.getpid()} {datetime.now().isoformat(timespec="seconds")}\n')
    try:
        if current(meta_path, want):  # finished elsewhere while we waited
            return f'{tape}: up to date'
        if folder.exists():
            if meta_path.is_file() and not replace:
                return f'{tape}: SKIPPED, existing output was made from other inputs (use --replace)'
            stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
            folder.rename(folder.with_name(f'{folder.name}.superseded-{stamp}'))
        folder.mkdir(parents=True)
        return _render(tape, short, items, align, m, npz_path, folder, meta_path, want, irig_root)
    finally:
        lock.unlink(missing_ok=True)


def current(meta_path: Path, want: dict) -> bool:
    """Outputs exist and were made from the same inputs (checksums only: paths differ by host)."""
    if not meta_path.is_file():
        return False
    have = json.loads(meta_path.read_text(encoding='utf-8'))
    same = (have.get('version') == want['version'] and have.get('sources') == want['sources']
            and all((have.get(k) or {}).get('sha256') == want[k]['sha256'] for k in ('alignment', 'map', 'grid')))
    return same and all((meta_path.parent / o['file']).is_file() for o in have.get('outputs', {}).values())


def _render(tape, short, items, align, m, npz_path, folder, meta_path, want, irig_root) -> str:
    g = np.load(npz_path)
    tau, D = g['t'], g['D']
    u = tau + D
    duration = float(tau[-1])
    count = int(round(u[-1] * SR))
    ref = align['reference']
    files, gates = {}, {}
    n_bins = int(np.ceil(duration / GATE_BIN_S)) + 1
    for t in items:
        f = align['files'][t.name]
        assumed = f['start_s'] is None
        start = align['files'][ref]['start_s'] if assumed else f['start_s']
        files[t.name] = dict(start_s=start, start_assumed=assumed, placed_by=f['placed_by'],
                             clock_minus_programme_ms=f.get('clock_minus_programme_ms'))
        gates[t.name] = gate_weights(t, start, irig_root, n_bins)
    speed = np.diff(tau) / np.diff(u)
    table = kernel_table(min(1., 1. / float(speed.max())) * .97)
    streams = {t.name: Stream(t) for t in items}
    names = [t.name for t in items]
    outs, tmp = {}, {}
    for n in names + ['mix']:
        p = folder / f'{short}_{n}.partial.wav'
        tmp[n] = p
        outs[n] = sf.SoundFile(p, 'w', samplerate=SR, channels=1, subtype='PCM_24',
                               format='RF64' if count * 3 > 4_000_000_000 else 'WAV')
    stats = {n: dict(peak=0., clipped=0, digest=hashlib.sha256()) for n in names + ['mix']}
    started = last = time.monotonic()
    block = int(round(BLOCK_S * SR))
    for s0 in range(0, count, block):
        times = np.arange(s0, min(count, s0 + block)) / SR
        tau_out = np.interp(times, u, tau)               # tape seconds of each output sample
        bins = np.clip((tau_out / GATE_BIN_S).astype(int), 0, n_bins - 1)
        num, den, plain = np.zeros(len(times)), np.zeros(len(times)), np.zeros(len(times))
        for n in names:
            pos = (tau_out - files[n]['start_s']) * SR   # fractional file samples
            left = int(np.floor(pos[0])) - HALO
            right = int(np.ceil(pos[-1])) + HALO + 1
            x = streams[n].get(left, right)
            y = sinc_at(x[:, None], pos - left, table)[:, 0].astype(np.float64)
            w = gates[n][bins]
            num += w * y
            den += w
            plain += y
            write(outs[n], stats[n], y)
        mix = np.where(den > 1e-3, num / np.maximum(den, 1e-3), plain / len(names))
        write(outs['mix'], stats['mix'], mix)
        now = time.monotonic()
        if now - last >= 60:
            print(f'  {short}: {100 * (s0 + block) / count:.0f}% ({now - started:.0f} s)', flush=True)
            last = now
    read = {n: streams[n].finish() for n in names}
    outputs = {}
    for n in names + ['mix']:
        outs[n].close()
        final = folder / f'{short}_{n}.wav'
        tmp[n].replace(final)
        st = stats[n]
        outputs[n] = dict(file=final.name, frames=count, peak_before_clipping=round(st['peak'], 6),
                          clipped_samples=st['clipped'], decoded_int24_sha256=st['digest'].hexdigest())
    for n in names:
        files[n]['decoded_source_frames'] = read[n]
    segments = [dict(source_start_s=s['source_start_s'], source_end_s=s['source_end_s'],
                     output_start_s=s['output_start_s'], output_end_s=s['output_end_s'],
                     get_equals_output_plus_s=s['get_equals_output_plus_s'], get_first=s['get_first'],
                     get_last=s['get_last'], residual_rms_ms=s['residual_rms_ms'],
                     untimed_before_s=s['untimed_before_s'], untimed_after_s=s['untimed_after_s'])
                for s in m['segments']]
    meta = {**want, 'rendered': datetime.now(timezone.utc).isoformat(timespec='seconds'), 'host': socket.gethostname(),
            'sample_rate': SR, 'output_frames': count, 'output_duration_s': count / SR,
            'tape_duration_s': duration, 'added_s': float(D[-1]),
            'rate_ppm_min': m['rate_ppm_min'], 'rate_ppm_max': m['rate_ppm_max'],
            'timecode_coverage_share': m['timecode_coverage_share'],
            'files': files, 'outputs': outputs, 'segments': segments,
            'mix': dict(rule='equal-weight mean of the files without timecode; plain mean where all carry it',
                        gate_bin_s=GATE_BIN_S, gate_pad_s=GATE_PAD_S, crossfade_s=CROSSFADE_S),
            'time_domain': 'output seconds = tape seconds + D(tape seconds); GET = output seconds + segment '
                           'constant; file seconds = tape seconds - start_s',
            'caveats': ['Timecode and programme can sit 0.683 s apart along the tape (nara_align.py '
                        'audio_checks); GET is that of the timecode. Files are mutually aligned on programme.'],
            'render': dict(seconds=round(time.monotonic() - started, 1),
                           resampler='sg_respeed.sinc_at: 64-tap Kaiser windowed sinc, 2,048-phase table; '
                                     f'{HALO}-sample halo; {BLOCK_S:g} s blocks; one inverse map for every file')}
    meta_path.write_text(json.dumps(meta, indent=1) + '\n', encoding='utf-8')
    clipped = sum(o['clipped_samples'] for o in outputs.values())
    return f'{tape}: done, +{D[-1]:.1f} s, {count / SR / 3600:.2f} h, {clipped} clipped samples, {meta["render"]["seconds"]:.0f} s'


def write(stream, st, y: np.ndarray) -> None:
    st['peak'] = max(st['peak'], float(np.abs(y).max()) if len(y) else 0.)
    q = np.round(y * FULL)
    st['clipped'] += int(np.count_nonzero((q > FULL - 1) | (q < -FULL)))
    q = np.clip(q, -FULL, FULL - 1).astype(np.int32)
    st['digest'].update(q.tobytes())
    stream.write(q << 8)  # soundfile takes the top 24 bits of int32 for PCM_24


def _threads(n: int) -> None:
    import numba
    numba.set_num_threads(n)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    p.add_argument('--tape', nargs='+', required=True, help="tapes (255-PAOa-640-AAA or 640-AAA), or 'all'")
    p.add_argument('--workers', type=int, default=3, help='tapes at once (each uses its share of the cores)')
    p.add_argument('--nara-root', type=Path, default=NARA_ROOT)
    p.add_argument('--irig-root', type=Path, default=IRIG_ROOT)
    p.add_argument('--align-root', type=Path, default=ALIGN_ROOT)
    p.add_argument('--map-root', type=Path, default=MAP_ROOT)
    p.add_argument('--output-root', type=Path, default=OUTPUT_ROOT)
    p.add_argument('--replace', action='store_true', help='move aside outputs made from other inputs and redo them')
    p.add_argument('--reverse', action='store_true', help='take tapes from the end of the list (a second host)')
    args = p.parse_args(argv)
    tapes = {k: v for k, v in by_tape(tracks(args.nara_root, args.tape)).items()
             if (args.map_root / f'{k}.json').is_file()}
    if args.reverse:
        tapes = dict(reversed(list(tapes.items())))
    args.output_root.mkdir(parents=True, exist_ok=True)
    print(f'{len(tapes)} tapes -> {args.output_root} ({args.workers} workers)', flush=True)
    failed = 0
    threads = max(1, (os.cpu_count() or 1) // args.workers)
    with ProcessPoolExecutor(max_workers=args.workers, initializer=_threads, initargs=(threads,)) as pool:
        jobs = {pool.submit(render_tape, tape, items, args.align_root, args.map_root, args.irig_root,
                            args.output_root, args.replace): tape for tape, items in tapes.items()}
        for job in as_completed(jobs):
            try:
                message = job.result()
            except Exception as error:  # noqa: BLE001 - report and continue with the other tapes
                message = f'{jobs[job]}: FAILED, {error}'
            failed += 'FAILED' in message
            print(message, flush=True)
    print(f'finished: {len(tapes) - failed} ok or skipped, {failed} failed')
    return 1 if failed else 0


if __name__ == '__main__':
    raise SystemExit(main())
