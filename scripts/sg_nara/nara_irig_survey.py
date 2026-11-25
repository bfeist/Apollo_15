"""NARA step 1: decode IRIG-B on every track of every NARA tape, over the full file.

Every track is searched, not only the one that usually carries timecode: the playback head
straddled the recorded tracks, so the timecode moves between files within a tape and often
appears on two files at once. Decoding is ``nara_irig.py``. Per track, the output under
``F:/Apollo_15_data/processing/sg_nara/irig/<tape>/<track>.json`` holds every frame candidate
(original-file seconds of Pr, decoded fields, strict flag, quality) and is bound to the source
checksum and the decoder's source checksum; a matching cache is reused, a stale one is moved
aside. ``survey.tsv`` summarizes strict-frame counts per track and 10-minute coverage.

    uv run python scripts/run.py nara_irig_survey.py --tape all --workers 12
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
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

import nara_irig
from nara_sources import DATA_ROOT, NARA_ROOT, Track, tracks

IRIG_ROOT = DATA_ROOT / 'irig'
VERSION = 'nara-irig-v1'


def decoder_sha() -> str:
    return hashlib.sha256(Path(nara_irig.__file__).read_bytes()).hexdigest()


def identity(track: Track) -> dict:
    return dict(version=VERSION, decoder_sha256=decoder_sha(), source_sha256=track.sha256,
                channel=track.channel, sample_rate=track.sample_rate)


def cache_path(root: Path, track: Track) -> Path:
    return root / track.tape / f'{track.name}.json'


def load(root: Path, track: Track) -> dict | None:
    p = cache_path(root, track)
    if not p.is_file():
        return None
    j = json.loads(p.read_text(encoding='utf-8'))
    return j if j.get('identity') == identity(track) else None


def survey_track(track: Track, root: Path) -> str:
    out = cache_path(root, track)
    if load(root, track) is not None:
        return f'{track.key}: up to date'
    if out.is_file():
        stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
        out.rename(out.with_name(f'{out.stem}.superseded-{stamp}.json'))
    out.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    frames, total = nara_irig.decode_stream(track.path, track.channel, track.sample_rate)
    strict = [f for f in frames if f.strict]
    cols = ['t', 'day', 'hours', 'minutes', 'seconds', 'sbs', 'quality', 'strict', 'marks']
    record = dict(
        identity=identity(track), track=track.row(), decoded_duration_s=total,
        decoded=datetime.now(timezone.utc).isoformat(timespec='seconds'),
        seconds=round(time.monotonic() - started, 1), candidates=len(frames), strict=len(strict),
        time_domain='original MP3 decoded seconds (ffmpeg, encoder delay removed), Pr leading edge',
        columns=cols,
        frames=[[round(f.t, 6), f.day, f.hours, f.minutes, f.seconds, f.sbs, round(f.quality, 3),
                 int(f.strict), f.marks] for f in frames])
    tmp = out.with_suffix('.partial')
    tmp.write_text(json.dumps(record, separators=(',', ':')) + '\n', encoding='utf-8')
    tmp.replace(out)
    return f'{track.key}: {len(strict)} strict / {len(frames)} candidates in {record["seconds"]:.0f} s'


def summary(root: Path, items: list[Track]) -> None:
    rows = ['tape\ttrack\tduration_s\tstrict\tcandidates\tstrict_10min_bins\tbins\tfirst_gmt\tlast_gmt']
    for t in items:
        j = load(root, t)
        if j is None:
            continue
        f = np.array(j['frames'], dtype=float).reshape(-1, 9)
        s = f[f[:, 7] == 1] if len(f) else f
        nb = int(np.ceil(t.duration_s / 600))
        hit = len(np.unique((s[:, 0] // 600).astype(int))) if len(s) else 0
        g = lambda r: f'{int(r[1]):03d}:{int(r[2]):02d}:{int(r[3]):02d}:{int(r[4]):02d}'
        rows.append(f'{t.tape}\t{t.name}\t{t.duration_s:.1f}\t{len(s)}\t{len(f)}\t{hit}\t{nb}\t'
                    f'{g(s[0]) if len(s) else ""}\t{g(s[-1]) if len(s) else ""}')
    (root / 'survey.tsv').write_text('\n'.join(rows) + '\n', encoding='utf-8')


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    p.add_argument('--tape', nargs='+', required=True, help="tapes (255-PAOa-640-AAA or 640-AAA), or 'all'")
    p.add_argument('--workers', type=int, default=max(1, (os.cpu_count() or 2) // 2))
    p.add_argument('--nara-root', type=Path, default=NARA_ROOT)
    p.add_argument('--output-root', type=Path, default=IRIG_ROOT)
    args = p.parse_args(argv)
    items = tracks(args.nara_root, args.tape)
    args.output_root.mkdir(parents=True, exist_ok=True)
    todo = [t for t in items if load(args.output_root, t) is None]
    print(f'{len(items)} tracks, {len(todo)} to decode ({args.workers} workers) -> {args.output_root}', flush=True)
    failed = 0
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        jobs = {pool.submit(survey_track, t, args.output_root): t for t in todo}
        for job in as_completed(jobs):
            try:
                msg = job.result()
            except Exception as error:  # noqa: BLE001 - report and continue
                msg, failed = f'{jobs[job].key}: FAILED, {error}', failed + 1
            print(msg, flush=True)
    all_tracks = tracks(args.nara_root, ['all'])
    summary(args.output_root, all_tracks)
    print(f'summary: {args.output_root / "survey.tsv"}')
    return 1 if failed else 0


if __name__ == '__main__':
    raise SystemExit(main())
