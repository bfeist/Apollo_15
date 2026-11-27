"""Which plan 07b will apply to each tape, before rendering (docs/23, archive run). Read-only.

  uv run python research/event_check/plan_inventory.py
"""
import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / 'scripts'), str(ROOT / 'scripts/30-track')]


def main():
    spec = importlib.util.spec_from_file_location('r07b', ROOT / 'scripts/30-track/07_retime/07b_retime.py')
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    from sg_inputs import tape_names
    import head_rows
    import sg_stage
    for tape in tape_names():
        irig, sg = m.irig_plan(tape), m.sg_plan(tape)
        chosen = irig or sg
        version = json.loads(Path(chosen).read_text())['version'] if chosen else None
        anchors = head_rows.current_anchors(tape)
        stale = version not in (None, 'irig-timing-v2', sg_stage.VERSION)
        print(f"{tape}: {'07i' if irig else '07a' if sg else 'NONE (blocked)'} {version or ''}"
              f"{'  STALE: refit' if stale else ''}; head rows from {anchors.name if anchors else 'NO v3 anchors (no shift)'}")


if __name__ == '__main__':
    main()
