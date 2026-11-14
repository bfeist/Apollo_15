"""Additional direct comparison of Genus fused output to saved Windows probes."""
import os
for name in ('OPENBLAS_NUM_THREADS','OMP_NUM_THREADS','MKL_NUM_THREADS'):
    os.environ[name]='1'
import sys,json
from pathlib import Path, PureWindowsPath
sys.path.insert(0,'scripts')
from _bootstrap import install
install()
import numpy as np
import soundfile as sf
import denoise_v2_numba as dsp
import denoise_v2_cluster as cluster
from render_denoise_v2 import gather_seeds,saved_seeds
base=Path('/mnt/d/Apollo_15_30-Track')
probe_path=next((base/'.denoise_v2_rollout').glob('*/cluster/probe.json'))
probe=cluster.read_json(probe_path)
source=base/'orig_concatenated'/Path(*PureWindowsPath(probe['job']['source']['path']).parts[-2:])
frames=sf.info(source).frames
hints=base/'denoised_v1'/probe['job']['tape']/f"CH{probe['job']['channel']:02d}"/'cleanup.jsonl'
seeds=saved_seeds(hints,0,frames)
dsp.warmup()
rows=[]
with sf.SoundFile(source) as stream:
    def read(a,n):
        stream.seek(a)
        return stream.read(n,dtype='float64')
    for index,(a,b) in enumerate(probe['intervals']):
        values=list(dsp.corrected_chunks(read,frames,a,b,lambda a,b:gather_seeds(seeds,a,b)))
        output=np.concatenate([v[2] for v in values]).astype('<f4')
        saved=probe_path.with_name(f'windows-{index}.npy')
        assert cluster.sha256(saved)==probe['files'][index]['sha256']
        metrics=cluster.compare_probe(np.load(saved,allow_pickle=False),output)
        rows.append(dict(index=index,interval=[a,b],windows_file_sha256=cluster.sha256(saved),comparison=metrics))
report=dict(host='genus',backend=dsp.backend_configuration(),passed=True,probes=rows)
output=Path(__file__).with_name('genus_windows_probe_comparison.json')
with output.open('x') as stream: json.dump(report,stream,indent=2);stream.write(chr(10))
print(json.dumps(report))
