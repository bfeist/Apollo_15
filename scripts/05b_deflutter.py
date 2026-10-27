"""Stage 05b: deflutter denoised audio, then apply bounded constant peak normalization."""
import argparse
import json
from pathlib import Path
from normalization import DEFAULT_TARGET_DBFS, DEFAULT_MAX_BOOST_DB
from flutter_plan_store import DEFAULT_PLANNING_ROOT, DEFAULT_DEFLUTTERED_ROOT, DEFAULT_DENOISED_ROOT, tape_directory


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path)
    parser.add_argument('--channel',type=int,required=True,help='Render a whole channel using its stage-03 intermediate')
    parser.add_argument('--denoised-input',type=Path,help='Prepared stage-03 folder containing denoised.wav and manifest')
    parser.add_argument('--experimental-transfer',action='store_true')
    parser.add_argument('--production-approval', type=Path, help='Accepted plan/channel and continuous listening evidence')
    parser.add_argument('--flutter-plan', type=Path, help='Saved continuous plan; defaults to the persistent tape folder')
    parser.add_argument('--tape', required=True, help='Tape name for the persistent saved plan')
    parser.add_argument('--planning-root', type=Path, default=DEFAULT_PLANNING_ROOT)
    parser.add_argument('--target-dbfs',type=float,default=DEFAULT_TARGET_DBFS,help='Final sample-peak target in dBFS (default: -3)')
    parser.add_argument('--max-boost-db',type=float,default=DEFAULT_MAX_BOOST_DB,help='Maximum constant gain boost in dB (default: 12)')
    args=parser.parse_args()
    try:
        from channel_review import render_channel_review, denoised_directory
        plan=args.flutter_plan or tape_directory(args.tape,args.planning_root)/'plan.json'
        index=plan/'plan.json' if plan.is_dir() else plan
        from flutter_policy import require_production
        require_production(args.production_approval, index, args.channel, args.experimental_transfer)
        inventory=json.loads(index.read_text(encoding='utf-8'))['inventory']
        if inventory['tape'] != args.tape:
            raise ValueError('Requested tape and saved plan disagree')
        row=next((r for r in inventory['channels'] if r['channel']==args.channel),None)
        if row is None:
            raise ValueError('Requested channel is unavailable in the saved plan')
        output=args.output or tape_directory(args.tape,DEFAULT_DEFLUTTERED_ROOT)/f'CH{args.channel:02d}'
        denoised=args.denoised_input or denoised_directory(DEFAULT_DENOISED_ROOT,args.tape,args.channel)
        render_channel_review(plan,args.channel,output,start=row['start_seconds'],experimental=args.experimental_transfer,
                              denoised_input=denoised,require_prepared=True,target_dbfs=args.target_dbfs,max_boost_db=args.max_boost_db,progress=lambda s:print(s,flush=True))
    except (ValueError,KeyError,TypeError,OSError,RuntimeError) as exc:parser.error(str(exc))

if __name__=='__main__':main()
