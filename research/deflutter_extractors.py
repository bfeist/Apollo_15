"""Research extraction alternatives; source files and cleanup caches stay intact."""
import numpy as np

VERSION='stationary-mains-projection-research-v1'


def project_stationary_mains(x,sr,carrier_hz,capture_hz):
    """Fit constant-amplitude/phase exact mains components near capture.

    This is an in-memory linear projection, not a broad frequency notch. A
    moving historical neighbor keeps its sidebands except any exactly
    coincident coherent component, which cannot be identified separately from
    digitization hum. Independent response validation remains mandatory.
    """
    if x.ndim!=1 or not np.isfinite(x).all() or sr<=0:
        raise ValueError('Expected finite mono samples and positive sample rate')
    y=x.copy();t=np.arange(len(x))/sr;measurements=[]
    low=max(60.,carrier_hz-max(60.,2*capture_hz))
    high=min(sr/2-1,carrier_hz+max(60.,2*capture_hz))
    for harmonic in range(int(np.ceil(low/60)),int(np.floor(high/60))+1):
        hz=60.*harmonic;c=np.cos(2*np.pi*hz*t);s=np.sin(2*np.pi*hz*t)
        gram=np.array([[np.dot(c,c),np.dot(c,s)],[np.dot(c,s),np.dot(s,s)]])
        coefficient=np.linalg.solve(gram,np.array([np.dot(y,c),np.dot(y,s)]))
        y-=coefficient[0]*c+coefficient[1]*s
        measurements.append(dict(hz=hz,cosine=float(coefficient[0]),sine=float(coefficient[1]),
            amplitude=float(np.hypot(*coefficient))))
    return y,dict(version=VERSION,components=measurements,
        frequencies='Exact 60Hz harmonic contamination; no historical-carrier whitelist',
        limitation='Only stationary coherent component; drifting hum and coincident historical sidebands unresolved')
