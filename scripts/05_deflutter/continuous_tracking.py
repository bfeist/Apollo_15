"""Chunk-invariant finite-latency carrier tracking on original samples.

All FIR, oscillator, decimation and phase state survives push() calls. Filter
delays are removed from sample coordinates, not fitted against target audio.
Only the stream's physical ends have missing context; I/O chunks have none.
"""
import numpy as np
from scipy import signal

RATE = 200
METHODS = {'stream_signed_2_15': (2., 15.), 'stream_signed_2_30': (2., 30.)}


class CarrierStream:
    def __init__(self, sample_rate, carrier_hz, capture_hz, amplitude_reference,
                 method='stream_signed_2_30'):
        if sample_rate % 1000 or sample_rate < 4000:
            raise ValueError('Carrier tracking requires a sample rate divisible by 1000')
        if method not in METHODS or not 0 < capture_hz < min(450, carrier_hz, sample_rate/2-carrier_hz):
            raise ValueError('Invalid streaming carrier method/capture')
        if not np.isfinite([carrier_hz, capture_hz, amplitude_reference]).all() or amplitude_reference <= 0:
            raise ValueError('A finite acquisition amplitude is required')
        self.sr, self.hz, self.capture = sample_rate, carrier_hz, capture_hz
        self.amplitude_reference, self.method = amplitude_reference, method
        self.decimation = sample_rate//1000
        self.anti_alias = signal.firwin(20*self.decimation+1, 400, fs=sample_rate)
        self.capture_filter = signal.firwin(121, capture_hz, fs=1000)
        self.motion_filter = signal.firwin(401, METHODS[method], pass_zero=False, fs=RATE)
        self.zi_alias = np.zeros(len(self.anti_alias)-1, complex)
        self.zi_capture = np.zeros(len(self.capture_filter)-1, complex)
        self.zi_motion = np.zeros(len(self.motion_filter)-1)
        self.zi_unsafe = np.zeros(400)
        self.raw_samples = self.base_samples = self.tracking_samples = 0
        self.last_phase = None
        self.phase_history = np.zeros(2)
        self.frequency_history = carrier_hz
        self.delay = 2+12+1+200  # 10 ms anti-alias, 60 ms capture, central diff, 1 s motion.
        self.finished = False
        self.pending_clips = []

    def push(self, samples):
        if self.finished:
            raise ValueError('Carrier stream is already finished')
        x = np.asarray(samples, dtype=float)
        if x.ndim != 1 or not np.isfinite(x).all():
            raise ValueError('Carrier stream needs finite mono samples')
        raw_indices = self.raw_samples+np.arange(len(x))
        clipped = np.unique(raw_indices[abs(x) >= .999]//(self.sr//RATE))
        if len(clipped):
            # Conservatively cover the demodulation/filter support around each
            # clipped tracking sample. These intervals survive push boundaries.
            breaks = np.flatnonzero(np.diff(clipped) > 1)
            starts = np.r_[0, breaks+1]
            stops = np.r_[breaks, len(clipped)-1]
            self.pending_clips.extend((int(clipped[a]), int(clipped[b])+31) for a,b in zip(starts,stops))
        mixed = x*np.exp(-2j*np.pi*self.hz*raw_indices/self.sr)
        z, self.zi_alias = signal.lfilter(self.anti_alias, [1.], mixed, zi=self.zi_alias)
        take = raw_indices % self.decimation == 0
        z = z[take]
        base_indices = self.base_samples+np.arange(len(z))
        self.raw_samples += len(x)
        self.base_samples += len(z)
        if not len(z):
            return self._empty()
        z, self.zi_capture = signal.lfilter(self.capture_filter, [1.], z, zi=self.zi_capture)
        z = z[base_indices % 5 == 0]
        if not len(z):
            return self._empty()
        phase = np.angle(z)
        if self.last_phase is None:
            theta = np.unwrap(phase)
        else:
            theta = np.unwrap(np.r_[self.last_phase, phase])[1:]
        self.last_phase = float(theta[-1])
        history = np.r_[self.phase_history, theta]
        frequency = self.hz+(history[2:]-history[:-2])*RATE/(4*np.pi)
        self.phase_history = history[-2:]
        # A slow local denominator follows mean-speed drift without using it as
        # a slow correction. The FIR removes DC/slow speed from the flutter map.
        centre, centre_state = signal.lfilter([1-np.exp(-1/(2*RATE))],
            [1., -np.exp(-1/(2*RATE))], frequency,
            zi=[self.frequency_history*np.exp(-1/(2*RATE))])
        self.frequency_history = float(centre[-1])
        relative = np.clip((frequency-centre)/np.maximum(centre, 1.), -.1, .1)
        motion, self.zi_motion = signal.lfilter(self.motion_filter, [1.], relative, zi=self.zi_motion)
        amplitude = abs(z)
        # The derivative is centred one sample before the latest phase. Use
        # both adjacent amplitudes conservatively at signal loss.
        unsafe = ((amplitude < .15*self.amplitude_reference)
                  | (abs(frequency-self.hz) >= .88*self.capture)
                  | (amplitude > 8*self.amplitude_reference))
        phase_indices = self.tracking_samples+np.arange(len(z))
        future_clips = []
        for low, high in self.pending_clips:
            unsafe |= (phase_indices >= low) & (phase_indices < high)
            if high > phase_indices[-1]+1:
                future_clips.append((low, high))
        self.pending_clips = future_clips
        unsafe_count, self.zi_unsafe = signal.lfilter(np.ones(401), [1.], unsafe.astype(float), zi=self.zi_unsafe)
        indices = self.tracking_samples+np.arange(len(z))-self.delay
        self.tracking_samples += len(z)
        support = (unsafe_count < .5).astype(float)
        support[indices < 2*RATE] = 0.
        motion = np.clip(motion, -.049, .049)*support
        return dict(indices=indices, speed=motion, support=support,
                    amplitude=amplitude, frequency=frequency)

    @staticmethod
    def _empty():
        return dict(indices=np.empty(0, dtype=np.int64), speed=np.empty(0),
                    support=np.empty(0), amplitude=np.empty(0), frequency=np.empty(0))

    def finish(self):
        end = self.raw_samples
        result = self.push(np.zeros((self.delay+402)*(self.sr//RATE)))
        self.finished = True
        keep = (result['indices'] >= 0) & (result['indices']/RATE < end/self.sr)
        return {key: value[keep] for key, value in result.items()}


def track_array(samples, sr, carrier_hz, capture_hz, amplitude_reference,
                method='stream_signed_2_30', chunk_frames=None):
    """Bounded acquisition helper, using exactly the production stream estimator."""
    tracker = CarrierStream(sr, carrier_hz, capture_hz, amplitude_reference, method)
    n = int(np.ceil(len(samples)*RATE/sr))
    speed, support = np.zeros(n), np.zeros(n)
    def accept(result):
        keep = (result['indices'] >= 0) & (result['indices'] < n)
        ids = result['indices'][keep]
        speed[ids], support[ids] = result['speed'][keep], result['support'][keep]
    chunk_frames = chunk_frames or sr*7
    for a in range(0, len(samples), chunk_frames):
        accept(tracker.push(samples[a:a+chunk_frames]))
    accept(tracker.finish())
    return speed, support
