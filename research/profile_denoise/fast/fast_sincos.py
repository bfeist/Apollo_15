"""Vectorizable double-precision sincos (Cody-Waite reduction, Cephes polynomials)."""
import numba
import numpy as np

# pi/2 split so that k*PIO2_1 is exact for |k| < 2**28 (Cephes DP1..DP3 doubled).
PIO2_1 = 2*7.85398125648498535156E-1
PIO2_2 = 2*3.77489470793079817668E-8
PIO2_3 = 2*2.69515142907905952645E-15
TWO_OVER_PI = 2/np.pi
S0, S1, S2, S3, S4, S5 = (1.58962301576546568060E-10, -2.50507477628578072866E-8,
                          2.75573136213857245213E-6, -1.98412698295895385996E-4,
                          8.33333333332211858878E-3, -1.66666666666666307295E-1)
C0, C1, C2, C3, C4, C5 = (-1.13585365213876817300E-11, 2.08757008419747316778E-9,
                          -2.75573141792967388112E-7, 2.48015872888517045348E-5,
                          -1.38888888888730564116E-3, 4.16666666666665929218E-2)


@numba.njit(cache=True, fastmath=False, inline='always')
def _sincos(x):
    k = np.rint(x*TWO_OVER_PI)
    r = ((x-k*PIO2_1)-k*PIO2_2)-k*PIO2_3
    q = np.int64(k) & 3
    z = r*r
    s = r+r*z*(S5+z*(S4+z*(S3+z*(S2+z*(S1+z*S0)))))
    c = 1.-.5*z+z*z*(C5+z*(C4+z*(C3+z*(C2+z*(C1+z*C0)))))
    swap = (q & 1) == 1
    sin_v = c if swap else s
    cos_v = s if swap else c
    sin_v = -sin_v if q >= 2 else sin_v
    cos_v = -cos_v if (q == 1 or q == 2) else cos_v
    return sin_v, cos_v


@numba.njit(cache=True, fastmath=False)
def exp_1j_affine(scale, phase, offset):
    """exp(1j*(scale*phase+offset)) with vectorizable sincos."""
    n = len(phase)
    re = np.empty(n)
    im = np.empty(n)
    for i in range(n):
        s, c = _sincos(scale*phase[i]+offset)
        re[i] = c
        im[i] = s
    out = np.empty(n, dtype=np.complex128)
    for i in range(n):
        out[i] = complex(re[i], im[i])
    return out


@numba.njit(cache=True, fastmath=False)
def exp_1j_affine_direct(scale, phase, offset):
    out = np.empty(len(phase), dtype=np.complex128)
    for i in range(len(phase)):
        s, c = _sincos(scale*phase[i]+offset)
        out[i] = complex(c, s)
    return out
