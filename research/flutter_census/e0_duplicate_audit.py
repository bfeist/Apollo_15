"""E0: cheap duplicate-channel audit (fingerprints of 3 x 2 s at 10/50/90 %)."""
import hashlib, json, re, sys
from collections import defaultdict
from pathlib import Path
import soundfile as sf
ORIG = Path('D:/Apollo_15_30-Track/orig_concatenated')
out = {}
for tape in sorted(p.name for p in ORIG.glob('A15_T*')):
    groups = defaultdict(list)
    for p in sorted((ORIG / tape).glob('*.wav')):
        m = re.search(r'_CH(\d+)_', p.name)
        if not m: continue
        with sf.SoundFile(p) as f:
            h = hashlib.sha1()
            for q in (.1, .5, .9):
                f.seek(int(f.frames * q)); h.update(f.read(16000, dtype='int16').tobytes())
        groups[h.hexdigest()].append(int(m.group(1)))
    dup = [sorted(v) for v in groups.values() if len(v) > 1]
    out[tape] = dict(channels=sum(len(v) for v in groups.values()), distinct=len(groups), duplicate_groups=dup)
    print(tape, out[tape], flush=True)
Path(sys.argv[1]).write_text(json.dumps(out, indent=1))
