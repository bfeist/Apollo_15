"""Qualified accelerated execution of the unchanged reviewed denoise contract.

The canonical run remains the reviewed output contract. Acceleration has its own
hashed execution identity and frame-span provenance, including retained reference
checkpoint prefixes. No completed reference channel is rewritten.
"""
import argparse
from copy import deepcopy
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import subprocess
import sys

import denoise_v2_cluster as cluster

# Set before importing/constructing any Numba dispatcher. Keep compiled caches
# on each machine, rather than mixing Windows/Linux cache writes on shared F:.
os.environ.setdefault('NUMBA_CACHE_DIR', str(
    Path(os.environ.get('LOCALAPPDATA', Path.home()/'.cache'))/'apollo15-numba'))

CODE_FILES = ('scripts/denoise_v2_numba.py', 'scripts/denoise_v2_accelerated.py')
PACKAGES = ('numpy', 'scipy', 'soundfile', 'numba', 'llvmlite')
CHECKPOINT_EXIT = 75


def environment():
    return dict(python=sys.version, packages={p:importlib.metadata.version(p) for p in PACKAGES})


def execution_folder(config, execution_id):
    if not re.fullmatch(r'numba-[0-9a-f]{24}', execution_id):
        raise ValueError('Invalid accelerated execution ID')
    return cluster.session_path(config)/'accelerators'/execution_id


def load_execution(config, execution_id, host=None):
    folder = execution_folder(config, execution_id)
    record = cluster.read_json(folder/'execution.json')
    identity = record['identity']
    if 'numba-'+cluster.digest_json(identity)[:24] != execution_id:
        raise ValueError('Accelerator identity changed')
    if identity['base_config_sha256'] != cluster.digest_json(config):
        raise ValueError('Accelerator belongs to another base run')
    if set(identity['implementation_sha256']) != set(CODE_FILES):
        raise ValueError('Incomplete accelerator code identity')
    for name, digest in identity['implementation_sha256'].items():
        if cluster.sha256(cluster.REPO/name) != digest:
            raise ValueError('Accelerated implementation changed: '+name)
    for machine in ('windows', 'genus'):
        evidence = folder/f'{machine}.json'
        if cluster.sha256(evidence) != identity['qualification_sha256'][machine]:
            raise ValueError('Accelerator qualification changed: '+machine)
        report = cluster.read_json(evidence)
        validate_report(report, config)
    if host is not None and environment() != identity['environments'][host]:
        raise ValueError('Accelerator numerical/compiler environment changed')
    return record


def validate_report(report, config):
    import denoise_v2_numba as backend
    expected_keys = {'scripts/denoise_v2.py', 'scripts/denoise_v2_refinement.py',
                     'scripts/denoise_v2_numba.py', 'research/qualify_denoise_numba.py'}
    if (not report.get('passed') or len(report.get('cases', [])) < 13
            or not all(row.get('passed') for row in report['cases'])
            or sum(row.get('type') == 'real_corrected_chunks' for row in report['cases']) < 6):
        raise ValueError('Complete model and boundary qualification is required')
    if (set(report.get('implementation_sha256', {})) != expected_keys
            or report.get('backend', {}).get('backend_id') != backend.BACKEND_ID):
        raise ValueError('Incomplete or wrong-backend qualification identity')
    expected = backend.backend_configuration()
    actual = report['backend']
    if {k:v for k,v in expected.items() if k != 'active_features'} != {
            k:v for k,v in actual.items() if k != 'active_features'}:
        raise ValueError('Qualified backend settings differ')
    if 'feature_policy' in expected:
        host = 'windows' if report['platform'].startswith('Windows') else 'genus'
        if actual.get('active_features') != expected['feature_policy'][host]:
            raise ValueError('Qualified platform features differ')
    if report['probe_sha256'] != cluster.sha256(cluster.session_path(config)/'probe.json'):
        raise ValueError('Qualification used different saved probes')
    for name, digest in report['implementation_sha256'].items():
        if cluster.sha256(cluster.REPO/name) != digest:
            raise ValueError('Qualification implementation changed: '+name)


def active_execution(config):
    active = cluster.read_json(cluster.session_path(config)/'active_accelerator.json')
    record = load_execution(config, active['execution_id'])
    if cluster.digest_json(record) != active['execution_sha256']:
        raise ValueError('Active accelerator descriptor changed')
    return active['execution_id'], record


def extend_segments(previous, cursor, execution_id):
    """Validate a committed prefix and record only newly committed frames."""
    old_cursor = previous.get('cursor', 0)
    if cursor < old_cursor:
        raise ValueError('Acceleration provenance cannot move backwards')
    metadata = previous.get('acceleration')
    segments = deepcopy(metadata['segments']) if metadata else (
        [dict(execution_id='reference', start_frame=0, end_frame=old_cursor)] if old_cursor else [])
    end = 0
    for row in segments:
        if row['start_frame'] != end or row['end_frame'] <= end:
            raise ValueError('Invalid acceleration provenance spans')
        if row['execution_id'] != 'reference' and not re.fullmatch(r'numba-[0-9a-f]{24}', row['execution_id']):
            raise ValueError('Invalid prior execution identity')
        end = row['end_frame']
    if end != old_cursor:
        raise ValueError('Acceleration provenance does not cover checkpoint prefix')
    if cursor > end:
        if segments and segments[-1]['execution_id'] == execution_id:
            segments[-1]['end_frame'] = cursor
        else:
            segments.append(dict(execution_id=execution_id, start_frame=end, end_frame=cursor))
    return dict(schema=1, executor=execution_id, segments=segments)


def private_writer(execution_id, execution, implementation='numba'):
    """Attach accelerated chunks and provenance to an isolated production writer."""
    import denoise_v2_numba as backend
    name = '_apollo15_accelerated_writer'
    spec = importlib.util.spec_from_file_location(name, cluster.REPO/'scripts/denoise_v2_production.py')
    writer = importlib.util.module_from_spec(spec)
    sys.modules[name] = writer
    spec.loader.exec_module(writer)
    if implementation == 'numba':
        writer.corrected_chunks = backend.corrected_chunks
    executor = execution_id if implementation == 'numba' else 'reference'
    original_atomic = writer.atomic_json

    def write_record(path, value):
        path = Path(path)
        if path.name not in ('checkpoint.json', 'denoised.json'):
            return original_atomic(path, value)
        checkpoint = path.with_name('checkpoint.json')
        previous = cluster.read_json(checkpoint) if checkpoint.exists() else {}
        cursor = value['cursor'] if path.name == 'checkpoint.json' else previous.get('cursor', 0)
        metadata = extend_segments(previous, cursor, executor)
        metadata['execution_sha256'] = cluster.digest_json(execution)
        metadata['execution_id'] = execution_id
        metadata['backend'] = implementation
        # Preserve the precise pre-transition records while holding the writer's
        # channel lock. Old checkpoint/audio identity checks remain authoritative.
        if previous and previous.get('acceleration', {}).get('executor') != executor:
            for current in (checkpoint, path.with_name('denoised.json')):
                if current.exists():
                    tag = execution_id if implementation == 'numba' else 'reference-'+execution_id
                    backup = current.with_name(current.stem+'.before-'+tag+'.json')
                    if not backup.exists():
                        with backup.open('xb') as stream:
                            stream.write(current.read_bytes())
                            writer.sync(stream)
        return original_atomic(path, dict(value, acceleration=metadata))

    writer.atomic_json = write_record
    original_write = writer.write_channel

    def write_channel(*args, **kwargs):
        result = original_write(*args, **kwargs)
        # Return the published record including persisted provenance. Remote
        # upload otherwise uses the writer's original in-memory base record.
        job, _, root = args[:3]
        return cluster.read_json(Path(root)/cluster.job_key(job)/'denoised.json')

    if implementation == 'numba':
        backend.warmup()
    return write_channel


def prepare(args):
    """Activate only complete, source-bound qualification; preserve old evidence."""
    if os.name != 'nt':
        raise ValueError('Prepare accelerated execution on the Windows coordinator')
    config = cluster.read_json(args.config)
    cluster.check_code(config)
    cluster.verify_qualification(config)
    reports = {host:cluster.read_json(path) for host, path in
               (('windows', args.windows_report), ('genus', args.genus_report))}
    for report in reports.values():
        validate_report(report, config)
    if {k:v for k,v in reports['windows']['backend'].items() if k != 'active_features'} != {
            k:v for k,v in reports['genus']['backend'].items() if k != 'active_features'}:
        raise ValueError('Backend settings differ across machines')
    command = cluster.remote_command(config, 'environment')
    command[-1] = 'exec '+shlex.join([config['remote_uv'], 'run', '--no-project',
        '--python', args.remote_python,
        str(PurePosixPath(config['remote_repo'])/'scripts'/Path(__file__).name), 'environment'])
    remote = subprocess.run(command, capture_output=True, text=True, check=True, timeout=30)
    environments = dict(windows=environment(), genus=json.loads(remote.stdout))
    for host, env in environments.items():
        report = reports[host]
        if (env['python'] != report['versions']['python']
                or any(env['packages'][p] != report['versions'][p] for p in ('numpy', 'scipy', 'soundfile'))
                or env['packages']['numba'] != report['backend']['numba']):
            raise ValueError('Qualification environment differs: '+host)
    identity = dict(schema=1, base_config_sha256=cluster.digest_json(config),
                    backend_by_host={h:r['backend'] for h,r in reports.items()}, remote_python=args.remote_python,
                    implementation_sha256={p:cluster.sha256(cluster.REPO/p) for p in CODE_FILES},
                    qualification_sha256={host:cluster.sha256(path) for host,path in
                        (('windows', args.windows_report), ('genus', args.genus_report))},
                    environments=environments)
    execution_id = 'numba-'+cluster.digest_json(identity)[:24]
    record = dict(execution_id=execution_id, identity=identity)
    folder = execution_folder(config, execution_id)
    with cluster.archive_lock():
        folder.mkdir(parents=True, exist_ok=True)
        files = [(args.windows_report, folder/'windows.json'), (args.genus_report, folder/'genus.json')]
        files += [(cluster.REPO/name, folder/Path(name).name) for name in CODE_FILES]
        for source, target in files:
            if target.exists():
                if cluster.sha256(source) != cluster.sha256(target):
                    raise ValueError('Existing execution evidence differs')
            else:
                shutil.copyfile(source, target)
        if (folder/'execution.json').exists():
            if cluster.read_json(folder/'execution.json') != record:
                raise ValueError('Existing execution identity differs')
        else:
            cluster.atomic_json(folder/'execution.json', record)
        load_execution(config, execution_id, 'windows')
        # Read the same shared descriptor and check the actual remote environment
        # before enabling it for future jobs.
        remote_config = str(PurePosixPath(config['remote_data'])/'.denoise_v2_rollout'/
                            config['frozen']['run_id']/'cluster/config.json')
        command[-1] = 'exec '+shlex.join([config['remote_uv'], 'run', '--no-project',
            '--python', args.remote_python,
            str(PurePosixPath(config['remote_repo'])/'scripts'/Path(__file__).name),
            'check', '--config', remote_config, '--execution-id', execution_id, '--host', 'genus'])
        subprocess.run(command, check=True, timeout=30)
        cluster.atomic_json(cluster.session_path(config)/'active_accelerator.json',
                            dict(execution_id=execution_id, execution_sha256=cluster.digest_json(record)))
    print('Activated qualified execution '+execution_id, flush=True)
    return 0


def worker(args):
    config = cluster.read_json(args.config)
    original_check = cluster.check_code
    original_check(config)
    execution = load_execution(config, args.execution_id, args.host)
    original_write = cluster.write_channel
    cluster.write_channel = private_writer(args.execution_id, execution, args.backend)

    def check(config):
        original_check(config)
        load_execution(config, args.execution_id, args.host)

    cluster.check_code = check
    try:
        return cluster.worker(config, args.key, args.host)
    finally:
        cluster.write_channel = original_write
        cluster.check_code = original_check


def remote_worker_command(command, config, execution_id, execution, implementation='numba'):
    """Rewrite only the qualified worker entry point and Python environment."""
    argv = shlex.split(command[-1])
    old_script = str(PurePosixPath(config['remote_repo'])/'scripts'/cluster.SCRIPT.name)
    if old_script not in argv or 'worker' not in argv:
        raise ValueError('Expected a remote cluster worker command')
    argv[argv.index(old_script)] = str(PurePosixPath(config['remote_repo'])/'scripts'/Path(__file__).name)
    python_index = argv.index('--python')+1
    if argv[python_index] != config['remote_python']:
        raise ValueError('Unexpected remote interpreter')
    argv[python_index] = execution['identity']['remote_python']
    return [*command[:-1], shlex.join([*argv, '--backend', implementation, '--execution-id', execution_id])]


class WorkerProcesses:
    """Coordinator-local subprocess proxy; never replaces global subprocess.Popen."""
    def __init__(self, original, execution_id, implementation='numba'):
        self.original, self.execution_id = original, execution_id
        self.implementation = implementation

    def __getattr__(self, name):
        return getattr(self.original, name)

    def Popen(self, command, *args, **kwargs):
        if (len(command) >= 4 and command[0] == sys.executable and command[1] == '-u'
                and command[2] == str(cluster.SCRIPT) and command[3] == 'worker'):
            command = [*command[:2], str(Path(__file__).resolve()), *command[3:],
                       '--backend', self.implementation, '--execution-id', self.execution_id]
        return self.original.Popen(command, *args, **kwargs)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['worker', 'environment', 'prepare', 'check'])
    parser.add_argument('--config', type=Path)
    parser.add_argument('--key')
    parser.add_argument('--host', choices=['windows', 'genus'])
    parser.add_argument('--execution-id')
    parser.add_argument('--backend', choices=['reference', 'numba'], default='numba')
    parser.add_argument('--windows-report', type=Path)
    parser.add_argument('--genus-report', type=Path)
    parser.add_argument('--remote-python', default='/home/bfeist/.local/share/apollo15-numba-venv/bin/python')
    args = parser.parse_args()
    if args.command == 'environment':
        print(json.dumps(environment()))
        return 0
    if args.command == 'prepare':
        if not all((args.config, args.windows_report, args.genus_report)):
            parser.error('prepare requires config, windows-report and genus-report')
        return prepare(args)
    if args.command == 'check':
        if not all((args.config, args.execution_id, args.host)):
            parser.error('check requires config, execution-id and host')
        config = cluster.read_json(args.config)
        cluster.check_code(config)
        load_execution(config, args.execution_id, args.host)
        print('Verified '+args.execution_id+' on '+args.host, flush=True)
        return 0
    if not all((args.config, args.key, args.host, args.execution_id)):
        parser.error('worker requires config, key, host and execution-id')
    return worker(args)


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except InterruptedError as exc:
        clean_stop = str(exc) in {'Stopped after the last committed checkpoint',
                                 'Stopped before publishing normalized audio'}
        print(f'Worker {"checkpointed" if clean_stop else "interrupted unexpectedly"}: {exc}',
              file=sys.stderr, flush=True)
        raise SystemExit(CHECKPOINT_EXIT if clean_stop else 2)
    except (ValueError, OSError, KeyError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f'Accelerated worker stopped: {exc}', file=sys.stderr, flush=True)
        raise SystemExit(2)
