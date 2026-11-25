"""Independent clock-line, shifted-waveform and local delay checks for discovered repeats."""
from collections import defaultdict
import sys

import numpy as np
from scipy import signal

from _bootstrap import REPO
sys.path.insert(0, str(REPO/'research'))
from crosstalk_probe import analysis_filter
from repeat_scan import RATE, WINDOW
from repeat_validate import band_agreement, corroborated, matched_level_db, spectrum


# The research search filter suppresses exact digitization lines through
# 3 kHz. These samples are evidence only. Stage 04 still fits on v2 audio.
FIR = analysis_filter()
HALO = len(FIR)//2
SHIFTS = np.array([round(RATE/t) for t in (120, 100, 60, 50, 25, 10)])
SHIFTS = np.r_[-SHIFTS[::-1], SHIFTS]
PAD = int(max(abs(SHIFTS)))+12


def filtered(stream, start, end):
    left, right = start-PAD-HALO, end+PAD+HALO
    if left < 0 or right > len(stream):
        return None
    stream.seek(left)
    return signal.oaconvolve(stream.read(right-left, dtype='float64'), FIR, mode='valid')


def measure(ref, samples, center, radius=5):
    first = center-radius
    candidates = np.lib.stride_tricks.sliding_window_view(
        samples[first:center+radius+WINDOW], WINDOW)
    power = np.einsum('ij,ij->i', candidates, candidates)
    products = np.einsum('ij,j->i', candidates, ref)
    corr = products/np.sqrt(np.maximum(power*np.dot(ref,ref), 1e-30))
    k = int(np.argmax(abs(corr)))
    return float(corr[k]), samples[first+k:first+k+WINDOW]


def witness(hit, target_stream, donor_stream, target_offset, donor_offset):
    frame = round(hit['early']*RATE)-target_offset
    lag = round((hit['later']-hit['early'])*RATE)
    donor_offset -= hit.get('donor_center_shift_frames',0)
    donor_frame = round((hit['early']+hit['early_path'])*RATE)-donor_offset
    late_donor_frame = round((hit['later']+hit['late_path'])*RATE)-donor_offset
    delta = late_donor_frame-donor_frame
    if abs(delta) > 8:
        return None, 'donor occurrences disagree'
    target = filtered(target_stream, frame, frame+lag+WINDOW)
    donor = filtered(donor_stream, donor_frame-8, donor_frame+WINDOW+8)
    if target is None or donor is None:
        return None, 'insufficient filtered context'
    early = target[PAD:PAD+WINDOW]
    corr, late = measure(early, target, PAD+lag)
    if abs(corr) < .30 or not corroborated(band_agreement(spectrum(early),spectrum(late))):
        return None, 'repeat loses broadband support after clock-line rejection'
    # Compare to both sides of the proposed occurrence. Periodic carriers
    # and pulse trains may match in several bands at many unrelated lags.
    control = max(abs(measure(early, target, PAD+lag+int(s), 2)[0]) for s in SHIFTS)
    if abs(corr) < max(control+.045, 1.20*control):
        return None, 'periodic shifted control also predicts the occurrence'
    ec, de = measure(early, donor, PAD+8)
    lc, dl = measure(late, donor, PAD+8+delta)
    if min(abs(ec),abs(lc)) < .32 or not all(
            corroborated(band_agreement(spectrum(y),spectrum(x))) for y,x in [(early,de),(late,dl)]):
        return None, 'donor loses independent broadband support after clock-line rejection'
    levels = matched_level_db(early,late,de,dl)
    return dict(hit, broadband_later_db=hit['later_db'], later_db=levels,
                line_rejected_within=corr, shifted_control_max=control,
                line_rejected_donor_correlations=[ec,lc]), None


def in_domain(e):
    separation = e['early_path']-e['late_path']
    return (np.isfinite(separation) and .8+1/RATE < separation < 1.9-1/RATE and
            max(abs(e['early_path']),abs(e['late_path'])) < 1.88)


class DelayWitnesses:
    """Observed magnitude support from separate speech bursts on this tape."""
    def __init__(self, events, rows):
        self.shifts = {ch:r.get('clock_shift_frames',0)/RATE for ch,r in rows.items()}
        buckets = defaultdict(list)
        for ch, hits in events.items():
            if rows[ch]['row']['role'] == 'timing_only':
                continue
            for e in hits:
                if in_domain(e) and rows[e['donor']]['row']['role'] != 'timing_only':
                    lag = e['later']-e['early']
                    time = e['early'] + self.shifts[ch]
                    buckets[int(time//60), round(lag/.005)].append((lag,ch,time))
        self.buckets = buckets

    def lookup(self, event, timing, channel=None):
        minute = int((event['early']+self.shifts.get(channel,0.))//60)
        lag = event['later']-event['early']
        bucket = round(lag/.005)
        selected = []
        for distance in range(2,31):
            for other in (minute-distance,minute+distance):
                if any(abs(other-int(w['time']//60)) < 2 for w in selected):
                    continue
                for index in (bucket,bucket-1,bucket+1,bucket-2,bucket+2,bucket-3,bucket+3):
                    examples = self.buckets.get((other,index), ())
                    found = next(((s,ch,t) for s,ch,t in examples if abs(s-lag) <= .015 and
                                  (not timing or not selected or ch != selected[0]['channel'])), None)
                    if found is not None:
                        s,ch,t = found
                        selected.append(dict(channel=ch,time=t,separation=s))
                        break
                if len(selected) >= 2:
                    return selected
        return []
