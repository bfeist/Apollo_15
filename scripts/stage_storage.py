"""Resolve retained Apollo evidence without modifying frozen worker sources or saved manifests."""
import json
import os
from pathlib import Path


def data_root():
    return Path('D:/Apollo_15_30-Track' if os.name == 'nt' else '/mnt/d/Apollo_15_30-Track')


def configure(module):
    root = data_root()
    production = root/'stage_evidence/denoise/production'
    tones = root/'stage_evidence/flutter/carrier_tones'
    previous_tones = root/'deflutter_planning_v5_allchannel'
    if module.__name__ == 'shared_tape' and tones.is_dir():
        module.DEFAULT_EVIDENCE_ROOT = tones
        original_candidates = module.continuous_candidates
        sources = json.loads((tones.parent/'carrier_tone_sources.json').read_bytes())

        def continuous_candidates(path, assessment):
            plan, candidates, rejected, bindings = original_candidates(path, assessment)
            # The old bundle supplied these exact bytes. Keep its recorded source
            # identity when comparing or extending previously committed maps.
            for binding in bindings:
                location = Path(binding['path'])
                if location.is_relative_to(tones):
                    relative = location.relative_to(tones)
                    if sources.get(relative.as_posix()) == binding['sha256']:
                        binding['path'] = str(previous_tones/relative)
            return plan, candidates, rejected, bindings

        module.continuous_candidates = continuous_candidates
    if module.__name__ == 'denoise_archive' and tones.is_dir():
        original_binding = module.bound_path

        def bound_path(item):
            path = original_binding(item)
            return tones/path.relative_to(previous_tones) if path.is_relative_to(previous_tones) else path

        module.bound_path = bound_path

    if not (production/'migration.json').is_file():
        return
    if module.__name__ == 'denoise_archive':
        module.CONTROL = production
        lock = module.archive_lock
        module.archive_lock = lambda control=production: lock(control)
        original_read = module.read_json
        old_control = root/'.denoise_v2_rollout'

        def read_json(path):
            path = Path(path)
            if path.is_relative_to(old_control):
                path = production/path.relative_to(old_control)
            return original_read(path)

        module.read_json = read_json
        original_historical = module.historical_path

        def historical_path(path, version='v1'):
            path = Path(path).absolute()
            if (version == 'v1' and module.CANONICAL == root/'denoised'
                    and module.ARCHIVE == root/'denoised_v1' and not module.ARCHIVE.exists()):
                if path.is_relative_to(module.CANONICAL):
                    relative = path.relative_to(module.CANONICAL)
                elif path.is_relative_to(module.ARCHIVE):
                    relative = path.relative_to(module.ARCHIVE)
                else:
                    return original_historical(path, version)
                if relative.name == 'cleanup.jsonl':
                    retained = module.exact_path(production.parent/'frequency_hints'/relative)
                    if retained.is_file():
                        return retained
                # Historical listening reviews can still resolve their original audio.
                # Production denoising reads its own retained frequency hints below.
                record = original_read(production/'migration.json')
                if (record.get('status') == 'complete' and record.get('version') == 'v1'
                        and record.get('from') == str(module.CANONICAL) and record.get('to') == str(module.ARCHIVE)):
                    archive = Path(original_read(production/'locations.json')['archived_denoise_v1'])
                    if not archive.is_relative_to(root/'_to_delete') or archive.name != 'denoised_v1':
                        raise ValueError(f'Unexpected historical denoise location: {archive}')
                    return module.exact_path(archive/relative)
            return original_historical(path, version)

        module.historical_path = historical_path
    elif module.__name__ == 'denoise_v2_production':
        original_hint = module.validate_hint

        def validate_hint(job, hint_root):
            if Path(hint_root) in (root/'denoised_v1', root/'denoised'):
                hint_root = production.parent/'frequency_hints'
            return original_hint(job, hint_root)

        module.validate_hint = validate_hint
    elif module.__name__ == 'denoise_v2_cluster':
        original_session = module.session_path

        def session_path(config):
            location = original_session(config)
            previous = root/'.denoise_v2_rollout'
            if location.is_relative_to(previous):
                return production/location.relative_to(previous)
            return location

        module.session_path = session_path
