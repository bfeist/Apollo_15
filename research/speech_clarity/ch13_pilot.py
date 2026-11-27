"""Render matched CH13 listening excerpts using local LavaSR v2 and in-band EQ.

Place LavaSR and its matcha Vocos dependency on PYTHONPATH. Supply the local
enhancer_v2 weights directory. See the output manifest for the tested revisions.
"""

import argparse
import hashlib
import json
from pathlib import Path

import numpy as np
import soundfile as sf
import torch
from LavaSR.enhancer.enhancer import LavaBWE
from scipy import ndimage, signal


SOURCE_RATE = 8000
OUTPUT_RATE = 48000
CLIP_SECONDS = 24
CONTEXT_SECONDS = 1
EXCERPTS = {"readback": 1246, "hose": 12036}


def activity_envelope(wave):
    # Only the added detail follows this envelope. The source always plays at unity.
    sos = signal.butter(3, [350, 1800], fs=SOURCE_RATE, btype="bandpass", output="sos")
    voice = signal.sosfiltfilt(sos, wave)
    times = np.arange(0, len(voice), 80)
    power = ndimage.uniform_filter1d(voice**2, size=320)[times]
    level = 10 * np.log10(np.maximum(power, 1e-12))
    floor = np.percentile(level, 15)
    amount = np.clip((level - floor - 5) / 12, 0, 1)
    amount = ndimage.gaussian_filter1d(amount**2 * (3 - 2 * amount), sigma=2)
    return np.interp(np.arange(len(wave)), times, amount)


def versions(wave, model):
    envelope = activity_envelope(wave)
    # Symmetric FIR adds up to 3 dB around 2 kHz without delaying the dry path.
    shape = signal.firwin2(
        161, [0, 700, 1100, 1650, 2100, 2600, 3250, 3650, 4000],
        [0, 0, 0.20, 0.85, 1, 0.8, 0.2, 0, 0], fs=SOURCE_RATE,
    )
    detail = signal.fftconvolve(wave, shape, mode="same")
    lifted = wave + envelope * (10**(3 / 20) - 1) * detail
    dry = signal.resample_poly(wave, 6, 1)
    eq = signal.resample_poly(lifted, 6, 1)

    # Drive the BWE network at a consistent level, undo that gain, and retain
    # only its added treble. One second of real context at each end is discarded.
    drive = 0.75 / max(np.max(abs(dry)), 1e-6)
    tensor = torch.from_numpy((dry * drive).astype("float32")).unsqueeze(0)
    with torch.inference_mode():
        prediction = model.infer(tensor).squeeze().cpu().numpy() / drive
    treble_filter = signal.firwin(1025, [4250, 6500], pass_zero=False, fs=OUTPUT_RATE)
    treble = signal.fftconvolve(prediction, treble_filter, mode="same")
    samples = np.arange(len(treble))
    amount = np.interp(samples / 6, np.arange(len(wave)), envelope)
    neural = dry.copy()
    neural[:len(treble)] += 0.35 * amount * treble
    take = slice(CONTEXT_SECONDS * OUTPUT_RATE, (CONTEXT_SECONDS + CLIP_SECONDS) * OUTPUT_RATE)
    assert len(treble) >= take.stop
    return {"A": dry[take], "B": eq[take], "C": neural[take]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("weights", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    device = "cuda" if torch.cuda.is_available() else "cpu"
    model = LavaBWE(str(args.weights), device=device)
    info = {
        "source": str(args.source.resolve()),
        "source_stage": "05b, delivered CH13, local elapsed seconds",
        "output_rate": OUTPUT_RATE, "mono_subtype": "FLOAT", "output_gain_db": 0,
        "methods": {"A": "source resampled", "B": "activity-weighted presence EQ",
                    "C": "source plus 35% activity-weighted 4.25-6.5 kHz LavaSR v2 output"},
        "lava_commit": "33ac040892519c1bb4aed7eb32e79af51cc29e2a",
        "vocos_matcha_commit": "451e522f7a11c3652c9522f63ea6780736d93de0",
        "weights_revision": "YatharthS/LavaSR@b98dc8be472da45ab7b6346ad7997e1dfeb5911d",
        "weights_sha256": hashlib.sha256((args.weights / "pytorch_model.bin").read_bytes()).hexdigest(),
        "excerpts": {},
    }
    with sf.SoundFile(args.source) as recording:
        assert recording.channels == 1 and recording.samplerate == SOURCE_RATE
        for name, start in EXCERPTS.items():
            folder = args.output / name
            folder.mkdir()
            recording.seek((start - CONTEXT_SECONDS) * SOURCE_RATE)
            wave = recording.read((CLIP_SECONDS + 2 * CONTEXT_SECONDS) * SOURCE_RATE)
            clips = versions(wave, model)
            info["excerpts"][name] = {
                "start_s": start, "duration_s": CLIP_SECONDS,
                "source_interval_f64le_sha256": hashlib.sha256(
                    wave[SOURCE_RATE:-SOURCE_RATE].astype("<f8").tobytes()
                ).hexdigest(),
            }
            for label, clip in clips.items():
                assert len(clip) == CLIP_SECONDS * OUTPUT_RATE and np.isfinite(clip).all()
                sf.write(folder / f"{label}.wav", clip, OUTPUT_RATE, subtype="FLOAT")
    (args.output / "manifest.json").write_text(json.dumps(info, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
