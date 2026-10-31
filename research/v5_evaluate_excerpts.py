"""Make equal-level raw and exact-map A/B excerpts from a bounded saved plan."""
import argparse, sys, json, hashlib
from pathlib import Path
import numpy as np, soundfile as sf
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from channel_review import prepare, continuous_map
from flutter import resample_at
p=argparse.ArgumentParser(); p.add_argument('plan',type=Path); p.add_argument('channel',type=int); p.add_argument('plan_start',type=float); p.add_argument('plan_duration',type=float); p.add_argument('excerpt_start',type=float); p.add_argument('duration',type=float); p.add_argument('output',type=Path); p.add_argument('--denoised',type=Path); a=p.parse_args()
if a.output.exists():
    raise ValueError('Preserve existing excerpts; choose a fresh output directory')
if a.duration <= 0 or a.excerpt_start < a.plan_start or a.excerpt_start+a.duration > a.plan_start+a.plan_duration:
    raise ValueError('Excerpt must be inside the saved map interval')
path,_,row,regions,start,offset,frames=prepare(a.plan,a.channel,a.plan_start,a.plan_duration)
t,nominal,*_=continuous_map(path.parent,regions,a.channel,start,frames,row['sample_rate'])
sr=row['sample_rate']; n=round(a.duration*sr); output_t=a.excerpt_start-start+np.arange(n)/sr
pos=np.interp(output_t,nominal,t)*sr+offset
low=max(0,int(np.floor(min(pos[0], offset+(a.excerpt_start-start)*sr)))-256); high=min(row['frames'],int(np.ceil(max(pos[-1],offset+(a.excerpt_start-start+a.duration)*sr)))+257)
source_path = row['path']
if a.denoised:
    meta = json.loads((a.denoised.parent/'denoised.json').read_text())
    sig = meta['signature']
    if meta['status'] != 'complete' or sig['source']['path'] != row['path'] or sig['offset_frames'] != 0 or sig['frames'] != row['frames'] or sig['source']['sample_rate'] != sr:
        raise ValueError('Denoised source provenance or alignment does not match plan')
    source_path = a.denoised
with sf.SoundFile(source_path) as source:
    source.seek(low); x=source.read(high-low,dtype='float64')
raw_index=round(offset+(a.excerpt_start-start)*sr)-low
original=x[raw_index:raw_index+n]; corrected=resample_at(x,pos-low)
a.output.mkdir(parents=True,exist_ok=False)
sf.write(a.output/'original.wav',original,sr,subtype='FLOAT'); sf.write(a.output/'corrected.wav',corrected,sr,subtype='FLOAT')
digest = lambda p: hashlib.sha256(Path(p).read_bytes()).hexdigest()
(a.output/'manifest.json').write_text(json.dumps(dict(plan=str(path.resolve()), plan_sha256=digest(path),
    channel=a.channel, plan_start=a.plan_start, plan_duration=a.plan_duration,
    excerpt_start=a.excerpt_start, duration=a.duration, source=str(Path(source_path).resolve()),
    source_kind='completed_denoised' if a.denoised else 'original', gain=1.,
    normalization_applied=False, listening_performed=False,
    files={name:digest(a.output/name) for name in ('original.wav','corrected.wav')},
    interpretation='original.wav is the unwarped input version; when source_kind is completed_denoised it is already denoised, not raw tape audio'),indent=2),encoding='utf-8')
print('Wrote paired unnormalized original and exact-map corrected excerpts; listening not performed.')
