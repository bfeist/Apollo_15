"""Extract timestamped rows from the Apollo 15 technical transcript text layer."""

# /// script
# requires-python = ">=3.10"
# dependencies = ["pymupdf==2.1"]
# ///

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import unicodedata
from collections import Counter
from pathlib import Path
from statistics import median

import pymupdf


EDITION = "AS15_TEC"
SOURCE_URL = "https://apollojournals.org/alsj/a15/AS15_TEC.PDF"
STAMP_DIGITS = str.maketrans({"O": "0", "o": "0", "l": "1", "I": "1"})
SPEAKERS = {"CC", "CDR", "LMP", "CMP", "SC", "CDR-EVA", "LMP-EVA", "CDR-LM", "LMP-LM"}
FIELDS = (
    "event_id", "pdf_page", "printed_page_ocr", "transcript_tape_ocr", "timed_row",
    "stamp_ocr", "get", "get_seconds", "speaker_ocr", "text_ocr", "search_text",
    "flags", "link_context", "x0_pt", "y0_pt", "x1_pt", "y1_pt", "scan_url",
)


def get_seconds(stamp: str) -> int | None:
    parts = stamp.translate(STAMP_DIGITS).split()
    if len(parts) != 4 or any(not re.fullmatch(r"[0-9]{2}", part) for part in parts):
        return None
    day, hour, minute, second = map(int, parts)
    if hour >= 24 or minute >= 60 or second >= 60:
        return None
    return day * 86400 + 3600 * hour + 60 * minute + second


def format_get(seconds: int) -> str:
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:03d}:{minutes:02d}:{seconds:02d}"


def normalize_text(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).casefold()
    text = re.sub(r"['’]", "", text)
    return " ".join(re.findall(r"[a-z0-9]+", text))


def page_lines(page: pymupdf.Page) -> list[list[tuple]]:
    words = sorted(page.get_text("words"), key=lambda word: (word[1], word[0]))
    lines = []
    for word in words:
        if not lines or word[1] - lines[-1][0][1] > 3:
            lines.append([])
        lines[-1].append(word)
    return [sorted(line, key=lambda word: word[0]) for line in lines]


def find_stamp_columns(lines: list[list[tuple]]) -> list[float]:
    samples = []
    for line in lines:
        for index in range(len(line) - 4):
            words = line[index:index + 4]
            x = [word[0] for word in words]
            if all(12 <= b - a <= 24 for a, b in zip(x, x[1:])) and get_seconds(
                " ".join(word[4] for word in words)
            ) is not None:
                samples.append(x)
                break
    return [median(column) for column in zip(*samples)]


def header_value(lines: list[list[tuple]], label: str) -> str:
    for line in lines:
        if min(word[1] for word in line) > 118:
            break
        for index, word in enumerate(line):
            if word[4].casefold() == label and index + 1 < len(line):
                following = line[index + 1]
                if following[0] - word[0] < 65:
                    return following[4]
    return ""


def extract_page(page: pymupdf.Page, link_context: str) -> tuple[list[dict], str]:
    lines = page_lines(page)
    columns = find_stamp_columns(lines)
    if not columns:
        raise ValueError(f"No stamp columns found on PDF page {page.number + 1}")
    stamp_x, *_, last_stamp_x = columns
    text_x = stamp_x + 133
    printed_page = header_value(lines, "page")
    transcript_tape = header_value(lines, "tape")
    page_number = page.number + 1
    rows = []
    pending = []
    row_number = 0

    def append_row(words: list[tuple], stamp: str, speaker: str, line: list[tuple]) -> None:
        nonlocal row_number
        if stamp:
            row_number += 1
            marker = f"r{row_number:02}"
        else:
            marker = f"u{round(min(word[1] for word in line)):03}"
        flags = []
        seconds = get_seconds(stamp)
        if not stamp:
            flags.append("untimed_continuation")
        elif seconds is None:
            flags.append("invalid_stamp_ocr")
        elif stamp != stamp.translate(STAMP_DIGITS):
            flags.append("inferred_stamp_digits")
        if stamp and speaker not in SPEAKERS:
            flags.append("unrecognized_speaker_ocr")
        if not printed_page.isdigit():
            flags.append("unreliable_printed_page_ocr")
        rows.append({
            "event_id": f"{EDITION}-p{page_number:04}-{marker}",
            "pdf_page": page_number,
            "printed_page_ocr": printed_page,
            "transcript_tape_ocr": transcript_tape,
            "timed_row": row_number if stamp else "",
            "stamp_ocr": stamp,
            "get": format_get(seconds) if seconds is not None else "",
            "get_seconds": seconds,
            "speaker_ocr": speaker,
            "flags": flags,
            "link_context": link_context,
            "words": words.copy(),
            "line": line.copy(),
            "scan_url": f"{SOURCE_URL}#page={page_number}",
        })

    for line in lines:
        line_text = " ".join(word[4] for word in line)
        if re.search(r"SIMULTANEOUS .*LINK IN USE BETWEEN CC AND CM", line_text, re.I):
            link_context = "cc_cm"
            continue
        if re.search(r"TRANSCRIPT OF COMMUNICATIONS BETWEEN CC AND (?:LM|IN)\s*RESUMED", line_text, re.I):
            link_context = "cc_lm"
            continue
        stamp_words = []
        for column in columns:
            matches = [word for word in line if abs(word[0] - column) <= 7]
            stamp_words.append(min(matches, key=lambda word: abs(word[0] - column)) if matches else None)
        speaker_words = [word for word in line if 14 <= word[0] - last_stamp_x <= 36]
        is_body = min(word[1] for word in line) >= 109
        if is_body and speaker_words and sum(word is not None for word in stamp_words) >= 3:
            if pending:
                append_row(pending, "", "", pending)
                pending.clear()
            stamp = " ".join(word[4] if word else "?" for word in stamp_words)
            speaker = " ".join(word[4] for word in speaker_words)
            left = [word for word in line if word[0] >= stamp_x - 7]
            words = [word for word in left if word[0] >= text_x - 10]
            append_row(words, stamp, speaker, left)
        elif is_body:
            words = [word for word in line if text_x - 10 <= word[0] < page.rect.width - 52]
            if not words or re.search(r"AIR-TO-GROUND|END OF TAPE|TRANSCRIPT OF COMMUNICATIONS", line_text, re.I):
                continue
            if rows:
                rows[-1]["words"].extend(words)
                rows[-1]["line"].extend(words)
                if "has_untimed_wrap" not in rows[-1]["flags"]:
                    rows[-1]["flags"].append("has_untimed_wrap")
            else:
                pending.extend(words)
    if pending:
        append_row(pending, "", "", pending)

    for row in rows:
        words, line = row.pop("words"), row.pop("line")
        row["text_ocr"] = " ".join(word[4] for word in words)
        row["search_text"] = normalize_text(row["text_ocr"])
        for index, (field, reducer) in enumerate(zip(FIELDS[-5:-1], (min, min, max, max))):
            row[field] = round(reducer(word[index] for word in line), 1)
        if row["text_ocr"].startswith(("- -", "--")) or row["text_ocr"].endswith(("- -", "--")):
            row["flags"].append("interrupted_or_continued_call")
    return rows, link_context


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdf", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--first-page", type=int, default=784)
    parser.add_argument("--last-page", type=int, default=910)
    parser.add_argument("--initial-link", choices=("cc_lm", "cc_cm", "unknown"))
    args = parser.parse_args()
    with pymupdf.open(args.pdf) as source:
        if not 1 <= args.first_page <= args.last_page <= len(source):
            parser.error("Expected a valid inclusive range of one-based PDF pages.")
        initial_link = args.initial_link or ("cc_lm" if args.first_page == 784 else "unknown")
        link_context = initial_link
        rows = []
        for page_index in range(args.first_page - 1, args.last_page):
            page_rows, link_context = extract_page(source[page_index], link_context)
            rows.extend(page_rows)

    prior = None
    stamp_counts = Counter(row["get_seconds"] for row in rows if row["get_seconds"] is not None)
    for row in rows:
        current = row["get_seconds"]
        if current is not None:
            if prior is not None and current < prior:
                row["flags"].append("get_regression_in_print_order")
            if stamp_counts[current] > 1:
                row["flags"].append("shared_whole_second_stamp")
            prior = current
        row["flags"] = ";".join(row["flags"])

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, FIELDS, dialect="excel-tab", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    metadata = {
        "edition": EDITION,
        "source_url": SOURCE_URL,
        "source_sha256": hashlib.sha256(args.pdf.read_bytes()).hexdigest(),
        "extractor": "PyMuPDF " + pymupdf.__version__,
        "first_pdf_page": args.first_page,
        "last_pdf_page": args.last_page,
        "initial_link_context": initial_link,
        "rows": len(rows),
        "status": "embedded_ocr_unreviewed",
    }
    args.output.with_suffix(".json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
