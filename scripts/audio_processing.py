"""Shared DSP used by the accepted review tools and numbered pipeline stages."""
import numpy as np
from scipy import signal, ndimage

DEFAULT_BROADBAND_CAP_DB = 3
TONAL_CLEANUP_METHOD = 'iterative_tones_plus_residual_hum_v1'


def frequency_median(db, width):
    """The same reflected frequency median, using SciPy's fast 1D path.

    A (width, 1) 2D filter never mixes time columns, but uses the much slower
    multidimensional rank-filter implementation. Contiguous 1D columns retain
    the exact window, boundary handling and float64 values.
    """
    columns = np.ascontiguousarray(db.T)
    background = np.empty_like(columns)
    for index, column in enumerate(columns):
        ndimage.median_filter(column, size=width, mode='reflect',
                              output=background[index])
    return background.T


def detect(x, sr):
    f, t, p = signal.spectrogram(x, sr, nperseg=2*sr, noverlap=sr,
                               window='hann', scaling='density', mode='psd')
    db = 10*np.log10(p+1e-30)
    baseline = frequency_median(db, 81)
    excess = db-baseline
    # Stable frequency evidence across time, rather than loudness alone.
    med = np.median(excess, axis=1)
    occupancy = np.mean(excess > 6, axis=1)
    peaks, _ = signal.find_peaks(med, height=7, distance=5)
    lines=[]
    for i in peaks:
        if not (25 < f[i] < sr/2-5 and occupancy[i]>=0.70):
            continue
        lo=hi=i
        while lo>1 and med[lo-1]>3 and i-lo<20: lo-=1
        while hi<len(f)-2 and med[hi+1]>3 and hi-i<20: hi+=1
        width=float(np.clip((f[hi]-f[lo])*1.5,2,12))
        lines.append(dict(hz=float(f[i]),width_hz=width,prominence_db=float(med[i]),occupancy=float(occupancy[i])))
    return lines


def remove(x,sr,lines):
    y=x.copy()
    for line in lines:
        b,a=signal.iirnotch(line['hz'],line['hz']/line['width_hz'],fs=sr)
        y=signal.filtfilt(b,a,y)
    return y


def detect_residual_hum(x, sr):
    """Find persistent hum-like harmonics missed by the general tone detector.

    This pass belongs on the speech-cleanup path, after general tonal removal.
    Original timing/reference audio must remain separate.
    """
    f, _, p = signal.spectrogram(x, sr, nperseg=4*sr, noverlap=3*sr)
    db = 10*np.log10(p+1e-30)
    background = frequency_median(db, 161)
    excess = np.median(db-background, axis=1)
    peaks, _ = signal.find_peaks(excess, distance=6)
    lines = []
    for k in peaks:
        hz = float(f[k])
        occupancy = float(np.mean(db[k]-background[k] > 4))
        if not (80 < hz < min(1600, sr/2-5)
                and abs(hz/60-round(hz/60))*60 < 3
                and excess[k] > 6 and occupancy > .6):
            continue
        lo = hi = k
        while lo > 1 and excess[lo-1] > 3 and k-lo < 16:
            lo -= 1
        while hi < len(f)-2 and excess[hi+1] > 3 and hi-k < 16:
            hi += 1
        width = float(np.clip((f[hi]-f[lo])*1.5, 2, 6))
        if hz < 130:
            width = max(width, 4.)
        lines.append(dict(hz=hz, width_hz=width,
                          prominence_db=float(excess[k]), occupancy=occupancy))
    return lines


def clean_tones(x, sr):
    """Shared stage-03 cleanup: up to six general and three residual-hum passes."""
    y = x.copy()
    passes = []
    for iteration in range(1, 7):
        lines = detect(y, sr)
        if not lines:
            break
        y = remove(y, sr, lines)
        passes.append(dict(iteration=iteration, lines=lines))
    hum_passes = []
    for iteration in range(1, 4):
        lines = detect_residual_hum(y, sr)
        if not lines:
            break
        y = remove(y, sr, lines)
        hum_passes.append(dict(iteration=iteration, lines=lines))
    return y, dict(tonal_cleanup_method=TONAL_CLEANUP_METHOD,
                   passes=passes, hum_passes=hum_passes,
                   residual_lines=detect(y, sr),
                   residual_hum_lines=detect_residual_hum(y, sr))


def denoise(x, sr, reduction_cap_db=DEFAULT_BROADBAND_CAP_DB):
    """Quiet-window spectral reduction with the listening-selected 3 dB default."""
    if not np.isfinite(reduction_cap_db) or not 0 <= reduction_cap_db <= 6:
        raise ValueError('Broadband reduction cap must be between 0 and 6 dB')
    n = 512
    _, times, z = signal.stft(x, sr, nperseg=n, noverlap=384)
    power = abs(z)**2
    # Find the quietest four-second candidate in the speech band, excluding edges.
    band = power[20:193].sum(axis=0)
    width = round(4*sr/128)
    smooth = np.convolve(band, np.ones(width)/width, mode='valid')
    k = int(np.argmin(smooth[width:-width])+width)
    noise = np.mean(power[:, k:k+width], axis=1, keepdims=True)
    gain = np.sqrt(np.maximum(1-noise/(power+1e-20), 0))
    gain = np.maximum(gain, 10**(-reduction_cap_db/20))
    gain = ndimage.gaussian_filter(gain, sigma=(1, 2))
    _, y = signal.istft(z*gain, sr, nperseg=n, noverlap=384)
    return y[:len(x)], [float(times[k]), float(times[min(k+width,len(times)-1)])]

