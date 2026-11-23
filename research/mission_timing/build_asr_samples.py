"""Materialize the published ASR pilot sample sets for local archive paths."""

import argparse
from hashlib import sha256
import json
from pathlib import Path
import wave


SPEC = Path(__file__).with_name("asr_samples.json")
SETS = ("census_T510093", "census_T510077", "census_T510100", "sustained_T510093",
        "matched", "dvd", "pao_full", "census_audit", "audit_with_silence")


def tape_inventory(root, tape, entry):
    if root is None:
        raise ValueError("Provide --stage05-root for this sample set")
    inventory = {}
    edition = sha256()
    for channel in entry.get("channels", range(1, 31)):
        name = f"CH{channel:02}"
        directory = root / tape / name
        review = directory / "review.json"
        audio = directory / "deflutter.wav"
        data = json.loads(review.read_text(encoding="utf-8"))
        if not audio.is_file() or (data["status"], data["tape"], data["channel"]) != (
            "complete", tape, channel,
        ):
            raise ValueError(f"Incomplete or mismatched stage-05b release at {directory}")
        digest = data["files"][audio.name]["sha256"]
        edition.update(f"{name}:{digest}\n".encode("ascii"))
        inventory[channel] = {"source": str(audio), "review": str(review), "sha256": digest,
                              "volume": f"{tape} {name} stage-05b"}
    if edition.hexdigest() != entry["reviewed_audio_set_sha256"]:
        raise ValueError(f"Published audio receipts at {root / tape} differ from the pilot edition")
    return inventory


def write_silence(directory, spec):
    path = directory / spec["filename"]
    if not path.exists():
        with path.open("xb") as output, wave.open(output, "wb") as audio:
            audio.setparams((1, 2, spec["sample_rate"], spec["frames"], "NONE", "not compressed"))
            audio.writeframes(bytes(spec["frames"] * 2))
    if sha256(path.read_bytes()).hexdigest() != spec["sha256"]:
        raise ValueError(f"Existing silence control differs from the pilot at {path}")
    return {"id": "digital_silence", "source": str(path), "sha256": spec["sha256"],
            "volume": "60 seconds of generated PCM silence"}


def build(args, spec):
    inventories = {}

    def tape(tape_id):
        if tape_id not in inventories:
            inventories[tape_id] = tape_inventory(args.stage05_root, tape_id, spec["tapes"][tape_id])
        return inventories[tape_id]

    def external(name):
        entry = spec["media"][name]
        directory = getattr(args, f"{name}_dir")
        if directory is None:
            raise ValueError(f"Provide --{name}-dir for this sample set")
        path = directory / entry["filename"]
        if path.stat().st_size != entry["size"]:
            raise ValueError(f"Source size differs from the pilot edition at {path}")
        return {"source": str(path), "sha256": entry["sha256"], "stream": entry["stream"],
                "volume": entry["volume"]}

    def excerpt(row):
        source = tape(row["tape"])[row["track"]] if "tape" in row else external(row["volume"])
        return {"id": row["id"], **source, "start": row["start"], "end": row["end"]}

    if args.set == "pao_full":
        return [{"id": "pao_572aaa", **external("pao")}]
    if args.set == "dvd":
        return [excerpt(row) for row in spec["matched"] if row.get("volume") == "dvd"]
    if args.set == "matched":
        return [excerpt(row) for row in spec["matched"]]
    if args.set == "sustained_T510093":
        span = spec["sustained"]
        return [{"id": f"CH{channel:02}", **source, "start": span["start"], "end": span["end"]}
                for channel, source in tape(span["tape"]).items()]

    tape_id = f"A15_{args.set.removeprefix('census_')}" if args.set.startswith("census_T") else "A15_T510093"
    entry = spec["tapes"][tape_id]
    overrides = {channel: group["starts"] for group in entry.get("census_starts_by_channel_group", [])
                 for channel in group["channels"]}
    census = [
        {"id": f"CH{channel:02}_{part}", **source, "start": start, "end": start + spec["census"]["duration"]}
        for channel, source in tape(tape_id).items()
        for part, start in zip(("early", "middle", "late"), overrides.get(channel, entry["census_starts"]))
    ]
    if args.set in {"census_audit", "audit_with_silence"}:
        census = [row for row in census if row["id"] in spec["census"]["audit_ids"]]
    if args.set == "audit_with_silence":
        census.append(write_silence(args.output.parent, spec["silence"]))
    return census


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--set", choices=SETS, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--stage05-root", type=lambda value: Path(value).resolve(), help="Published defluttered_shared directory")
    for name in ("pao", "dvd", "spur"):
        parser.add_argument(f"--{name}-dir", type=lambda value: Path(value).resolve())
    args = parser.parse_args()
    args.output = args.output.resolve()
    if args.output.exists():
        parser.error(f"Output already exists at {args.output}")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    rows = build(args, json.loads(SPEC.read_text(encoding="utf-8")))
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(rows, stream, indent=2)
        stream.write("\n")
    print(f"Wrote {len(rows)} inputs to {args.output}")


if __name__ == "__main__":
    main()
