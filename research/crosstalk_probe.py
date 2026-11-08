"""Bounded pre-deflutter crosstalk experiments in original sample coordinates."""

import argparse
import json
import re
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy import signal


RATE = 8000
CONTEXT = 2 * RATE
LAG_BANDS = {"early": (-1.55, -1.10), "coincident": (-.025, .025),
             "late": (1.10, 1.55)}


def sources(folder):
    channels = {}
    for path in sorted(folder.glob("*.wav")):
        match = re.search(r"_CH(\d+)_", path.name, re.IGNORECASE)
        if match:
            channel = int(match[1])
            if channel in channels:
                raise ValueError(f"Multiple originals for channel {channel}")
            channels[channel] = path
    if not channels:
        raise ValueError(f"No channel WAVs in {folder}")
    return channels


def read_window(path, start, duration):
    start_frame = round(start * RATE) - CONTEXT
    frames = round(duration * RATE) + 2 * CONTEXT
    with sf.SoundFile(path) as wav:
        if wav.samplerate != RATE or wav.channels != 1:
            raise ValueError(f"Expected mono {RATE} Hz: {path}")
        if start_frame < 0 or start_frame + frames > wav.frames:
            raise ValueError(f"Insufficient window context: {path}")
        wav.seek(start_frame)
        return wav.read(frames, dtype="float64")


def analysis_filter():
    hz = np.arange(RATE // 2 + 1)
    distance = abs(hz - 60 * np.rint(hz / 60))
    gain = np.clip(distance - 3, 0, 1) * ((hz >= 320) & (hz <= 3000))
    return signal.firwin2(2049, hz, gain, fs=RATE, window=("kaiser", 8.6))


def lag_peaks(target, donor):
    """Positive lag reads the donor later than the target."""
    reference = target[CONTEXT:-CONTEXT]
    power = np.dot(reference, reference)
    products = signal.correlate(donor, reference, mode="valid", method="fft")
    cumulative = np.r_[0., np.cumsum(donor ** 2)]
    donor_power = cumulative[len(reference):] - cumulative[:-len(reference)]
    correlation = products / np.sqrt(np.maximum(power * donor_power, 1e-30))
    peaks = {}
    for name, (low, high) in LAG_BANDS.items():
        first, last = CONTEXT + round(low * RATE), CONTEXT + round(high * RATE)
        peak = first + int(np.argmax(abs(correlation[first:last + 1])))
        peaks[name] = dict(lag_samples=peak - CONTEXT, correlation=float(correlation[peak]),
                           gain=float(products[peak] / max(donor_power[peak], 1e-30)),
                           donor_over_target_db=float(10 * np.log10(max(donor_power[peak], 1e-30)
                                                                      / max(power, 1e-30))))
    return peaks


def screen(channels, starts, duration):
    kernel = analysis_filter()
    rows = []
    for start in starts:
        filtered = {ch: signal.fftconvolve(read_window(path, start, duration), kernel, mode="same")
                    for ch, path in channels.items()}
        for target, samples in filtered.items():
            for donor in (target - 1, target + 1):
                if donor in filtered:
                    rows.append(dict(start=start, target=target, donor=donor,
                                     peaks=lag_peaks(samples, filtered[donor])))
    return rows


def donor_taps(samples, lag, count, frames):
    first = CONTEXT + lag - count // 2
    last = first + frames + count - 1
    return np.lib.stride_tricks.sliding_window_view(samples[first:last], count)


def score(target, prediction):
    predicted_power = np.dot(prediction, prediction)
    target_power = np.dot(target, target)
    shared = np.dot(target, prediction)
    correlation = shared / max(np.sqrt(predicted_power * target_power), 1e-30)
    residual = target - prediction
    return dict(correlation=float(correlation), residual_correlation=float(
        np.dot(residual, prediction) / max(np.linalg.norm(residual) * np.sqrt(predicted_power), 1e-30)),
        residual_power_change_db=float(10 * np.log10(max(np.dot(residual, residual), 1e-30)
                                                     / max(target_power, 1e-30))),
        held_out_gain_ratio=float(shared / max(predicted_power, 1e-30)))


def trial(channels, target, donor, fit_start, checks, duration, lag, output, *, export_diagnostics=False):
    if target == donor or target not in channels or donor not in channels:
        raise ValueError("Trial requires two distinct available channels")
    starts = sorted([fit_start, *checks])
    if any(b - a < duration + 2 * CONTEXT / RATE for a, b in zip(starts, starts[1:])):
        raise ValueError("Fit and check windows must have disjoint source context")
    if not any(low <= lag / RATE <= high for low, high in LAG_BANDS.values()):
        raise ValueError("Lag must be in one of the screened bands")
    kernel = analysis_filter()
    frames = round(duration * RATE)
    evaluation_kernels = [signal.firwin(513, edges, fs=RATE, pass_zero=False)
                          for edges in [(320, 800), (800, 1450), (1450, 2200), (2200, 3000)]]

    def window(start):
        raw = read_window(channels[target], start, duration)
        source = read_window(channels[donor], start, duration)
        return (raw[CONTEXT:-CONTEXT],
                signal.fftconvolve(raw, kernel, mode="same")[CONTEXT:-CONTEXT],
                signal.fftconvolve(source, kernel, mode="same"))

    def evaluate(y, prediction):
        result = score(y, prediction)
        result["band_scores"] = [score(signal.fftconvolve(y, band, mode="same"),
                                       signal.fftconvolve(prediction, band, mode="same"))
                                 for band in evaluation_kernels]
        return result

    _, y_fit, donor_fit = window(fit_start)
    models = {"scalar": (lag, 1), "fir": (lag, 33), "zero_control": (0, 33),
              "wrong_lag_control": (lag + (2400 if lag <= 0 else -2400), 33)}
    coefficients = {}
    for name, (model_lag, taps) in models.items():
        design = np.ascontiguousarray(donor_taps(donor_fit, model_lag, taps, frames))
        gram = design.T @ design
        ridge = max(.01 * np.trace(gram) / taps, 1e-20)
        coefficients[name] = np.linalg.solve(gram + ridge * np.eye(taps), design.T @ y_fit)

    evaluations = []
    fade = np.ones(frames)
    edge = min(round(.05 * RATE), frames // 2)
    fade[:edge] = np.sin(np.linspace(0, np.pi / 2, edge)) ** 2
    fade[frames - edge:] = fade[:edge][::-1]
    output.mkdir()
    for index, start in enumerate(checks, 1):
        raw, y, x = window(start)
        predictions = {name: fade * (donor_taps(x, model_lag, taps, frames) @ coefficients[name])
                       for name, (model_lag, taps) in models.items()}
        ratings = {name: evaluate(y, pred) for name, pred in predictions.items()}
        fir = ratings["fir"]
        gates = dict(fit_transfers=.65 <= fir["held_out_gain_ratio"] <= 1.5,
                     multiband=sum(b["held_out_gain_ratio"] > .5 for b in fir["band_scores"]) >= 2,
                     beats_controls=all(fir["residual_power_change_db"] <
                                        ratings[key]["residual_power_change_db"] - .02
                                        for key in ("zero_control", "wrong_lag_control")),
                     reduces_energy=fir["residual_power_change_db"] <= -.03)
        passed = all(gates.values())
        evaluations.append(dict(start=start, scores=ratings, fir_gates=gates, fir_passed=passed))
        if passed or export_diagnostics:
            # Common fixed gain and untouched target coordinates for Audition.
            versions = {"baseline": raw, "scalar": raw - predictions["scalar"],
                        "fir": raw - predictions["fir"], "removed_fir": predictions["fir"]}
            gain = .99 / max(.99, *(np.max(abs(wav)) for wav in versions.values()))
            evaluations[-1]["common_gain"] = gain
            evaluations[-1]["audio_export"] = "candidate" if passed else "failed_diagnostic"
            for label, wav in versions.items():
                sf.write(output / f"{index:02d}_{label}.wav", wav * gain, RATE, subtype="PCM_24")
    report = dict(rate=RATE, duration=duration, target=target, donor=donor, fit_start=fit_start,
                  sources={ch: str(channels[ch].resolve()) for ch in (target, donor)},
                  models={name: dict(lag_samples=models[name][0], coefficients=value.tolist())
                          for name, value in coefficients.items()}, evaluations=evaluations)
    (output / "trial.json").write_text(json.dumps(report, indent=2), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    screen_parser = commands.add_parser("screen")
    screen_parser.add_argument("--starts", nargs="+", type=float, required=True)
    trial_parser = commands.add_parser("trial")
    trial_parser.add_argument("--pair", nargs=2, type=int, metavar=("TARGET", "DONOR"), required=True)
    trial_parser.add_argument("--fit-start", type=float, required=True)
    trial_parser.add_argument("--checks", nargs="+", type=float, required=True)
    trial_parser.add_argument("--lag-samples", type=int, required=True)
    trial_parser.add_argument("--export-diagnostics", action="store_true",
                              help="Also export comparisons for failed check windows")
    for command in (screen_parser, trial_parser):
        command.add_argument("input", type=Path)
        command.add_argument("output", type=Path)
        command.add_argument("--duration", type=float, default=12)
    args = parser.parse_args()
    if round(args.duration * RATE) < 2:
        parser.error("duration must include at least two samples")
    channels = sources(args.input)
    if args.command == "trial":
        trial(channels, *args.pair, args.fit_start, args.checks, args.duration, args.lag_samples,
              args.output, export_diagnostics=args.export_diagnostics)
        return
    rows = screen(channels, args.starts, args.duration)
    with args.output.open("x", encoding="utf-8") as handle:
        json.dump(dict(sources={ch: str(path.resolve()) for ch, path in channels.items()},
                       rate=RATE, duration=args.duration, lag_bands_seconds=LAG_BANDS,
                       lag_definition="donor sample minus target sample", rows=rows), handle, indent=2)


if __name__ == "__main__":
    main()
