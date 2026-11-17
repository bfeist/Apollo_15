"""Stage 05 analysis: archive assessment, follow-up, qualification and the QA batch.

Reads an immutable detection census and writes a separate bundle
deflutter_analysis/<census-run-id>/<analysis-id>/. Use run after the census
to assess, follow up, qualify and validate in order. Individual commands:

  assess      pass 2: inventory every tape, collapse beep events, flag and queue
  followup    pass 3: one bounded follow-up pass over queued regions (resumable)
  qualify     frozen policy -> apply / bypass decisions and qualified streams
  validate    held-out evaluation of the frozen policy (written once)
  qa-batch    freeze one stratified random listening selection
  qa-render   matched A/B WAVs for that selection from qualified 05a plans

regression applies the contradiction rule to the saved regression corpus.
See docs/13_2026-11-17_deflutter_qualification.md.
"""
import os
import sys
from pathlib import Path

for _name in ('OPENBLAS_NUM_THREADS', 'MKL_NUM_THREADS', 'OMP_NUM_THREADS',
              'BLIS_NUM_THREADS', 'NUMEXPR_NUM_THREADS', 'VECLIB_MAXIMUM_THREADS'):
    os.environ[_name] = '1'

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _bootstrap import install
install()

import argparse
import json


def main(argv=None):
    import flutter_analysis as analysis
    import flutter_followup as followup
    import flutter_qualification as qualification
    import qualification_policy as rules
    from crosstalk_stage import DEFAULT_CROSSTALK_ROOT
    from denoise_archive import CANONICAL

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='command', required=True)

    def census_options(p):
        p.add_argument('--census-dir', type=Path, required=True, help='deflutter_detection_census/<run-id>/')
        p.add_argument('--root', type=Path, default=analysis.DEFAULT_ANALYSIS_ROOT)
        p.add_argument('--seed', type=int, default=analysis.DEFAULT_SEED, help='Sample, split and QA seed')
        p.add_argument('--exclude-tape', action='append', default=[])
        p.add_argument('--originals', type=Path, help='Canonical originals; default: the census input root')
        p.add_argument('--no-source-check', action='store_true', help='Skip rereading current original inventories')

    for name, text in (('analysis-id', 'Print the analysis ID for the current census state'),
                       ('assess', 'Pass 2: assess all saved census results')):
        census_options(sub.add_parser(name, help=text))
    run = sub.add_parser('run', help='Assess, follow up, qualify and validate; reuse completed stages')
    census_options(run)
    follow = sub.add_parser('followup', help='Pass 3: run or resume the follow-up pass')
    follow.add_argument('--analysis-dir', type=Path, required=True)
    for p in (run, follow):
        p.add_argument('--workers', type=int, choices=range(1, followup.MAX_WORKERS+1), default=4)
    qualify = sub.add_parser('qualify', help='Apply the frozen policy and write qualified streams')
    qualify.add_argument('--analysis-dir', type=Path, required=True)
    qualify.add_argument('--allow-incomplete', action='store_true',
                         help='Freeze windows without follow-up results as incomplete (05a refuses those tapes)')
    for name in ('validate', 'summary'):
        sub.add_parser(name).add_argument('--analysis-dir', type=Path, required=True)
    reg = sub.add_parser('regression', help='Contradiction rule on the saved regression corpus')
    reg.add_argument('--corpus', type=Path, default=qualification.DEFAULT_CORPUS)
    reg.add_argument('--output', type=Path, required=True, help='New JSON report file')
    reg.add_argument('--seed', type=int, default=analysis.DEFAULT_SEED)
    batch = sub.add_parser('qa-batch', help='Freeze the representative listening selection')
    batch.add_argument('--analysis-dir', type=Path, required=True)
    batch.add_argument('--audio-root', type=Path, default=DEFAULT_CROSSTALK_ROOT,
                       help='Stage-04 roots (default) or v2 denoised roots for speech screening')
    batch.add_argument('--per-stratum', type=int, default=2)
    batch.add_argument('--diagnostic', type=int, default=6)
    batch.add_argument('--excerpt-seconds', type=float, default=64.)
    render = sub.add_parser('qa-render', help='Render the frozen selection as matched A/B WAVs')
    render.add_argument('--analysis-dir', type=Path, required=True)
    render.add_argument('--planning-root', type=Path, required=True, help='Qualified 05a plans of this analysis')
    render.add_argument('--audio-root', type=Path, help='Override the stage-04 or explicit denoised-only root')
    render.add_argument('--skip-crosstalk', action='store_true', help=f'Explicit denoised-only: {CANONICAL}')
    render.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command in ('analysis-id', 'assess', 'run'):
            exclude = tuple(args.exclude_tape)
            if args.command == 'analysis-id':
                manifest = analysis.input_manifest(args.census_dir, args.originals, exclude, not args.no_source_check)
                analysis_id, _ = analysis.analysis_identity(manifest, rules.POLICY, args.seed)
                folder = args.root.resolve()/manifest['census_run_id']/analysis_id
                print(json.dumps(dict(analysis_id=analysis_id, analysis_dir=str(folder),
                                      exists=(folder/'assessment.json').is_file(),
                                      tapes={s: sum(t['status'] == s for t in manifest['tapes'])
                                             for s in sorted({t['status'] for t in manifest['tapes']})}), indent=2))
                return 0
            def progress(message):
                print(message, file=sys.stderr, flush=True)
            bundle = analysis.assess(args.census_dir, root=args.root, seed=args.seed, exclude=exclude,
                                     originals=args.originals, check_sources=not args.no_source_check, progress=progress)
            folder = bundle['dir']
            result = dict(analysis_dir=str(folder), analysis_id=bundle['config']['analysis_id'])
            if args.command == 'run':
                if not (folder/'qualification'/'summary.json').is_file():
                    status = followup.run_followup(folder, workers=args.workers, progress=progress)
                    if not status['complete']:
                        raise analysis.AnalysisError(f'Follow-up unfinished. See {folder/"followup"/"status.json"}')
                summary = qualification.qualify(folder, progress=progress)
                report = qualification.validate(folder)
                result.update(qualification_complete=summary['complete'], validation_passed=report['passed'],
                              validation_report=str(folder/'validation.json'))
            print(json.dumps(result, indent=2))
            if result.get('validation_passed') is False:
                return 2
            return 0
        if args.command == 'followup':
            status = followup.run_followup(args.analysis_dir, workers=args.workers)
            print(json.dumps({k: v for k, v in status.items() if k != 'statuses'}, indent=2))
            return 0 if status['complete'] else 2
        if args.command == 'qualify':
            summary = qualification.qualify(args.analysis_dir, allow_incomplete=args.allow_incomplete)
            print(json.dumps({k: v for k, v in summary.items() if k not in ('files', 'tapes')}, indent=2))
            return 0 if summary['complete'] else 2
        if args.command == 'validate':
            report = qualification.validate(args.analysis_dir)
            print(json.dumps(report, indent=2))
            return 0 if report['passed'] else 2
        if args.command == 'summary':
            bundle = analysis.open_analysis(args.analysis_dir)
            print((bundle['dir']/'archive_summary.json').read_text(encoding='utf-8'))
            return 0
        if args.command == 'regression':
            if args.output.exists():
                raise ValueError('Regression report must be a new file')
            report = qualification.regression(args.corpus, seed=args.seed)
            args.output.parent.mkdir(parents=True, exist_ok=True)
            analysis.atomic_json(args.output, report)
            print(json.dumps({k: dict(windows=v['windows'], eligible=v['eligible'], contradicted=len(v['contradicted']),
                                      held_out_kept=v['held_out_kept'], held_out_removed=v['held_out_removed'])
                              for k, v in report['sets'].items()}, indent=2))
            return 0
        if args.command == 'qa-batch':
            record = qualification.qa_batch(args.analysis_dir, args.audio_root, per_stratum=args.per_stratum,
                                            diagnostic=args.diagnostic, excerpt_seconds=args.excerpt_seconds)
            print(json.dumps({k: v for k, v in record.items() if k != 'selections'}, indent=2))
            return 0
        if args.command == 'qa-render':
            audio = args.audio_root or (CANONICAL if args.skip_crosstalk else DEFAULT_CROSSTALK_ROOT)
            renders = qualification.qa_render(args.analysis_dir, args.planning_root, audio, args.output,
                                              skip_crosstalk=args.skip_crosstalk)
            print(json.dumps(renders, indent=2))
            return 0
    except (OSError, ValueError, KeyError, RuntimeError) as error:
        print(f'Analysis stopped: {error}', file=sys.stderr, flush=True)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
