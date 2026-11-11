"""Denoise the Apollo 15 originals or continue from saved channel checkpoints."""

# Resolve shared modules from the stage folders.
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))
from _bootstrap import install as _install
_install()

import argparse
import os
import re
import sys

if __name__ == '__main__':
    for name in ('OPENBLAS_NUM_THREADS', 'MKL_NUM_THREADS', 'OMP_NUM_THREADS',
                 'BLIS_NUM_THREADS', 'NUMEXPR_NUM_THREADS', 'VECLIB_MAXIMUM_THREADS'):
        os.environ[name] = '1'

from denoise_archive import (CANONICAL, CONTROL, MARKER, archive_lock,
                             atomic_json, exact_path, read_json, sha256)
from denoise_v2_background import lower_priority
from denoise_v2_production import job_key, publish
from denoise_v2_rollout import INVENTORY, REVIEW, canary_jobs, freeze_run, load_inventory, marker, run_scheduler


def prepare_output(root, run, freeze):
    """Use the existing output directory and leave its files in place."""
    root, run = map(exact_path, (root, run))
    wanted = marker(freeze)
    root_index = exact_path(root/MARKER)
    current = read_json(root_index) if root_index.is_file() else None
    transition = run/'supersede.json'
    prior = read_json(transition)['prior_run_id'] if transition.is_file() else None
    if prior is not None and not re.fullmatch(r'v2-[0-9a-f]{24}', prior):
        raise ValueError('Invalid prior run ID')
    if current == wanted:
        return prior
    if (not prior or not current or current.get('run_id') != prior
            or current.get('cleanup_version') != 'v2'):
        raise ValueError('The new output directory belongs to another run')
    atomic_json(root_index, wanted)
    return prior


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tapes', type=int, default=3)
    parser.add_argument('--workers', type=int, default=6)
    args = parser.parse_args(argv)
    if not 1 <= args.workers <= 61 or args.tapes < 1:
        parser.error('Use positive tape and worker limits, with at most 61 workers')
    if os.name == 'nt':
        lower_priority()
    args.mode, args.inventory = 'resume', INVENTORY
    print(f'Denoising {args.tapes} tapes at a time with {args.workers} background workers', flush=True)
    with archive_lock():
        inventory = load_inventory(INVENTORY)
        freeze = freeze_run(INVENTORY, REVIEW/'CH20_30min/manifest.json')
        run = exact_path(CONTROL/freeze['run_id'])
        run.mkdir(exist_ok=True)
        atomic_json(run/'frozen.json', freeze)
        args.previous_run_id = prepare_output(CANONICAL, run, freeze)
        thresholds = None
        approval_path = run/'approval.json'
        if approval_path.is_file():
            approval = read_json(approval_path)
            if approval.get('status') == 'accepted' and approval.get('run_id') == freeze['run_id']:
                thresholds = approval['checks']['thresholds']
                for job in canary_jobs(inventory['jobs']):
                    staged = exact_path(run/'canary'/job_key(job))
                    final = exact_path(CANONICAL/job_key(job))
                    if staged.exists():
                        if sha256(staged/'denoised.json') != approval['checks']['manifest_sha256']:
                            raise ValueError('The approved canary manifest changed')
                        publish(staged, final, args.previous_run_id)
                        print(f'Reused completed {job_key(job)}', flush=True)
        return run_scheduler(args, inventory, freeze, run, thresholds, inventory['jobs'])


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
    except (OSError, ValueError, KeyError, RuntimeError) as exc:
        print(f'Denoising stopped: {exc}', file=sys.stderr)
        raise SystemExit(2)
