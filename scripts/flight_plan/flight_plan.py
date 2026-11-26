"""Apollo 15 flight-plan ingestion (docs/17_2026-11-23_flight_plan_ingestion.md).

Example: uv run python scripts/run.py flight_plan.py pages

Run after sg_transcript.py: these steps read its snapshots and transcript tables.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# md_common and md_ai are in 00_common; md_snapshot and md_pao come from the SG transcript stage.
# The nearest _bootstrap.py puts every registered folder on sys.path.
sys.path.insert(0, str(next(p for p in Path(__file__).resolve().parents if (p / '_bootstrap.py').is_file())))
import _bootstrap  # noqa: E402,F401


def main(argv=None) -> int:
    import md_flightplan
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    md_flightplan.add_steps(parser.add_subparsers(dest='fp_command', required=True))
    return md_flightplan.main(parser.parse_args(argv))


if __name__ == '__main__':
    raise SystemExit(main())
