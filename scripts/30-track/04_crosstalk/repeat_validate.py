"""Verify repeat candidates against independent frequency bands and completed donor recordings."""
import argparse
from collections import Counter
from contextlib import ExitStack
import json
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy import signal

from repeat_scan import RATE, WINDOW, NATIVE_FIR, distinctive, fractional, profiles


SEARCH = round(1.87*RATE)
BANDS = [(350, 850), (900, 1550), (1600, 2650)]


def supported_hits(hits):
    """Require a second within-target measurement nearby at a consistent delay."""
    support = np.zeros(len(hits), dtype=bool)
    for gap in range(1, min(5, len(hits))):
        consistent = ((hits['frame'][gap:] - hits['frame'][:-gap] <= 4*WINDOW) &
                      (abs(hits['lag'][gap:] - hits['lag'][:-gap]) <= .004*RATE))
        support[gap:] |= consistent
        support[:-gap] |= consistent
    return hits[support]


def spectrum(audio):
    return np.fft.rfft(audio*np.hanning(len(audio)))


def band_agreement(a, b):
    """A shared line or a periodic carrier does not count as broadband speech evidence."""
    hz = np.fft.rfftfreq((len(a)-1)*2, 1/RATE)
    scores = []
    for lo, hi in BANDS:
        mask = (hz >= lo) & (hz < hi)
        x, y = a[mask], b[mask]
        px, py = abs(x)**2, abs(y)**2
        ex, ey = px.sum(), py.sum()
        if (min(ex, ey) < 1e-12 or max(px.max()/ex, py.max()/ey) > .22
                or min(ex/max(np.sum(abs(a)**2), 1e-20), ey/max(np.sum(abs(b)**2), 1e-20)) < .012):
            scores.append(0.)
        else:
            scores.append(float(np.vdot(x, y).real / np.sqrt(ex*ey)))
    return scores


def corroborated(scores):
    values = np.asarray(scores)
    return max(np.count_nonzero(values >= .22), np.count_nonzero(values <= -.22)) >= 2


def section(source, start, count):
    if start < 0 or start+count > len(source):
        return None
    source.seek(start)
    raw = source.read(count, dtype='float32')
    return signal.oaconvolve(raw, NATIVE_FIR, mode='same')


def locate(phrase, donor):
    values = profiles(phrase, donor)
    index, ok = distinctive(values, .35, 32, ratio=1.18)
    if not ok or index < 4 or index >= len(values)-4:
        return None
    k = int(index)
    return fractional(values, k), float(values[k])


def matched_level_db(earlier, later, early_reference, late_reference):
    """Compare the component coherent with the donor inside each target occurrence."""
    def level(target, reference):
        return abs(np.dot(target, reference))/max(np.linalg.norm(reference), 1e-20)
    early = level(earlier, early_reference)
    late = level(later, late_reference)
    return float(20*np.log10(max(late, 1e-20)/max(early, 1e-20)))


def donor_match(stream, earlier, later, early_frame, lag):
    # Each occurrence searches its full signed donor domain independently.
    # A common monitored source on both tapes' channels is permissible.
    first = early_frame - SEARCH
    data = section(stream, first-48, 2*SEARCH+lag+WINDOW+96)
    if data is None:
        return None
    data = data[48:-48]
    early = locate(earlier, data[:2*SEARCH+WINDOW])
    if early is None:
        return None
    late = locate(later, data[lag:lag+2*SEARCH+WINDOW])
    if late is None or abs(early[0] - (lag+late[0])) > 6:
        return None
    k = round(early[0])
    donor = spectrum(data[k:k+WINDOW])
    bands = [band_agreement(spectrum(x), donor) for x in (earlier, later)]
    if not all(corroborated(x) for x in bands):
        return None
    positions = np.arange(WINDOW)
    axis = np.arange(len(data))
    early_donor = np.interp(positions+early[0], axis, data)
    late_donor = np.interp(positions+lag+late[0], axis, data)
    return dict(early_path=(early[0]-SEARCH)/RATE, late_path=(late[0]-SEARCH)/RATE,
                early_corr=early[1], late_corr=late[1], band_correlations=bands,
                later_db=matched_level_db(earlier, later, early_donor, late_donor))


def validate(source, scan, *, spec=None, paths=None):
    spec = spec or json.loads((source.parent/'sources.json').read_bytes())
    entries = {r['row']['channel']: r for r in spec['channels']}
    shifts = spec.get('clock_shift_frames', {})
    target = int(source.stem[2:])
    source_offset = round(entries[target]['row']['start_seconds']*RATE)
    hits = np.load(scan, allow_pickle=False)
    paired = supported_hits(hits)
    counts = Counter(scan_hits=len(hits), locally_consistent=len(paired))
    events = []
    with ExitStack() as stack:
        streams = {ch: stack.enter_context(sf.SoundFile(paths[ch] if paths is not None else source.parent/r['name'])) for ch, r in entries.items()}
        neighbors = sorted((ch for ch in entries if ch != target and
                            entries[ch]['audio']['sha256'] != entries[target]['audio']['sha256']),
                           key=lambda ch: (abs(ch-target), ch))
        for hit in paired:
            frame, lag = int(hit['frame']), round(hit['lag'])
            raw = section(streams[target], frame-48, WINDOW+lag+96)
            if raw is None:
                continue
            earlier, later = raw[48:48+WINDOW], raw[48+lag:48+lag+WINDOW]
            band = band_agreement(spectrum(earlier), spectrum(later))
            if not corroborated(band):
                continue
            counts['broadband_pairs'] += 1
            canonical = source_offset+frame
            for donor in neighbors:
                donor_frame = canonical-round(entries[donor]['row']['start_seconds']*RATE)
                # Only the donor search origin moves. Paths remain fractional residuals
                # about that origin, and both occurrences retain target WAV coordinates.
                shift = shifts.get(str(target),0) - shifts.get(str(donor),0)
                donor_frame += shift
                match = donor_match(streams[donor], earlier, later, donor_frame, lag)
                if match is not None:
                    if shifts:
                        match['donor_center_shift_frames'] = shift
                    events.append(dict(early=canonical/RATE, later=(canonical+float(hit['lag']))/RATE,
                                       total_later_db=float(hit['later_db']), within=float(hit['correlation']),
                                       bands=band, donor=donor, **match))
                    break
    counts['donor_supported_windows'] = len(events)
    return dict(tape=spec['tape'], channel=target, counts=counts, events=events)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('output', type=Path)
    parser.add_argument('scan', type=Path)
    args = parser.parse_args()
    result = validate(args.source, args.scan)
    temp = args.output.with_suffix('.partial')
    temp.write_text(json.dumps(result, allow_nan=False), encoding='utf-8')
    temp.replace(args.output)
    print(json.dumps(result['counts']))
