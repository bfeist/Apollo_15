"""Read-only batch readiness and reviewable, unapproved acceptance templates."""
import argparse
import json
from pathlib import Path

from flutter_plan_store import DEFAULT_PLANNING_ROOT, DEFAULT_DENOISED_ROOT, load_saved_plan
from flutter_policy import (METHODOLOGY, implementation_digest,
                            policy_digest, require_rollout, require_production)
from flutter_planning import inventory
from channel_review import completed_denoised


def rollout_template():
    return dict(status='pending', methodology=METHODOLOGY, implementation_sha256=implementation_digest(),
                policy_sha256=policy_digest(), reviewer=None, reviewed_at=None,
                **{name: dict(path='', sha256='') for name in
                   ('carrier_qualification', 'fresh_holdouts', 'speech_listening')})


def readiness(originals, plans, denoised, approvals, rollout):
    result = dict(ready_for_batch_planning=False, ready_for_production=False, tapes=[], rollout_error=None)
    try:
        require_rollout(rollout)
        rollout_ok = True
    except (ValueError, OSError, KeyError, TypeError) as exc:
        rollout_ok = False
        result['rollout_error'] = str(exc)
    folders = sorted(Path(originals).glob('A15_T*'))
    if not folders:
        raise ValueError('No canonical tapes found')
    for folder in folders:
        row = dict(tape=folder.name, originals_valid=False, plan_complete=False, denoised_channels=0,
                   expected_channels=0, production_channels=0, errors=[])
        try:
            inv = inventory(folder)
            targets = [r for r in inv['channels'] if r['role'] != 'timing_only']
            row['originals_valid'] = bool(targets)
            row['expected_channels'] = len(targets)
            row['denoised_channels'] = sum(completed_denoised(r, Path(denoised)/folder.name/f'CH{r["channel"]:02d}', 0, r['frames']) for r in targets)
            plan_path = Path(plans)/folder.name/'plan.json'
            try:
                plan = load_saved_plan(plan_path, expand_regions=False)
                row['plan_complete'] = (plan.get('methodology') == METHODOLOGY
                    and plan.get('implementation_sha256') == implementation_digest()
                    and plan.get('policy_sha256') == policy_digest() and plan.get('full_tape', False))
                row['coverage'] = plan.get('summary', {}).get('duration_weighted_coverage', [])
                for target in targets:
                    try:
                        require_production(Path(approvals)/folder.name/'production_acceptance.json', plan_path, target['channel'])
                        row['production_channels'] += 1
                    except (ValueError, OSError, KeyError, TypeError) as exc:
                        row['errors'].append(f'CH{target["channel"]}: {exc}')
            except (ValueError, OSError, KeyError) as exc:
                row['errors'].append('plan: '+str(exc))
        except (ValueError, OSError) as exc:
            row['errors'].append(str(exc))
        result['tapes'].append(row)
    result['ready_for_batch_planning'] = all(t['originals_valid'] for t in result['tapes'])
    result['ready_for_production'] = rollout_ok and result['ready_for_batch_planning'] and all(t['plan_complete'] and
        t['production_channels'] == t['denoised_channels'] == t['expected_channels'] and t['expected_channels'] > 0 for t in result['tapes'])
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--originals', type=Path, default=Path('D:/Apollo_15_30-Track/orig_concatenated'))
    p.add_argument('--planning-root', type=Path, default=DEFAULT_PLANNING_ROOT)
    p.add_argument('--denoised-root', type=Path, default=DEFAULT_DENOISED_ROOT)
    p.add_argument('--approval-root', type=Path)
    p.add_argument('--rollout-approval', type=Path)
    p.add_argument('--write-rollout-template', type=Path)
    p.add_argument('--output', type=Path)
    args = p.parse_args()
    try:
        if args.write_rollout_template:
            args.write_rollout_template.parent.mkdir(parents=True, exist_ok=True)
            with args.write_rollout_template.open('x', encoding='utf-8') as stream:
                json.dump(rollout_template(), stream, indent=2)
            return 0
        result = readiness(args.originals, args.planning_root, args.denoised_root,
                           args.approval_root or args.planning_root, args.rollout_approval)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            with args.output.open('x', encoding='utf-8') as stream:
                json.dump(result, stream, indent=2)
        print(json.dumps(result, indent=2))
        return 0 if result['ready_for_production'] else 2
    except (ValueError, OSError, KeyError, TypeError) as exc:
        p.error(str(exc))


if __name__ == '__main__':
    raise SystemExit(main())
