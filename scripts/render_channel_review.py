"""Denoise then deflutter one complete channel, with matched A/B review audio."""
import os
for name in ('OPENBLAS_NUM_THREADS','OMP_NUM_THREADS','MKL_NUM_THREADS'):
    os.environ.setdefault(name,'1')
import argparse
from pathlib import Path
from normalization import DEFAULT_TARGET_DBFS, DEFAULT_MAX_BOOST_DB
from channel_review import render_channel_review, denoised_directory
from flutter_plan_store import DEFAULT_PLANNING_ROOT, DEFAULT_DEFLUTTERED_ROOT, DEFAULT_DENOISED_ROOT, tape_directory


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tape',required=True)
    parser.add_argument('--channel',type=int,required=True)
    parser.add_argument('--output',type=Path,help='Defaults to defluttered/<tape>/CHnn')
    parser.add_argument('--denoised-root',type=Path,default=DEFAULT_DENOISED_ROOT,help='Persistent reusable denoised channels')
    parser.add_argument('--planning-root',type=Path,default=DEFAULT_PLANNING_ROOT)
    parser.add_argument('--flutter-plan',type=Path,help='Override the persistent tape plan')
    parser.add_argument('--experimental-transfer',action='store_true',help='Apply unvalidated proposals for listening; failed decisions remain bypassed')
    parser.add_argument('--start',type=float,default=0,help='Canonical source seconds; default full channel from zero')
    parser.add_argument('--duration',type=float,help='Optional bounded review, with its own endpoint anchoring')
    parser.add_argument('--cleanup-block-seconds',type=float,default=60)
    parser.add_argument('--render-block-seconds',type=float,default=5)
    parser.add_argument('--dry-run',action='store_true',help='Verify saved artifacts and show the job without writing audio')
    parser.add_argument('--target-dbfs',type=float,default=DEFAULT_TARGET_DBFS,help='Final sample-peak target in dBFS (default: -3)')
    parser.add_argument('--max-boost-db',type=float,default=DEFAULT_MAX_BOOST_DB,help='Maximum constant gain boost in dB (default: 12)')
    args = parser.parse_args()
    try:
        path = args.flutter_plan or tape_directory(args.tape,args.planning_root)/'plan.json'
        # Do not allow an explicit plan to silently select a different tape.
        import json
        index = path/'plan.json' if path.is_dir() else path
        if json.loads(index.read_text(encoding='utf-8'))['inventory']['tape'] != args.tape:
            raise ValueError('Requested tape and saved plan disagree')
        output=args.output or tape_directory(args.tape,DEFAULT_DEFLUTTERED_ROOT)/f'CH{args.channel:02d}'
        denoised=denoised_directory(args.denoised_root,args.tape,args.channel,args.start,args.duration)
        render_channel_review(path,args.channel,output,start=args.start,duration=args.duration,
                              experimental=args.experimental_transfer,cleanup_block_seconds=args.cleanup_block_seconds,
                              render_block_seconds=args.render_block_seconds,dry_run=args.dry_run,
                              denoised_input=denoised,target_dbfs=args.target_dbfs,max_boost_db=args.max_boost_db,progress=lambda s:print(s,flush=True))
    except (ValueError,KeyError,TypeError,OSError,RuntimeError) as exc:
        parser.error(str(exc))


if __name__ == '__main__':
    main()
