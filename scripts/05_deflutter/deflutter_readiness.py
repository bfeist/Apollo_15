"""Read-only readiness for shared tape maps and completed v2 render inputs."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _bootstrap import install
install()

import argparse
import json
from denoise_archive import CANONICAL
from flutter_planning import inventory
from shared_render import prepared_source
from shared_tape import DEFAULT_PLANNING_ROOT, DEFAULT_EVIDENCE_ROOT, load_tape_plan, identity
from flutter_detection import load_census


def readiness(originals, plans=DEFAULT_PLANNING_ROOT, denoised=CANONICAL, evidence=DEFAULT_EVIDENCE_ROOT,
              detection=None, qualification=None):
    rows = []
    for folder in sorted(Path(originals).glob('A15_T*')):
        row = dict(tape=folder.name, cleanup_version='v2', shared_map_ready=False,
                   denoised_channels=0, expected_channels=0, errors=[])
        try:
            inv = inventory(folder)
            row['expected_channels'] = len(inv['channels'])
            row['completed_tone_evidence'] = all((Path(evidence)/folder.name/name).is_file()
                for name in ('plan/plan.json', 'joined_assessment.json', 'survey_manifest.json'))
            if detection is not None:
                try:
                    census = load_census(detection, folder.name)['evidence']
                    if identity(census['inventory']) != identity(inv):
                        raise ValueError('Detection census belongs to a different source inventory')
                    row.update(census_complete=True, census_coverage=census['coverage']['tape_time_fraction'])
                except (ValueError, OSError, KeyError) as error:
                    row.update(census_complete=False)
                    row['errors'].append(f'census: {error}')
            if qualification is not None:
                try:
                    from flutter_qualification import load_qualified
                    qualified = load_qualified(qualification, folder.name)
                    decisions = qualified['decisions']
                    if identity(decisions['inventory']) != identity(inv):
                        raise ValueError('Qualification belongs to a different source inventory')
                    row.update(qualification_complete=True, qualified_fraction=decisions['qualified_fraction'],
                               qualification_seconds=decisions['seconds'])
                except (ValueError, OSError, KeyError) as error:
                    row.update(qualification_complete=False)
                    row['errors'].append(f'qualification: {error}')
            for target in inv['channels']:
                try:
                    prepared_source(target, Path(denoised)/folder.name/f'CH{target["channel"]:02d}')
                    row['denoised_channels'] += 1
                except (ValueError, OSError, KeyError, TypeError, RuntimeError) as error:
                    row['errors'].append(f'CH{target["channel"]:02d}: {error}')
            path = Path(plans)/folder.name/'plan.json'
            if path.is_file():
                _, plan, data = load_tape_plan(path)
                if plan['inventory']['tape'] != inv['tape'] or identity(plan['inventory']) != identity(inv):
                    raise ValueError('Shared plan belongs to a different source inventory')
                row.update(shared_map_ready=plan['full_tape'], tape_time_coverage=plan['map']['coverage'],
                    map_sha256=plan['map']['sha256'], qualified_plan=plan.get('qualified', False))
                del data
                if qualification is not None:
                    provenance = plan.get('census') or {}
                    if not (row['qualification_complete'] and row['qualified_plan']
                            and provenance.get('analysis_id') == decisions['analysis_id']
                            and provenance.get('decisions_sha256') == qualified['decisions_sha256']):
                        row['shared_map_ready'] = False
                        row['errors'].append('Shared plan does not match the requested qualification')
            row['ready_for_render'] = (row['shared_map_ready'] and row['expected_channels'] > 0
                and row['denoised_channels'] == row['expected_channels'])
        except (ValueError, OSError, KeyError, TypeError, RuntimeError) as error:
            row['errors'].append(str(error))
            row['ready_for_render'] = False
        rows.append(row)
    if not rows:
        raise ValueError('No canonical tapes found')
    return dict(methodology='shared-tape-motion-v1', cleanup_version='v2', tapes=rows,
        ready_for_render=all(r['ready_for_render'] for r in rows),
        interpretation='Rendering readiness is not listening or production acceptance')


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--originals', type=Path, default=Path('D:/Apollo_15_30-Track/orig_concatenated'))
    p.add_argument('--planning-root', type=Path, default=DEFAULT_PLANNING_ROOT)
    p.add_argument('--denoised-root', type=Path, default=CANONICAL)
    p.add_argument('--evidence-root', type=Path, default=DEFAULT_EVIDENCE_ROOT)
    p.add_argument('--detection-root', type=Path, help='Detection census run folder to report')
    p.add_argument('--qualification-root', type=Path, help='Qualified analysis bundle to report')
    args = p.parse_args(argv)
    try:
        result = readiness(args.originals, args.planning_root, args.denoised_root, args.evidence_root,
                           args.detection_root, args.qualification_root)
        print(json.dumps(result, indent=2))
        return 0 if result['ready_for_render'] else 2
    except (OSError, ValueError, RuntimeError, KeyError, TypeError) as error:
        p.error(str(error))


if __name__ == '__main__':
    raise SystemExit(main())
