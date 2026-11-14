from pathlib import Path
import json,hashlib,sys
sys.path.insert(0,'scripts')
from _bootstrap import install
install()
import denoise_v2_cluster as cluster
import denoise_v2_production as production
host=sys.argv[1]
base=Path('D:/Apollo_15_30-Track' if host=='windows' else '/mnt/d/Apollo_15_30-Track')
config_path=next((base/'.denoise_v2_rollout').glob('*/cluster/config.json'))
config=cluster.read_json(config_path); cluster.check_code(config)
freeze=config['frozen'] if host=='windows' else cluster.remote_freeze(config)
work=config_path.parent.parent/'work' if host=='windows' else cluster.scratch_paths(config,freeze)[2]
rows=[]
for p in sorted(work.glob('*/CH*/checkpoint.json')):
 saved=cluster.read_json(p); stage=p.parent
 record=dict(saved['identity']); record.pop('quality_thresholds')
 job=next(j for j in cluster.read_json(config_path.parent/'jobs.json') if cluster.job_key(j).as_posix()==stage.relative_to(work).as_posix())
 if host=='genus': job=cluster.mapped_job(job,config['remote_data'])
 source,info=production.validate_source(job,base/'orig_concatenated')
 hint=production.validate_hint(job,base/'denoised_v1')
 header=production.wav_header(info.frames,info.samplerate)
 audio=stage/('unity.wav' if 'normalization' in freeze else 'denoised.wav')
 with audio.open('rb') as stream:
  header_equal=stream.read(len(header))==header
  audio_equal=production.prefix(stream,4*saved['cursor'],hashlib.sha256(header)).hexdigest()==saved['audio_sha256']
 with (stage/'cleanup.jsonl').open('rb') as stream:
  log_equal=production.prefix(stream,saved['log_bytes']).hexdigest()==saved['log_sha256']
 identity_equal=record==production.new_record(job,freeze,hint)
 row=dict(key=stage.relative_to(work).as_posix(),cursor=saved['cursor'],checkpoint_sha256=cluster.sha256(p),header_equal=header_equal,audio_prefix_equal=audio_equal,log_prefix_equal=log_equal,identity_equal=identity_equal)
 row['passed']=all([header_equal,audio_equal,log_equal,identity_equal]); rows.append(row)
report=dict(host=host,checkpoints=rows,passed=bool(rows) and all(r['passed'] for r in rows))
output=Path('research/benchmarks/denoise_fused_20261114')/(host+'_checkpoint_audit.json')
with output.open('x') as stream: json.dump(report,stream,indent=2);stream.write(chr(10))
print(json.dumps(report))
assert report['passed']
