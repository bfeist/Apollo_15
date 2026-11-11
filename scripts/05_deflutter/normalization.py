"""Constant sample-peak gain applied after deflutter, including streamed channels."""
import math

import numpy as np
import soundfile as sf

DEFAULT_TARGET_DBFS = -3.
DEFAULT_MAX_BOOST_DB = 12.


def validate_settings(target_dbfs, max_boost_db):
    if not math.isfinite(target_dbfs) or target_dbfs >= 0:
        raise ValueError('Choose a finite --target-dbfs below 0')
    if not math.isfinite(max_boost_db) or not 0 <= max_boost_db <= 60:
        raise ValueError('Choose --max-boost-db between 0 and 60')


def gain_for_peak(peak, target_dbfs=DEFAULT_TARGET_DBFS, max_boost_db=DEFAULT_MAX_BOOST_DB):
    validate_settings(target_dbfs, max_boost_db)
    if not math.isfinite(peak) or peak < 0:
        raise ValueError('Invalid normalization input peak')
    gain = min(10**(max_boost_db/20), 10**(target_dbfs/20)/peak) if peak else 1.
    return dict(method='constant_sample_peak', input_peak=peak, gain=gain,
                gain_db=20*math.log10(gain), target_dbfs=target_dbfs, max_boost_db=max_boost_db)


def normalize_array(audio, target_dbfs=DEFAULT_TARGET_DBFS, max_boost_db=DEFAULT_MAX_BOOST_DB):
    if not np.isfinite(audio).all():
        raise ValueError('Non-finite normalization input')
    record = gain_for_peak(float(np.max(np.abs(audio))), target_dbfs, max_boost_db)
    return audio*record['gain'], record


def verify_peak(peak, record):
    if not math.isfinite(peak) or peak > 10**(record['target_dbfs']/20)+1e-7:
        raise ValueError('Written output exceeds the chosen peak target')
    record['output_peak'] = peak


def normalize_pending_file(path, target_dbfs=DEFAULT_TARGET_DBFS, max_boost_db=DEFAULT_MAX_BOOST_DB,
                           progress=print):
    """Scan then scale an unpublished render in bounded blocks; never a source/cache."""
    validate_settings(target_dbfs, max_boost_db)
    peak = 0.
    with sf.SoundFile(path, 'r+') as audio:
        block = audio.samplerate*10
        for samples in audio.blocks(blocksize=block, dtype='float64'):
            if not np.isfinite(samples).all():
                raise ValueError('Non-finite normalization input')
            peak = max(peak, float(np.max(np.abs(samples))))
        record = gain_for_peak(peak, target_dbfs, max_boost_db)
        progress(f"Normalize: {record['gain_db']:+.2f} dB constant gain; target {target_dbfs:g} dBFS")
        if record['gain'] != 1.:
            for start in range(0, audio.frames, block):
                audio.seek(start)
                samples = audio.read(min(block, audio.frames-start), dtype='float64')
                audio.seek(start)
                audio.write(samples*record['gain'])
    return record
