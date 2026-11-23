"""Optional voice-band gain preprocessing for voice activity detection."""

import numpy as np
from scipy import ndimage, signal

from transcribe import ASR_RATE


VOICE_FILTER = signal.butter(4, (220, 3200), btype="bandpass", fs=ASR_RATE, output="sos")


def boost_voice_band(waveform):
    """Boost quiet, voice-band sections without changing their sample coordinates."""
    filtered = signal.sosfiltfilt(VOICE_FILTER, waveform).astype(np.float32)
    frame = ASR_RATE // 50
    count = (len(filtered) + frame - 1) // frame
    padded = np.pad(filtered, (0, count * frame - len(filtered)))
    frame_rms = np.sqrt(np.mean(padded.reshape(count, frame) ** 2, axis=1))
    mean_power = ndimage.uniform_filter1d(frame_rms ** 2, size=50, mode="nearest")
    smooth_rms = np.sqrt(np.maximum(mean_power, 0))
    gain = np.clip(0.045 / np.maximum(smooth_rms, 0.005), 1, 8)
    smoothed_gain = np.interp(np.arange(len(filtered)), np.arange(count) * frame + frame / 2, gain)
    return np.clip(filtered * smoothed_gain, -0.98, 0.98).astype(np.float32)
