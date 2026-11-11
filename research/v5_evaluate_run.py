"""Run bounded v5 plan on a predeclared passage in a fresh destination."""
import argparse, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from _bootstrap import install as _install
_install()
from v5_allchannel import plan_tape
p=argparse.ArgumentParser(); p.add_argument('tape'); p.add_argument('start',type=float); p.add_argument('duration',type=float); p.add_argument('output',type=Path); p.add_argument('--denoised-root',type=Path)
a=p.parse_args(); begin=time.perf_counter()
plan=plan_tape(Path('D:/Apollo_15_30-Track/orig_concatenated')/a.tape,a.output,start=a.start,duration=a.duration,denoised_root=a.denoised_root)
print('elapsed_seconds',time.perf_counter()-begin, 'outcome',plan.get('optimization_outcome'))
