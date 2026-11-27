"""Stage 07c (stub): gently reduce historical ground hum on retimed channels.

Not implemented yet. The design will be completed after 07b retiming has run on real tapes.

Intended contract:
- Input: 07b ``retimed.wav`` per channel (default root D:/Apollo_15_30-Track/retimed).
- Output: a separate per-channel ``dehummed.wav`` with a provenance manifest under
  D:/Apollo_15_30-Track/historical_hum. The 07b output is kept unchanged as the
  archival master. Stage 08 will gain an explicit option to read 07c and will keep
  07b as its default input.
- Method: find narrow, persistent lines (about 40-1500 Hz) at any frequency and track
  their slow drift. Attenuate them by a capped amount (about 6-10 dB) over narrow
  bands. This is a pull-back, not removal. Stage 03 already removed the exact
  60*k Hz comb. What remains is off-grid: 60*k +/- 6.5 Hz sidebands and drifting
  remote-site families, for example about 58.7 to 58.4 Hz on A15_T510088 CH10.
  See research/ground_hum_20261127/README.md.
- Per-channel cleanup only. This is not a tape-motion correction, so the shared
  tape-map rules do not apply. Restart tracking at any 07b cut or join.
- Bypass is always allowed, so the delivery can stay unmodified historical audio.
"""

# Resolve shared modules from the stage folders.
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))
from _bootstrap import install as _install
_install()

import argparse
from pathlib import Path

from sg_inputs import TAPE_DATA

DEFAULT_RETIMED_ROOT = TAPE_DATA / 'retimed'
DEFAULT_HUM_ROOT = TAPE_DATA / 'historical_hum'


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--input-root', type=Path, default=DEFAULT_RETIMED_ROOT, help='07b retimed channels')
    p.add_argument('--output-root', type=Path, default=DEFAULT_HUM_ROOT)
    tapes = p.add_mutually_exclusive_group()
    tapes.add_argument('--tape')
    tapes.add_argument('--all-tapes', action='store_true')
    channels = p.add_mutually_exclusive_group()
    channels.add_argument('--channel', type=int)
    channels.add_argument('--all-channels', action='store_true')
    p.add_argument('--max-reduction-db', type=float, default=8.0, help='Cap on attenuation of any line (planned)')
    return p.parse_args(argv)


def main(argv=None):
    parse_args(argv)
    raise SystemExit('07c historical hum reduction is a stub: not implemented yet (run 07b first; '
                     'see research/ground_hum_20261127/README.md). Nothing was written.')


if __name__ == '__main__':
    main()
