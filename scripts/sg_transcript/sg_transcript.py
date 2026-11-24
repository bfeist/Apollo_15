"""Apollo 15 mission-data ingestion (docs/16_2026-11-23_mission_data_ingestion.md).

Example: uv run python scripts/run.py sg_transcript.py snapshot journals

Run this first. The flight plan, TOC and their checks follow in flight_plan.py
(scripts/run.py flight_plan.py --help). Packaging for the web is the separate,
not yet designed web_publish step.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='command', required=True)

    p = sub.add_parser('snapshot', help='step 1: fetch journals into .local and raw documents into the raw folder')
    p.add_argument('what', choices=('journals', 'raw', 'all'))
    p.add_argument('--refresh', action='store_true', help='refetch even when a snapshot exists')
    p.add_argument('--delay', type=float, default=1.5, help='seconds between requests')
    p.add_argument('--seed-tec', type=Path, help='local AS15_TEC.PDF to copy instead of downloading')

    p = sub.add_parser('transcripts', help='step 2: extract the 1971 transcripts from their text layers')
    p.add_argument('--edition', choices=('tec', 'pao', 'cm', 'lm', 'all'), default='all')
    p.add_argument('--check-layers', action='store_true', help='only report text-layer quality per edition')

    sub.add_parser('journals', help='step 3: extract AFJ and ALSJ blocks and spans')
    p = sub.add_parser('speakers', help='step 4: speaker census and map check')
    p.add_argument('--seed', action='store_true', help='add unmapped labels with proposed roles for review')

    p = sub.add_parser('spans', help='§4: rule, alignment and AI judgements; review queue')
    p.add_argument('--ai', action='store_true', help='also run the AI classifier')
    p.add_argument('--backend', choices=('hgx', 'strata'), default='hgx')
    p.add_argument('--budget', type=int, default=200, help='maximum AI requests this invocation')
    p.add_argument('--window', type=int, default=40, help='utterances per AI request')
    p.add_argument('--record', action='store_true', help='first save decided queue rows to MISSION_DATA/review')

    p = sub.add_parser('reconcile', help='step 5: utterance events and wording review')
    p.add_argument('--record', action='store_true', help='first save wording choices to MISSION_DATA/review')
    sub.add_parser('commentary', help='step 6: merged commentary')

    p = sub.add_parser('sg', help='SG reels: catalogue, PAO separation and timing anchors against the TEC and journals')
    p.add_argument('step', choices=('reels', 'pao', 'anchors', 'all'))
    p.add_argument('--reel', nargs='+', help='only these reels (writes *_partial.tsv)')

    p = sub.add_parser('photos', help='step 7: photo indexes')
    p.add_argument('what', choices=('hasselblad', 'imldi', 'sim', 'all'))
    p.add_argument('--delay', type=float, default=1.0)
    p.add_argument('--limit', type=int, help='stop after this many SIM view pages (testing)')

    p = sub.add_parser('media', help='step 8: download raw images and convert for hosting')
    p.add_argument('what', choices=('download-imldi', 'download-alsj', 'download-sim', 'convert'))
    p.add_argument('--collection', choices=('flight', 'mapping', 'panoramic', 'all'), default='all')
    p.add_argument('--limit', type=int)
    p.add_argument('--delay', type=float, default=1.0)
    p.add_argument('--dry-run', action='store_true')

    p = sub.add_parser('ocr', help='OXR page OCR for flagged transcript pages')
    p.add_argument('edition', choices=('tec', 'pao', 'cm', 'lm'))
    p.add_argument('--pages', help='comma list or ranges, e.g. 787,849,870-875; default: flagged pages')
    p.add_argument('--dpi', type=int, default=180)
    p.add_argument('--url', default='http://127.0.0.1:8010')

    sub.add_parser('all', help='run every offline text step in order (3–7)')
    args = parser.parse_args(argv)

    if args.command == 'snapshot':
        from md_common import Fetcher
        from md_snapshot import snapshot_journals, snapshot_raw_documents
        fetcher = Fetcher(delay=args.delay)
        if args.what in ('journals', 'all'):
            print(snapshot_journals(fetcher, refresh=args.refresh))
        if args.what in ('raw', 'all'):
            seed = {'original_docs/AS15_TEC.PDF': args.seed_tec} if args.seed_tec else None
            snapshot_raw_documents(fetcher, refresh=args.refresh, seed=seed)
        return 0
    if args.command == 'transcripts':
        import md_transcripts
        return md_transcripts.main(args)
    if args.command == 'journals':
        import md_journals
        return md_journals.main(args)
    if args.command == 'speakers':
        import md_speakers
        return md_speakers.main(args)
    if args.command == 'spans':
        import md_spans
        return md_spans.main(args)
    if args.command == 'reconcile':
        import md_reconcile
        return md_reconcile.main(args)
    if args.command == 'commentary':
        import md_commentary
        return md_commentary.main(args)
    if args.command == 'sg':
        import md_sg
        return md_sg.main(args)
    if args.command == 'photos':
        import md_photos
        return md_photos.main(args)
    if args.command == 'media':
        import md_media
        return md_media.main(args)
    if args.command == 'ocr':
        import md_ocr
        return md_ocr.main(args)
    if args.command == 'all':
        import md_commentary, md_journals, md_photos, md_reconcile, md_spans, md_speakers
        ns = argparse.Namespace(record=False, ai=False, backend='hgx', budget=0, window=40, seed=False, what='hasselblad',
                                delay=1.0, limit=None)
        for step in (md_journals, md_speakers, md_spans, md_reconcile, md_commentary, md_photos):
            print(f'== {step.__name__}')
            code = step.main(ns)
            if code:
                return code
        return 0
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
