"""Time the first words of distinctive scan-verified calls using a second acoustic model.

Requires torch and torchaudio 2.8. Store all retrieval decisions, independent
greedy readings, and token endpoints so the acoustic picks can be audited.
"""

import argparse
import csv
import hashlib
import json
import re
from pathlib import Path

import numpy as np
import torch
import torchaudio

from acoustic_search import read_audio
from find_calls import tokens, word_index


@torch.inference_mode()
def align_phrase(model, labels, path, words):
    origin = max(0, words[0][1] - 3)
    stop = words[-1][2] + 3
    if stop - origin > 60:
        return None
    audio = read_audio(path, origin, stop - origin, rate=16000)
    logits, _ = model(torch.from_numpy(audio)[None, :])
    logprob = logits.log_softmax(dim=-1)
    greedy = "".join(
        labels[i] for i in logits[0].argmax(-1).unique_consecutive() if i
    ).replace("|", " ")
    # A wildcard on either side absorbs neighboring speech in the padded crop.
    # Give it the best ordinary emission at that frame, so a good acoustic
    # match is preferred to stretching the first character into the padding.
    wildcard = len(labels)
    extended = torch.cat((logprob, logprob.max(dim=-1, keepdim=True).values), dim=-1)
    target = [wildcard]
    spellings = []
    for word, _, _ in words:
        if spellings:
            target.append(labels.index("|"))
        spelling = word.upper()
        target.extend(labels.index(c) for c in spelling)
        spellings.append(spelling)
    target.append(wildcard)
    path_ids, path_scores = torchaudio.functional.forced_align(
        extended, torch.tensor([target])
    )
    spans = torchaudio.functional.merge_tokens(path_ids[0], path_scores[0].exp())
    pos, results = 1, []
    for spelling, (_, raw_start, raw_end) in zip(spellings, words):
        part = spans[pos : pos + len(spelling)]
        # The encoder has a 320-sample stride, 400-sample receptive field.
        # Use the centre of the first/last emitting receptive field.
        start = origin + (part[0].start * 320 + 199.5) / 16000
        end = origin + ((part[-1].end - 1) * 320 + 199.5) / 16000
        results.append(
            dict(
                word=spelling.lower(),
                whisper_start=raw_start,
                whisper_end=raw_end,
                emission_start=start,
                emission_end=end,
                mean_character_score=float(np.mean([s.score for s in part])),
            )
        )
        pos += len(spelling) + 1
    return dict(greedy=greedy.strip().lower(), words=results)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reviews", type=Path)
    parser.add_argument("candidates", type=Path)
    parser.add_argument("segments", type=Path)
    parser.add_argument("audio", type=Path)
    parser.add_argument("outdir", type=Path)
    args = parser.parse_args()
    args.outdir.mkdir(parents=True, exist_ok=True)
    torch.set_num_threads(3)
    bundle = torchaudio.pipelines.WAV2VEC2_ASR_BASE_960H
    model = bundle.get_model().eval()
    labels = bundle.get_labels()
    with args.reviews.open(encoding="utf-8-sig") as stream:
        reviews = {r["event_id"]: r for r in csv.DictReader(stream, delimiter="\t")}
    words = word_index(args.segments)
    index = {(w[1], w[0]): i for i, w in enumerate(words)}
    anchors, audits = [], []
    for line in args.candidates.read_text(encoding="utf-8").splitlines():
        candidate = json.loads(line)
        best, other = candidate["best"], candidate["other"]
        event = candidate["event_id"]
        row = reviews[event]
        audit = dict(event_id=event, start=best["start"], score=best["score"])
        audits.append(audit)
        audit["status"] = "unqualified lexical identity or missing initial word"
        if (
            best["score"] < 0.66
            or best["matched"] < 6
            or best["leading_missing"]
            or best["score"] - other.get("score", 0) < 0.15
            or "candidate" not in row["reference_status"]
        ):
            continue
        first = index[(best["start"], tokens(best["audio_text"])[0])]
        sample = []
        for word in words[first : first + min(7, len(tokens(best["audio_text"])))]:
            if not re.fullmatch("[a-z]+", word[0]):
                break
            sample.append(word)
        if len(sample) < 3:
            continue
        result = align_phrase(model, labels, args.audio, sample)
        if not result:
            audit["status"] = "crop too long"
            continue
        audit.update(result)
        picked = result["words"][0]
        # Demand separate unconstrained phonetic recognition of the opening
        # word (including common recognition spellings) near its forced pick.
        check = tokens(result["greedy"])
        variants = {"okay": {"ok", "okay", "kay"}, "roger": {"roger", "rodger"}}
        first_forms = variants.get(picked["word"], {picked["word"]})
        if not first_forms.intersection(check) or picked["mean_character_score"] < 0.55:
            audit["status"] = "opening word uncertain in acoustic alignment"
            continue
        audit["status"] = "qualified"
        onset = round(picked["emission_start"] * 48000)
        anchors.append(
            dict(
                anchor_id=event,
                exchange_id=row["exchange_group"],
                source_seconds=onset / 48000,
                source_sample=onset,
                get_seconds=row["get_seconds"],
                speaker=row["printed_speaker"],
                direction=row["direction"],
                channel="stereo mean",
                pdf_page=row["pdf_page"],
                printed_page=row["printed_page"],
                printed_stamp=row["printed_stamp"],
                transcript_locator=row["scan_url"],
                landmark="first letter emission of opening word",
                pick_sigma_seconds=0.25,
                transcript_sigma_seconds=1.0,
                whisper_start=best["start"],
                text=row["printed_text"],
                status="accepted",
            )
        )
    (args.outdir / "acoustic_audit.jsonl").write_text(
        "".join(json.dumps(a) + "\n" for a in audits), encoding="utf-8"
    )
    if not anchors:
        raise ValueError("No calls qualified")
    with (args.outdir / "ctc_candidates.csv").open(
        "w", newline="", encoding="utf-8"
    ) as out:
        writer = csv.DictWriter(out, fieldnames=anchors[0])
        writer.writeheader()
        writer.writerows(anchors)
    inputs = [
        args.reviews,
        args.candidates,
        args.segments,
        Path(torch.hub.get_dir())
        / "checkpoints"
        / "wav2vec2_fairseq_base_ls960_asr_ls960.pth",
    ]
    receipts = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in inputs}
    (args.outdir / "acoustic_alignment.json").write_text(
        json.dumps(
            dict(
                torch=torch.__version__,
                torchaudio=torchaudio.__version__,
                device="cpu",
                model="WAV2VEC2_ASR_BASE_960H",
                sha256=receipts,
            ),
            indent=2,
        )
    )
    print(f"{len(anchors)} qualified calls from {len(audits)} candidates")


if __name__ == "__main__":
    main()
