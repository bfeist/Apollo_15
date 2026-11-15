"""Synthetic simultaneous tape channels for detection-census tests."""
import numpy as np
import soundfile as sf

SR = 8000


def flutter(t):
    """About 1.6 % RMS speed error: 6.6 Hz with a 13.2 Hz harmonic."""
    return (.02*np.sin(2*np.pi*6.6*t)+.007*np.sin(2*np.pi*13.2*t+.7)
            + .004*np.sin(2*np.pi*9.1*t+1.1))


def fm_tone(t, hz, e, amplitude, phase=0.):
    """A tone recorded on tape and replayed with fractional speed error e."""
    return amplitude*np.sin(2*np.pi*hz*np.cumsum(1+e)/SR+phase)


def channels(duration, count=5, seed=1, motion=flutter, carriers=None, comb=None):
    """Hum recorded on tape (fluttered) beside 40 dB stronger exact digitization lines."""
    rng = np.random.default_rng(seed)
    t = np.arange(int(duration*SR))/SR
    e = motion(t)
    out = {}
    extras = carriers or {1: 431., 2: 987., 3: 431., 4: 640., 5: 987.}
    for ch in range(1, count+1):
        x = .004*rng.standard_normal(len(t))
        x += fm_tone(t, 59.1, e, .003, ch) + fm_tone(t, 118.2, e, .002, 2*ch)
        x += .3*np.sin(2*np.pi*60*t+ch) + .1*np.sin(2*np.pi*120*t+.3*ch)
        if ch in extras:
            x += fm_tone(t, extras[ch], e, .01, .5*ch)
        if comb:
            x += .01*np.sin(2*np.pi*comb*t+.15*np.sin(2*np.pi*6.6*t)*0+ch)
        out[ch] = x.astype(np.float32)
    return out, e


def write_tape(folder, tape, audio, offsets=None):
    folder.mkdir(parents=True, exist_ok=True)
    offsets = offsets or {}
    for ch, x in audio.items():
        start = offsets.get(ch, 0)
        end = start+len(x)//SR
        stamp = lambda s: f'207-00-{s//60:02d}-{s % 60:02d}'
        sf.write(folder/f'{tape}_HR1U_CH{ch}_VOICE_{stamp(start)}_{stamp(end)}.wav', x, SR, subtype='PCM_16')
    return folder
