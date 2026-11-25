"""Defer final gain to stage 07 without changing frozen DSP or checkpoints.

The historical run identity describes the reference writer. This separately
hash-bound output policy records an explicit gain bypass on newly finished
channels. Previously normalized channels retain their original verifier.
"""
from contextlib import contextmanager
from pathlib import Path
import sys

import denoise_archive as archive
import denoise_v2_cluster as cluster
import denoise_v2_production as production

METHOD = 'unity_gain_deferred_to_stage07'


def descriptor(config):
    return dict(schema=1, method=METHOD, config_sha256=cluster.digest_json(config),
                implementation_sha256=archive.sha256(Path(__file__)))


def load_policy(config, *, prepare=False):
    path = cluster.session_path(config)/'output_policy.json'
    expected = descriptor(config)
    if prepare:
        with cluster.planning_lock(path.with_suffix('')):
            if not path.exists():
                archive.atomic_json(path, expected)
    if archive.read_json(path) != expected:
        raise production.IdentityError('Denoise output policy changed; preserve its recorded implementation')
    return expected


def bypass_record(settings, audio, policy):
    return dict(method=METHOD, gain=1., gain_db=0., settings=settings,
                policy=policy, unity_audio_sha256=audio['sha256'],
                output_peak=audio['peak'], samples_above_full_scale=audio['samples_above_full_scale'],
                ignored_endpoint_impulses=[])


def publish_unity(unity_path, output_path, audio, settings, policy, stop=None):
    """Name the already verified float WAV using a hard link, without rewriting it."""
    if stop is not None and stop.exists():
        raise InterruptedError('Stopped before publishing normalized audio')
    unity_path, output_path = map(archive.exact_path, (unity_path, output_path))
    before = unity_path.stat()
    if (before.st_size, before.st_mtime_ns) != (audio['bytes'], audio['mtime_ns']):
        raise production.IdentityError('Verified unity audio changed before publication')
    if output_path.exists():
        if not output_path.samefile(unity_path):
            raise production.IdentityError(f'Private output already contains different audio: {output_path}')
    else:
        output_path.hardlink_to(unity_path)
    after = output_path.stat()
    if (after.st_size, after.st_mtime_ns) != (before.st_size, before.st_mtime_ns):
        raise production.IdentityError('Unity audio changed during publication')
    return dict(audio), bypass_record(settings, audio, policy)


def verify_unity(folder, job, freeze, policy, *, full=False):
    """Apply the original binding/hash checks with an explicit unity-gain policy."""
    record = archive.read_json(folder/'denoised.json')
    expected = production.new_record(job, freeze, True)
    if (production.action(job) != production.DENOISE or 'normalization' not in freeze
            or any(record.get(k) != value for k, value in expected.items())
            or record.get('normalization') != bypass_record(freeze['normalization'], record['audio'], policy)):
        raise production.IdentityError(f'Incompatible unity-gain v2 channel: {folder}')
    path = archive.validate_binding(folder, record, job['source'], 0, job['source']['frames'], 'v2')
    if archive.sha256(folder/'cleanup.jsonl') != record['cleanup_log_sha256']:
        raise production.IdentityError(f'Cleanup evidence changed: {folder}')
    if full and archive.sha256(path) != record['audio']['sha256']:
        raise production.IdentityError(f'Completed audio bytes changed: {folder}')
    return record


@contextmanager
def unity_outputs(config, *, prepare=False):
    """Install the same policy for coordinator, reference and private Numba writers."""
    import denoise_v2_accelerated as accelerated

    # Keep the complete frozen source and input checks authoritative.
    original_check = cluster.check_code
    original_check(config)
    policy = load_policy(config, prepare=prepare)
    original_verify = production.verify_completed
    originals = []

    def replace(module, name, value):
        originals.append((module, name, getattr(module, name)))
        setattr(module, name, value)

    def verify(folder, job, freeze, *, full=False):
        record = archive.read_json(folder/'denoised.json')
        if record.get('normalization', {}).get('method') == METHOD:
            return verify_unity(folder, job, freeze, policy, full=full)
        return original_verify(folder, job, freeze, full=full)

    def normalize(unity_path, output_path, audio, settings, stop=None):
        load_policy(config)
        return publish_unity(unity_path, output_path, audio, settings, policy, stop)

    def progress_writer(original_write):
        def write(*args, **kwargs):
            progress = kwargs.get('progress', print)
            kwargs['progress'] = lambda message: progress(
                'Unity gain; final speech level deferred to stage 07'
                if message.startswith('Normalized by ') else message)
            return original_write(*args, **kwargs)
        return write

    def install_writer(writer):
        replace(writer, 'normalize_audio', normalize)
        replace(writer, 'verify_completed', verify)
        replace(writer, 'write_channel', progress_writer(writer.write_channel))

    original_factory = accelerated.private_writer

    def private_writer(*args, **kwargs):
        callback = original_factory(*args, **kwargs)
        # The accelerator deliberately loads a private copy of the reference
        # writer. Patch that copy as well, leaving its source bytes untouched.
        writer = sys.modules['_apollo15_accelerated_writer']
        install_writer(writer)
        # The accelerator's callback closes over the original write function;
        # its normalization and verification globals now use this policy.
        return progress_writer(callback)

    def check(current):
        original_check(current)
        load_policy(current)

    try:
        install_writer(production)
        replace(cluster, 'write_channel', production.write_channel)
        replace(cluster, 'verify_completed', verify)
        replace(cluster, 'check_code', check)
        replace(accelerated, 'private_writer', private_writer)
        yield policy
    finally:
        for module, name, value in reversed(originals):
            setattr(module, name, value)
