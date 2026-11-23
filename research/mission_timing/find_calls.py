"""Retrieve scan rows in a reel's word index without using the printed time.

This produces candidates for review. A high lexical score can also belong to
a repeated transmission or start after a missed word at the start of the line.
"""

import argparse
import csv
import difflib
import json
import re
from collections import Counter, defaultdict
from pathlib import Path


def tokens(text):
    return re.findall(r"[a-z0-9]+", text.lower().replace("'", "").replace("’", ""))


def word_index(path):
    words = []
    for line in path.read_text(encoding="utf-8").splitlines():
        for word in json.loads(line)["words"]:
            for token in tokens(word["word"]):
                words.append((token, word["start"], word["end"]))
    return words


def search(text, words, lookup):
    phrase = tokens(text)[:18]
    if len(phrase) < 3:
        return []
    votes = Counter()
    for j, token in enumerate(phrase):
        indices = lookup[token]
        if len(indices) > 800:
            continue
        for i in indices:
            for delta in (-1, 0, 1):
                k = i - j + delta
                if k >= 0:
                    votes[k] += 1 / (1 + len(indices) ** 0.4)
    hits = {}
    for start, _ in votes.most_common(55):
        sample = words[start : start + len(phrase) + 5]
        blocks = difflib.SequenceMatcher(
            None, phrase, [w[0] for w in sample], autojunk=False
        ).get_matching_blocks()[:-1]
        if not blocks:
            continue
        first, last = blocks[0], blocks[-1]
        count = sum(b.size for b in blocks)
        low, high = start + first.b, start + last.b + last.size
        span = words[high - 1][2] - words[low][1]
        # Include edit and boundary costs. Sustained gaps require review.
        score = 2 * count / (len(phrase) + high - low + first.a)
        if span > 6 + 1.7 * len(phrase):
            score *= 0.7
        entry = dict(
            start=words[low][1],
            end=words[high - 1][2],
            leading_missing=first.a,
            matched=count,
            score=round(score, 4),
            audio_text=" ".join(w[0] for w in words[low:high]),
            context=" ".join(w[0] for w in words[max(0, low - 5) : high + 4]),
        )
        if low not in hits or hits[low]["score"] < score:
            hits[low] = entry
    return sorted(hits.values(), key=lambda h: -h["score"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reviews", type=Path)
    parser.add_argument("segments", type=Path)
    parser.add_argument("out", type=Path)
    parser.add_argument("--field", default="text_ocr")
    args = parser.parse_args()
    words = word_index(args.segments)
    lookup = defaultdict(list)
    for i, word in enumerate(words):
        lookup[word[0]].append(i)
    with (
        args.reviews.open(encoding="utf-8-sig") as file,
        args.out.open("w", encoding="utf-8") as out,
    ):
        for row in csv.DictReader(file, delimiter="\t"):
            candidates = search(row[args.field], words, lookup)
            if candidates:
                best = candidates[0]
                other = next(
                    (
                        c
                        for c in candidates
                        if c["end"] < best["start"] - 1 or c["start"] > best["end"] + 1
                    ),
                    {},
                )
                out.write(
                    json.dumps(
                        dict(
                            event_id=row["event_id"],
                            query=row[args.field],
                            best=best,
                            other=other,
                        )
                    )
                    + "\n"
                )


if __name__ == "__main__":
    main()
