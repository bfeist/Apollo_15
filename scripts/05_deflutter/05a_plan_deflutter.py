"""Stage 05a: compile one shared tape map from completed all-channel tone evidence."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _bootstrap import install
install()

import argparse
import json
from flutter_plan_store import planning_directory, planning_lock
from shared_tape import DEFAULT_EVIDENCE_ROOT, DEFAULT_PLANNING_ROOT, build_tape_plan, identity
from flutter_planning import inventory


def discover_regional_plans(data_root, tape):
    """Find completed whole-tape and bounded regional evidence in saved stores."""
    found = []
    for root in sorted(Path(data_root).glob('deflutter_planning*')):
        folder = root/tape
        for path in [folder/'plan.json', *sorted(folder.glob('audits/*/plan.json'))]:
            if path.is_file():
                meta = json.loads(path.read_text(encoding='utf-8'))
                if meta.get('complete') and meta.get('schema') == 1:
                    found.append(path.resolve())
    return found


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--input', type=Path, required=True, help='Canonical original tape folder')
    p.add_argument('--output', type=Path)
    p.add_argument('--planning-root', type=Path, default=DEFAULT_PLANNING_ROOT)
    p.add_argument('--evidence-root', type=Path, default=DEFAULT_EVIDENCE_ROOT,
                   help='Completed all-channel survey bundles; reused without new analysis')
    p.add_argument('--regional-plan', type=Path, action='append', default=[],
                   help='Additional completed regional evidence; may be repeated')
    p.add_argument('--start', type=float, default=0)
    p.add_argument('--duration', type=float)
    args = p.parse_args(argv)
    try:
        source = inventory(args.input)
        bundle = args.evidence_root/source['tape']
        output = args.output or planning_directory(source['tape'], args.planning_root, args.start, args.duration)
        evidence, assessment = bundle/'plan/plan.json', bundle/'joined_assessment.json'
        if not evidence.is_file() or not assessment.is_file():
            raise ValueError('Completed all-channel tone evidence is unavailable; no automatic resurvey is run')
        saved = json.loads(evidence.read_text(encoding='utf-8'))
        if source['tape'] != saved['inventory']['tape'] or identity(source) != identity(saved['inventory']):
            raise ValueError('Saved tone evidence does not match the canonical originals')
        regional = list(args.regional_plan)
        regional.extend(discover_regional_plans(args.input.resolve().parent.parent, source['tape']))
        regional = sorted({(path/'plan.json' if path.is_dir() else path).resolve() for path in regional})
        destination = output.resolve()
        protected = [args.input.resolve(), bundle.resolve(), *[path.parent for path in regional]]
        if any(destination == path or destination in path.parents or path in destination.parents
               for path in protected):
            raise ValueError('Shared output must not overlap originals or saved evidence')
        with planning_lock(output):
            plan = build_tape_plan(evidence, assessment, output, regional_paths=regional,
                start=args.start, end=None if args.duration is None else args.start+args.duration)
        print(json.dumps(dict(tape=source['tape'], output=str(output),
            eligible_seconds=plan['map']['eligible_seconds'], supported_seconds=plan['map']['supported_seconds'],
            bypass_seconds=plan['map']['bypass_seconds'], tape_time_coverage=plan['map']['coverage'],
            new_analysis=False, map_sha256=plan['map']['sha256']), indent=2))
        return 0
    except (OSError, ValueError, RuntimeError, KeyError, TypeError) as error:
        p.error(str(error))


if __name__ == '__main__':
    main()
