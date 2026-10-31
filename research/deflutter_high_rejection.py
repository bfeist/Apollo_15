"""Isolated research carrier stream with stronger finite-filter rejection.

All numerical-quality gates stay unchanged. This experiment rejects clipped
input outright; it does not claim general clipping recovery. Changed FIR delay
is removed analytically from coordinates, never fitted to target responses.
"""
from pathlib import Path
import sys
import numpy as np
from scipy import signal
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from continuous_tracking import CarrierStream,RATE

VERSION='high-rejection-carrier-research-v1'


class HighRejectionStream(CarrierStream):
    def __init__(self,sample_rate,carrier_hz,capture_hz,amplitude_reference,method='stream_signed_2_30'):
        super().__init__(sample_rate,carrier_hz,capture_hz,amplitude_reference,method)
        self.anti_alias=signal.firwin(40*self.decimation+1,400,fs=sample_rate,window=('kaiser',10.))
        self.capture_filter=signal.firwin(401,capture_hz,fs=1000,window=('kaiser',10.))
        self.zi_alias=np.zeros(len(self.anti_alias)-1,complex)
        self.zi_capture=np.zeros(len(self.capture_filter)-1,complex)
        # 20ms anti-alias + 200ms capture + 5ms centered derivative + 1s motion.
        self.delay=4+40+1+200

    def push(self,samples):
        if np.any(abs(np.asarray(samples))>=.999):
            raise ValueError('High-rejection research stream rejects clipped input; propagate original clipping flags after any transform')
        return super().push(samples)


def track_array_high_rejection(samples,sr,carrier_hz,capture_hz,amplitude_reference,chunk_frames=None):
    tracker=HighRejectionStream(sr,carrier_hz,capture_hz,amplitude_reference)
    n=int(np.ceil(len(samples)*RATE/sr));speed=np.zeros(n);support=np.zeros(n)
    def accept(result):
        keep=(result['indices']>=0)&(result['indices']<n);ids=result['indices'][keep]
        speed[ids]=result['speed'][keep];support[ids]=result['support'][keep]
    size=chunk_frames or 7*sr
    for start in range(0,len(samples),size):accept(tracker.push(samples[start:start+size]))
    accept(tracker.finish())
    return speed,support
