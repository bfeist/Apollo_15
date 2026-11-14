"""Resume a byte-for-byte checkpoint copy; never run the production coordinator.

Exercises the production render/resume reader and the execution adapter's
checkpoint writer. Only the explicitly isolated sandbox and new report are
written. No publication, heartbeat, production stop flag or scheduling occurs.
"""
import os
for name in ('OPENBLAS_NUM_THREADS', 'OMP_NUM_THREADS', 'MKL_NUM_THREADS',
             'BLIS_NUM_THREADS', 'NUMEXPR_NUM_THREADS'):
    os.environ[name] = '1'

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sys
import time
import traceback

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from _bootstrap import install
install()
import soundfile as sf
import denoise_v2_accelerated as accelerated


def sha256(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--host', choices=['windows', 'genus'], required=True)
    parser.add_argument('--sandbox', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--run-seconds', type=float, default=180)
    args = parser.parse_args()
    if args.sandbox.exists() or args.output.exists():
        parser.error('Use new sandbox and report paths; preserve previous evidence')
    cluster = accelerated.cluster
    config = cluster.read_json(args.config)
    cluster.check_code(config)
    execution_id, execution = accelerated.active_execution(config)
    accelerated.load_execution(config, execution_id, args.host)
    session = cluster.session_path(config)
    statuses = cluster.read_json(session/'status.json')['jobs']
    key = min((key for key, state in statuses.items() if state.get('host') == args.host
               and state.get('status') == 'interrupted' and state.get('cursor', 0) > 0),
              key=lambda key: statuses[key]['cursor'])
    job = next(j for j in cluster.read_json(session/'jobs.json') if cluster.job_key(j).as_posix() == key)
    if args.host == 'genus':
        job = cluster.mapped_job(job, config['remote_data'])
        _, _, work = cluster.scratch_paths(config, cluster.remote_freeze(config))
        hints = Path(config['remote_data'])/'denoised_v1'
    else:
        work = session.parent/'work'
        hints = cluster.ARCHIVE
    source_stage = work/key
    saved = cluster.read_json(source_stage/'checkpoint.json')
    names = ('checkpoint.json', 'denoised.json', 'cleanup.jsonl',
             'unity.wav' if 'normalization' in saved['identity']['frozen'] else 'denoised.wav')
    before = {name:sha256(source_stage/name) for name in names}
    # Never hard-link writable smoke audio to production staging.
    args.sandbox.mkdir(parents=True)
    for name in names:
        shutil.copy2(source_stage/name, args.sandbox/name)
    assert before == {name:sha256(args.sandbox/name) for name in names}
    accelerated.private_writer(execution_id, execution)
    writer = sys.modules['_apollo15_accelerated_writer']
    record = dict(saved['identity'])
    thresholds = record.pop('quality_thresholds')
    start = time.monotonic()
    class Deadline:
        def exists(self):
            return time.monotonic()-start >= args.run_seconds
    code, error = 2, None
    try:
        source = Path(job['source']['path'])
        writer.render_float(source, args.sandbox, sf.info(source), hints/key/'cleanup.jsonl',
                            record, thresholds, lambda v:print(v, flush=True), True, Deadline())
    except InterruptedError as exc:
        if str(exc) == 'Stopped after the last committed checkpoint':
            code = accelerated.CHECKPOINT_EXIT
        else:
            error = traceback.format_exc()
    except Exception:
        error = traceback.format_exc()
    elapsed = time.monotonic()-start
    final = cluster.read_json(args.sandbox/'checkpoint.json')
    prefix_checks = {}
    for name, length, expected in ((names[-1], len(writer.wav_header(job['source']['frames'], 8000))+4*saved['cursor'], saved['audio_sha256']),
                                   ('cleanup.jsonl', saved['log_bytes'], saved['log_sha256'])):
        with (args.sandbox/name).open('rb') as stream:
            prefix_checks[name] = writer.prefix(stream, length).hexdigest() == expected
    after = {name:sha256(source_stage/name) for name in names}
    segments = final.get('acceleration', {}).get('segments', [])
    passed = (code == accelerated.CHECKPOINT_EXIT and final['cursor'] > saved['cursor']
              and all(prefix_checks.values()) and before == after
              and segments[-1]['execution_id'] == execution_id
              and segments[-1]['end_frame'] == final['cursor'])
    report = dict(host=args.host, key=key, isolated=True, execution_id=execution_id,
                  source_stage=str(source_stage), sandbox=str(args.sandbox),
                  run_seconds=args.run_seconds, elapsed_seconds=elapsed, exit_code=code,
                  cursor_before=saved['cursor'], cursor_after=final['cursor'],
                  source_audio_h_per_h=(final['cursor']-saved['cursor'])/8000/elapsed,
                  acceleration=final.get('acceleration'), preserved_prefix=prefix_checks,
                  source_sha256_before=before, source_sha256_after=after, error=error, passed=passed)
    with args.output.open('x') as stream:
        json.dump(report, stream, indent=2)
        stream.write('\n')
    print(json.dumps(report), flush=True)
    return code if passed else 2


if __name__ == '__main__':
    raise SystemExit(main())
