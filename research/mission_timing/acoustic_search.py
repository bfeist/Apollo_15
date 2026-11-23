"""Compare short PAO or journal passages with specific 05b and DVD editions.

The search reports the strongest peak and a distant competitor for every
window. Inspect phrase order, agreement across windows and control hits before
interpreting a peak. Offsets are target elapsed time minus query elapsed time.
"""

import argparse
import csv
import json
import math
import subprocess
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy import ndimage, signal


RATE = 4000
HOP = 80
DT = HOP / RATE


def read_audio(path, start, duration, rate=RATE):
    if path.suffix.lower() == ".wav":
        with sf.SoundFile(path) as reader:
            reader.seek(round(start * reader.samplerate))
            data = reader.read(
                round(duration * reader.samplerate), dtype="float32", always_2d=True
            )
            divisor = math.gcd(rate, reader.samplerate)
            return signal.resample_poly(
                data.mean(axis=1), rate // divisor, reader.samplerate // divisor
            )
    args = [
        "ffmpeg",
        "-v",
        "error",
        "-ss",
        str(start),
        "-i",
        str(path),
        "-t",
        str(duration),
        "-map",
        "0:a:0",
        "-ac",
        "1",
        "-ar",
        str(rate),
        "-f",
        "f32le",
        "pipe:1",
    ]
    return np.frombuffer(subprocess.check_output(args), dtype="<f4")


def features(audio):
    # Locally centred band energies suppress a persistent carrier or gain drift.
    frames = np.lib.stride_tricks.sliding_window_view(audio, 256)[::HOP]
    bands = np.arange(200, 1901, 100)
    edges = np.searchsorted(np.fft.rfftfreq(256, 1 / RATE), bands)
    out = []
    window = np.hanning(256).astype("f")
    for start in range(0, len(frames), 10000):
        spectrum = np.abs(np.fft.rfft(frames[start : start + 10000] * window)) ** 2
        energies = np.add.reduceat(spectrum, edges, axis=1)[:, :-1]
        out.append(np.log(np.maximum(energies, 1e-10)).astype("f"))
    energy = np.concatenate(out)
    contrast = energy - ndimage.uniform_filter1d(energy, 75, axis=0)
    return np.clip(contrast, -5, 5)


def scores(target, query):
    n = len(query)
    kernel = query - query.mean(axis=0)
    products = signal.fftconvolve(target, kernel[::-1], mode="valid", axes=0).sum(
        axis=1
    )
    power = np.r_[0.0, np.cumsum(np.sum(target * target, axis=1), dtype="d")]
    denominator = np.sqrt(
        np.maximum((power[n:] - power[:-n]) * (kernel * kernel).sum(), 1e-12)
    )
    return products / denominator


def find_peak(target, query):
    values = scores(target, query)
    best = int(np.argmax(values))
    mask = np.abs(np.arange(len(values)) - best) > round(4 / DT)
    competitor = float(values[mask].max()) if mask.any() else None
    return best * DT, float(values[best]), competitor


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--query", required=True, type=Path)
    parser.add_argument("--query-starts", required=True, type=float, nargs="+")
    parser.add_argument("--length", type=float, default=12)
    parser.add_argument(
        "--targets", required=True, type=Path, help="JSON list: path, start, duration"
    )
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    queries = [
        (q, features(read_audio(args.query, q, args.length))) for q in args.query_starts
    ]
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(
            [
                "target",
                "target_window_start",
                "query_start",
                "query_length",
                "target_start",
                "target_minus_query",
                "similarity",
                "competitor",
            ]
        )
        for spec in json.loads(args.targets.read_text(encoding="utf-8-sig")):
            path, start = Path(spec["path"]), spec["start"]
            target = features(read_audio(path, start, spec["duration"]))
            for q, query in queries:
                if len(target) < len(query):
                    continue
                elapsed, score, other = find_peak(target, query)
                writer.writerow(
                    [
                        str(path),
                        start,
                        q,
                        args.length,
                        start + elapsed,
                        start + elapsed - q,
                        score,
                        other,
                    ]
                )
            stream.flush()


if __name__ == "__main__":
    main()
