import os; os.environ['OMP_NUM_THREADS']='1'
import time, numpy as np, numba
n=136*8000; x=np.random.default_rng(0).normal(0,.01,n); osc=np.exp(2j*np.pi*1234.5*np.arange(n)/8000)
starts=np.arange(0,n-4000+1,2000); w=np.hanning(4000); w/=w.sum()

def make(fm):
    @numba.njit(fastmath=fm)
    def obs(samples, oscillator, starts, window):
        result = np.empty(len(starts), dtype=np.complex128)
        for index in range(len(starts)):
            real, imag = 0., 0.
            start = starts[index]
            for offset in range(len(window)):
                sample = 2.*samples[start+offset]
                real += (sample*oscillator[start+offset].real)*window[offset]
                imag += (-sample*oscillator[start+offset].imag)*window[offset]
            result[index] = complex(real, imag)
        return result
    return obs

@numba.njit(fastmath={'reassoc','contract'})
def obs_split(samples, osc_re, osc_im, starts, window):
    result = np.empty(len(starts), dtype=np.complex128)
    for index in range(len(starts)):
        real, imag = 0., 0.
        start = starts[index]
        for offset in range(len(window)):
            s = samples[start+offset]*window[offset]
            real += s*osc_re[start+offset]
            imag -= s*osc_im[start+offset]
        result[index] = complex(2*real, 2*imag)
    return result

@numba.njit(fastmath={'reassoc','contract'})
def obs_blocked(samples, oscillator, starts, window):
    """Accumulate the windowed product per frame in 4 independent lanes."""
    result = np.empty(len(starts), dtype=np.complex128)
    for index in range(len(starts)):
        start = starts[index]
        r0=r1=r2=r3=0.; i0=i1=i2=i3=0.
        m = len(window)
        for offset in range(0, m-3, 4):
            k = start+offset
            s0=samples[k]*window[offset]; s1=samples[k+1]*window[offset+1]
            s2=samples[k+2]*window[offset+2]; s3=samples[k+3]*window[offset+3]
            r0+=s0*oscillator[k].real; i0-=s0*oscillator[k].imag
            r1+=s1*oscillator[k+1].real; i1-=s1*oscillator[k+1].imag
            r2+=s2*oscillator[k+2].real; i2-=s2*oscillator[k+2].imag
            r3+=s3*oscillator[k+3].real; i3-=s3*oscillator[k+3].imag
        result[index] = complex(2*(r0+r1+r2+r3), 2*(i0+i1+i2+i3))
    return result

def demod_einsum(samples, oscillator, starts, window):
    demod = 2*samples*oscillator.conj()
    frames = np.lib.stride_tricks.sliding_window_view(demod, len(window))
    return np.einsum('ij,j->i', frames[::starts[1]-starts[0]], window)

def dot_matrix(samples, oscillator, starts, window):
    """Frames as a (543,4000) view; BLAS-free matvec via @ on complex? use real/imag separately."""
    demod = 2*samples*oscillator.conj()
    frames = np.lib.stride_tricks.sliding_window_view(demod, len(window))[::2000]
    return frames @ window

def bench(f,*a,reps=20):
    f(*a); t=time.perf_counter()
    for _ in range(reps): r=f(*a)
    return (time.perf_counter()-t)/reps*1e3, r
ref = demod_einsum(x,osc,starts,w)
for name,f,args in [('einsum (reference)',demod_einsum,(x,osc,starts,w)),('frames @ window',dot_matrix,(x,osc,starts,w)),
                    ('numba strict',make(False),(x,osc,starts,w)),('numba reassoc',make({'reassoc','contract'}),(x,osc,starts,w)),
                    ('numba fastmath=True',make(True),(x,osc,starts,w)),
                    ('numba split re/im',obs_split,(x,np.ascontiguousarray(osc.real),np.ascontiguousarray(osc.imag),starts,w)),
                    ('numba 4-lane',obs_blocked,(x,osc,starts,w))]:
    ms,r=bench(f,*args); print(f'{name:22s} {ms:6.2f} ms  relerr {np.max(abs(r-ref))/np.max(abs(ref)):.1e}')
