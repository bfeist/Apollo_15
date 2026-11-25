"""Final transcript of the respeeded NARA flight tapes (the analogue of ``sg_retimed_transcribe.py``).

Transcribes the transcription mix of each tape written by ``nara_respeed.py``
(``F:/Apollo_15_data/processing/sg_nara/respeed/<tape>/<short>_mix.wav``): the equal-weight mean
of the tape's files with the timecode-bearing stretches gated out. Its clock runs at mission rate:
output seconds = tape seconds + D(tape seconds), and GET = output seconds + the segment constant
recorded in ``<short>.json``. Word and segment ``sf``/``ef`` are frames on that output clock.

Uses the shared recognizer and chunk store (scripts/00_common) with its own store and lock under
``F:/Apollo_15_data/processing/sg_nara/retimed_transcripts``, so it runs alongside the other
transcription stages. A tape is selected only when its render provenance is complete (the mix's
frame count matches the WAV); the provenance checksum and map checksum are part of the source
identity, so a re-rendered tape gets a new identity.

Subcommands are those of stage 06a: inventory, estimate, run, status, watch, rescue, verify, stop.
For example:
  uv run python scripts/run.py nara_retimed_transcribe.py run --tape all --batch-size 8 --prep-workers 4
"""
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))
from _bootstrap import install as _install
_install()

import hashlib
import json
import os
from pathlib import Path

from audio_sources import Source, wav_layout
from nara_sources import DATA_ROOT
import transcribe_cli
from transcribe_cli import Collection

RESPEED_ROOT = Path(os.environ.get('A15_NARA_RESPEED_ROOT', DATA_ROOT / 'respeed'))
TRANSCRIPT_ROOT = DATA_ROOT / 'retimed_transcripts'
EVIDENCE_ROOT = TRANSCRIPT_ROOT / 'evidence'


def wanted(tape: str, names) -> bool:
    if not names or 'all' in names:
        return True
    short = tape.split('-', 2)[-1]  # 255-PAOa-640-AAA -> 640-AAA
    return tape in names or short in names


def retimed_sources(root, names=None, hashes=None, errors=None):
    out = []
    for folder in sorted(p for p in Path(root).iterdir() if p.is_dir() and '.' not in p.name):
        tape = folder.name
        if not wanted(tape, names):
            continue
        short = tape.split('-', 2)[-1]
        key = f'sg_nara/{short}'
        try:
            meta_path = folder / f'{short}.json'
            wav = folder / f'{short}_mix.wav'
            if not meta_path.is_file() or not wav.is_file():
                raise ValueError('no respeed provenance (render not finished)')
            meta_bytes = meta_path.read_bytes()
            meta = json.loads(meta_bytes)
            layout = wav_layout(wav)
            mix = meta['outputs']['mix']
            if mix['frames'] != layout.frames:
                raise ValueError(f"provenance frames {mix['frames']} != WAV frames {layout.frames}")
            stat = wav.stat()
            checksum = hashes.get(wav) if hashes else ''
            out.append(Source(
                key=key, collection='sg_nara_respeed', relpath=f'{tape}/{wav.name}', path=str(wav.resolve()),
                sample_rate=layout.rate, frames=layout.frames, source_channels=layout.channels,
                mix='single_channel', sample_format=layout.description, time_domain='sg_nara_respeed_output_seconds',
                content_sha256=checksum or '', hash_origin='computed' if checksum else 'pending',
                size=stat.st_size, mtime_ns=stat.st_mtime_ns, tape=tape, label=f'NARA flight tape {short} (retimed)',
                review=dict(sha256=hashlib.sha256(meta_bytes).hexdigest(), map_sha256=meta['map']['sha256'],
                            version=meta.get('version'), sources_sha256=meta.get('sources'))))
        except (OSError, ValueError, KeyError) as error:
            if errors is None:
                raise
            errors.append(dict(key=key, error=str(error)))
    return out


def add_selection(group):
    group.add_argument('--tape', action='append', default=[], help="NARA tape such as 640-AAA, or 'all'")
    group.add_argument('--respeed-root', type=Path, default=RESPEED_ROOT)


TAPES = Collection(
    name='sg_nara_respeed', transcript_root=TRANSCRIPT_ROOT, evidence_root=EVIDENCE_ROOT, add_selection=add_selection,
    has_selection=lambda args: bool(args.tape),
    select=lambda args, cache, hash_external, errors: retimed_sources(args.respeed_root, args.tape, cache, errors),
    scope=lambda args: dict(tape=args.tape, respeed_root=str(args.respeed_root)),
    selection_hint='Select tapes with --tape NAME or --tape all', window_help='retimed output seconds')


def main(argv=None):
    return transcribe_cli.main(TAPES, __doc__, list(_sys.argv[1:] if argv is None else argv))


if __name__ == '__main__':
    raise SystemExit(main())
