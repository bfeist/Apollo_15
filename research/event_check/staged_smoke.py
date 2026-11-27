"""Smoke test of the staged 07b runner on two channels into a test root (not the archive)."""
import importlib.util
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / 'scripts'), str(ROOT / 'scripts/30-track')]


def main():
    spec = importlib.util.spec_from_file_location('r07b', ROOT / 'scripts/30-track/07_retime/07b_retime.py')
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    from quindar_tape import load_shared_plan
    import staged_io
    tape, chans = sys.argv[1], [int(c) for c in sys.argv[2].split(',')]
    out, scratch = Path(sys.argv[3]), Path(sys.argv[4])
    plan = m.irig_plan(tape) or m.sg_plan(tape)
    saved = load_shared_plan(plan)
    srcs = [Path(f'D:/Apollo_15_30-Track/defluttered_shared/{tape}/CH{c:02d}/deflutter.wav') for c in chans]
    rows = m.tape_row_plan(tape, saved, srcs, out)
    jobs = [(s, out / tape / s.parent.name, rows['channels'][str(int(s.parent.name[2:]))]['knots']) for s in srcs]
    t = time.time()
    print(staged_io.run_staged(jobs, plan, scratch / tape, len(chans), lock_root=out))
    print('elapsed', round(time.time() - t))


if __name__ == '__main__':
    main()
