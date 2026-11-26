# Flight plan ingestion: the as-planned mission and its as-flown correction

**Status: in progress (2026-11-23).** Steps 1–3 and 6–10 are implemented and have run; OCR (step 4)
and AI structuring (step 5) are implemented and wait for the GPU. See [§9](#9-execution-status-2026-11-23).
This guide describes how to turn the Apollo 15
Final Flight Plan into a page-referenced data structure for the full mission. It also covers
retiming the plan against the mission record to produce an as-flown timeline, and deriving a
high-level table of contents (TOC) like `../Apollo_11/MISSION_DATA/A11_TOC.csv`,
`../Apollo_13/MISSION_DATA/A13_TOC.csv` and `../Apollo17.org/MISSION_DATA/Mission TOC.csv`. It
extends [mission data ingestion](16_2026-11-23_mission_data_ingestion.md) and uses that guide's
conventions: snapshots, the raw folder and its manifest, OXR OCR, the GLM-5.3-Flash/HGX
classifier, review TSVs and reproducible builds. Where that guide already decides something,
this one does not repeat it.

**Three layers, kept separate.**

1. **Printed plan.** What the 6/21/71 Final edition says, with every item traced to a page and
   a region on that page.
2. **Annotations.** Hand-written additions on the scans, by David Woods. These are mostly
   updates read up during the flight. They are not part of the printed plan.
3. **As flown.** When each planned activity actually happened, or whether it happened at all,
   established from the transcripts and journals. Unplanned activities are added here.

The website and the TOC consume layer 3. Layer 1 is kept so that the interface can show the
original page next to the actual time.

## 1. Sources (checked 2026-11-23)

| Source                                                                                                      | What it is                                                                                                                                                   |     Pages | Notes                                                                                                                                                                                                                                                                                                            |
| ----------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------ | --------: | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| [ALSJ excerpt](https://apollojournals.org/alsj/a15/a15.fltplan.html) `a15.fltplan.html`                     | `a15fp001.jpg`–`a15fp099.jpg`, scanned by Frank O'Brien. Page revised 4 May 2025                                                                             |   98 JPGs | Covers 96:00–177:00 only. `a15fp008` does not exist. **Separate scan** from the AFJ copy, so it is an independent OCR witness for these pages. **The only source for summary pages 5-9 to 5-15** (`a15fp001`–`007`, Chapter 5 "Abbreviated Timeline", 12 hours per page with CSM and LM columns and REV markers) |
| [AFJ flight plan index](https://apollojournals.org/afj/ap15fj/fplanindex.html) `fplanindex.html`            | One GIF per page under `afj/ap15fj/fplan/`, for example `fplan/3-126.gif`, about 1100×850 px. Each entry has a page number and title                         | 464 links | Front matter (26), Chapter 1 (9), Chapter 2 (23) and Chapter 3 (406: `3-001`–`3-408`, with no `3-003` or `3-004`). Chapters 4–6 are listed without links. The titles are Woods's, and they contain errors: `3-095` is titled "098 hours" between 088 and 090. GIFs were modified 2025-10-22                      |
| [AFJ full PDF](https://apollojournals.org/afj/ap15fj/pdf/a15flightplan.pdf) `pdf/a15flightplan.pdf`         | 23.2 MB. Ghostscript 2003, modified 2025-10-22                                                                                                               |       466 | A cover note, then one image per page (142 portrait, 322 landscape), with **no text layer**. The cover note says the hand-written additions date from compiling the journal in 1998–2000, and largely reflect changes read up to the crew                                                                        |
| [Virtual AGC PDF](https://www.ibiblio.org/apollo/Documents/Apollo15FlightPlan.pdf) `Apollo15FlightPlan.pdf` | 47.1 MB, modified 2009-09-07. Same Ghostscript source, creation date 06/17/2003 19:07:34                                                                     |       466 | **The same scan as the AFJ PDF** (identical embedded image sizes on sampled pages), plus an OCR text layer on 464 pages. That layer is noisy (`6r1 /7T` for `6/21/71`, stray glyphs). Use it only as a weak third reading                                                                                        |
| AFJ chapter pages (already snapshotted)                                                                     | Links to `fplan/3-xxx.gif`, such as `3-352` ×7 and `3-089` ×4, plus **722 "page 3-xxx" mentions** and 138 "flight plan update" mentions in `journal_afj.tsv` |         — | They link transcript moments to flight-plan pages and say what was changed                                                                                                                                                                                                                                       |
| `transcript_tec.tsv` (already extracted)                                                                    | 199 rows mention "Flight Plan", for example "Flight Plan, page 384"                                                                                          |         — | Primary evidence of read-up changes                                                                                                                                                                                                                                                                              |

**Searches with no result:** Internet Archive (title and full-text "apollo 15 flight plan"), NTRS
(only the Preliminary Science Report and unrelated papers), and the JSC History Portal transcript
page. **Still to search:** UHCL JSC History Collection finding aids (MSC flight-plan series),
NARA Fort Worth, and the Sundance/Space Center Houston document scans. Keep looking for a
first-generation scan, but do not block the pipeline on it: the AFJ scan is good enough to start
(§8). A new scan would fill missing pages such as Chapters 4–6 and become another witness for
the existing ones. Record each search with its date in the source manifest
so it is not repeated.

**Companion documents** in `a15docs.html` supply detail or ground truth. Snapshot them through
the raw manifest like the transcripts:

- **Ground truth for actual times:** the Mission Report `ap15mr.pdf` (sequence-of-events table)
  and the 5-day report.
- **Planned EVA detail:** Final Lunar Surface Procedures `a15lsp.pdf`, LM Activation Checklist
  `pdf/a15lmact.pdf`, press kit `A15_PressKit.pdf` (planned timeline).
- **Clues to replanning:** the MOCR and SSR console logs (`pdf/a15-csm-*-log.pdf`).

**Open checks before extraction.**

- **The page-count mismatch.** The PDF has 465 scan pages and the index has 464 links. Find
  the extra page by image matching (§2), and confirm whether `3-003`/`3-004` exist (blank, or
  merged into `3-002`).
- **The LEP list of effective pages** (`i`–`v`) is the authoritative page inventory. Parse it
  for the page list and each page's revision date (most are 6/21/71, some 5/21/71). Report pages
  it lists that no source has.
- **Chapters 4–6.** Chapter 4 (Consumables), most of Chapter 5 and Chapter 6 (Alternate Mission
  Timelines) are not available. Record them as `missing` pages, not as an empty chapter.

## 2. Page identity and references

**Each page is identified by its printed page number**, normalised to `page_id`: `cover`,
`sigpage`, `acknow`, `i`…`xxv`, `1-001`…`1-009`, `2-001`…`2-023`, `3-001`…`3-408`,
`5-009`…`5-015`. The number in the printed footer is authoritative. The index title is only a
label. Every scan of a page is a **witness** of that page:

| Field                             | Example                                                                                                            |
| --------------------------------- | ------------------------------------------------------------------------------------------------------------------ |
| `afj_gif_url`, `afj_gif_sha256`   | `https://apollojournals.org/afj/ap15fj/fplan/3-126.gif`                                                            |
| `afj_pdf_page`                    | 1-based page in `a15flightplan.pdf`, giving `…/a15flightplan.pdf#page=N`                                           |
| `vagc_pdf_page`                   | page in the Virtual AGC PDF                                                                                        |
| `alsj_jpg_url`, `alsj_jpg_sha256` | `https://apollojournals.org/alsj/a15/a15fp019.jpg` (alternative scan)                                              |
| `hosted_{full,medium,thumbnail}`  | `A15/flightplan/{full,medium,thumbnail}/3-126.png` on `media.apolloinrealtime.org`                                 |
| `witness_choice`                  | which scan the interface shows by default, and why. The AFJ scan, unless the page is missing or damaged there (§8) |

**Matching the witnesses.**

1. Decode the GIF and the PDF's embedded image without resampling (PyMuPDF `extract_image`).
   Compare pixel hashes, falling back to a perceptual hash with a small Hamming tolerance.
   Equal pixels prove that the AFJ GIF and PDF page are the same scan.
2. Match the Virtual AGC pages in the same way.
3. The ALSJ JPGs are a different scan, so pair them by footer page number and then confirm by
   aligning the two images (ORB features and a homography). Store the homography, so a region
   on one scan can be shown on the other.
4. Stop on any page that has no footer reading, two candidate pages or an unmatched image.

**Region references.** Every extracted item keeps `page_id`, the witness it was read from, and a
bounding box in that witness's pixel coordinates. It also keeps the box in normalised page
coordinates (0–1), so the interface can highlight it on any hosted size. The interface can then
show "planned on page 3-126" and open the page with the PDI line highlighted.

## 3. Understanding a page

### 3.1 Page types

The page type is chosen by rules: the header title, the footer layout and the column rules
found by line detection. AI is used only when the rules disagree.

| `page_type`        | Pages                                               | Layout                                                                                                                  | What is extracted                                                                                                                                                                                                                                                          |
| ------------------ | --------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `front_matter`     | cover–xxv                                           | prose, lists                                                                                                            | LEP page inventory and dates. **The abbreviations (xi–xxii) and symbol nomenclature (xxv) become a glossary**, used by the extractor and given to the AI as context                                                                                                        |
| `notes`            | 1-001–1-009                                         | prose                                                                                                                   | Conventions. 1-009 "Synchronization of GET" is needed for §5.3                                                                                                                                                                                                             |
| `table`            | 2-001–2-023                                         | ruled tables                                                                                                            | One TSV per table, cell by cell with boxes. Several tables are already lists of events: 2-012 CSM Burn/Event Schedule (printed as Table 2-9), 2-014 LM Burn Schedule, 2-007 TV Schedule, 2-001 Suit Wearing, 2-008 dumps, 2-009 battery charges, 2-003–2-006 MSFN coverage |
| `csm_timeline`     | 3-xxx "CSM FLIGHT PLAN"                             | GET ruler on the left, activity column, MSFN/AOS bar, and a right-hand area for PADs, checklists and P30 maneuver forms | activities, event markers, PADs                                                                                                                                                                                                                                            |
| `lm_timeline`      | 3-106–3-286, even numbers, "LM FLIGHT PLAN"         | GET ruler and columns **MCC-H \| CDR \| LMP \| NOTES**                                                                  | per-crewman activities, MCC-H uplinks and calls, notes                                                                                                                                                                                                                     |
| `paired_timeline`  | 3-106–3-287                                         | an LM page (even) and a CSM page (odd) for the same hour                                                                | linked by `pair_page_id`. The CSM side is Worden alone                                                                                                                                                                                                                     |
| `procedure`        | burn tables, UV, film cycling, SIM door and similar | forms and checklists without a ruler                                                                                    | a structured procedure, attached to the activity that cites it                                                                                                                                                                                                             |
| `summary_timeline` | 5-9–5-15 (ALSJ)                                     | two 6-hour strips per page with CSM and LM columns, REV boxes and MSFN bars                                             | coarse activities, revolutions and the cross-check for Chapter 3                                                                                                                                                                                                           |
| `blank`            | 3-007, 3-016, …                                     | —                                                                                                                       | recorded so the page sequence stays complete                                                                                                                                                                                                                               |

### 3.2 Page anatomy (timeline pages)

These are illustrated by `3-126` (LM, 104:00–105:00) and `3-288` (CSM, 177:00–177:30).

- **Header:** the vehicle title, plus a **CDT wall-clock time** at the top of the ruler, such as
  `1634 CDT` at 104:00. Launch was at 1971-07-26 13:34:00 GMT, so 104:00 GET is 21:34 GMT, or
  16:34 CDT. On 3-288, 177:00 gives 1734 CDT. Use this as a check on each page's GET ruler.
- **The GET ruler** carries hour and ten-minute labels (`104:00`, `:10` … `105:00`) and minor
  ticks. Fit a linear y-pixel to GET map per page from the label positions and the tick spacing,
  and store it in `fp_ruler.tsv` with its residual. **Every activity without a printed time is
  given its planned GET from the ruler** (`get_basis = ruler`, with ± half a tick).
- **Parenthesised five-digit codes beside the ruler,** for example `(22112)`, or `(61102)` and
  `(11111)` with `V48 (11102)` on 3-288, are DAP configuration words, not times. Classify them
  as `code` from the glossary, and never parse them as GET.
- **Communication and lighting bars:** the `M S F N` coverage bar, AOS/LOS, and the sunrise and
  sunset symbols (⊙ and others defined on xxv). They become `fp_coverage.tsv` intervals.
- **Event lines:** horizontal rules across the columns carry boxed labels, such as `PDI`,
  `LM LUNAR TOUCHDOWN` and `LUNAR SURFACE CHECKLIST`. **These are the plan's key events.**
  Their time comes from the ruler, or from an adjacent printed value such as
  `GET: 104:40:57`.
- **Note boxes:** burn data such as `TIG: 104:28:54.8`, `BT: 12 MIN, 2.1 SEC`,
  `ΔVR: 6700 FT/SEC` and `ULLAGE: 4 JET 7.5 SEC`. Parse them into fields with units, and keep
  the raw text.
- **MCC-H column:** underlined headings (`UPLINK TO LM`, `UPDATE TO LM`) followed by the items
  sent, plus Go/No-Go and Stay/No-Stay calls.
- **Footer:** `MISSION | EDITION | DATE | TIME | DAY/REV | PAGE`, for example
  `APOLLO 15 | FINAL (7/26) | 6/21/71 | 104:00 - 105:00 | 5/14 | 3-126`. The footer time range
  bounds the ruler fit, and the DAY/REV field gives the flight day and revolution.

### 3.3 OCR

Follow the OCR section of the ingestion guide, with these differences:

- **The images are the source.** The PDFs only wrap images of about 1100 px. Extract the
  embedded image at native size instead of rendering at a DPI. On the scored sample, compare
  native size with 2× and 3× Lanczos upscaling before OXR.
- **Readings per page:** OXR on the AFJ scan (required); OXR on the ALSJ scan (98 pages, an
  independent scan); and the Virtual AGC text layer (weak). Store each reading with its engine,
  revision, scale and box, as for the transcripts.
- **A scored sample** is hand-keyed. It covers one page of each type, including 3-126, 3-288,
  2-012, a rest-period page, a procedure page, a 5-series summary and one LEP page. Score time
  digits, column assignment, event lines and dropped text. Times decide acceptance.
- **Deterministic geometry is used before OCR:** vertical column rules, horizontal event rules,
  boxes and the ruler ticks, found with OpenCV. Each OXR block is then assigned to a column and
  a time by geometry, not by the model.
- **Difficult regions,** such as small ruler labels or boxed values, are rerun as tight crops.
  Disagreements go to `review/fp_ocr.tsv` with the crop.

### 3.4 Structuring with GLM-5.3-Flash

The HGX endpoint, model alias `LLM-Alpha-512K` (GLM-5.3-Flash), with streaming, low reasoning
effort, temperature 0.1, strict flat `json_schema`, request budgets, atomic checkpoints, a
cache keyed by input, model and prompt hash, and `--backend hgx|strata`. All of it follows
§4 step 4 of the ingestion guide.

**The model is used on text only.** It receives the OCR blocks of a page, each with an ID,
column, box, ruler-derived GET and reading, plus the page type, the footer, the glossary entries
for the abbreviations on the page, and the last activities of the preceding page in the same
vehicle lane. Image input is not assumed. If a probe shows that the HGX alias accepts images,
evaluate it on the scored sample as an optional extra witness.

It returns one row per block:

- `block_id`
- `kind`: `activity`, `event_marker`, `uplink`, `call`, `pad_field`, `note`,
  `checklist_ref`, `procedure_step`, `code`, `annotation` or `noise`
- `category`: `sleep`, `eat`, `exercise`, `burn`, `navigation`, `photography`, `sim_bay`,
  `eva`, `lrv`, `alsep`, `systems`, `comm`, `tv`, `transfer` or `housekeeping`
- `activity_group`: the ID of the first block of the activity it belongs to
- `continues_from_previous_page`
- `expansion`: a glossary expansion for display, never used as the reading
- `confidence`

**Code validates every answer:**

- `block_id`s must exist and appear exactly once.
- No new text. Titles are built from block readings, as in §4 of the ingestion guide.
- **The model never supplies a time.** Times come only from printed values or the ruler.
- A group must be contiguous in its column.
- Rejected rows are retried singly and then go to `review/fp_structure.tsv`.

**Calibration:** measure category and grouping accuracy on the scored sample before a full run,
and set the automatic-acceptance threshold for each `kind`.

### 3.5 Hand-written annotations

The AFJ/PDF/Virtual AGC scan contains Woods's additions. For example, on 3-288 the LM JETTISON
box has `177 : 20 : 33` written into it, beside the printed `177:20:45`. Rules:

- **Detect them by appearance.** Handwriting differs in stroke style from the IBM typescript.
  Differencing against the ALSJ scan works where it exists. The model can confirm the label
  from text cues: a value inside an empty PAD box, a value that differs from the printed one.
  Mark them `origin = annotation`, never `printed`.
- **An annotation is evidence for layer 3, not layer 1.** It is usually a PAD value read up
  before the event, not a record of when it happened. The 177:20:33 jettison PAD was overtaken
  by the hatch-seal problem. The AFJ index to events gives the actual jettison at 179:30:17.
- **Show it to the reader, but label it.** The interface displays the AFJ scan as delivered,
  with any annotation regions marked as later additions. Spot checks show few annotations, so
  this is a per-region flag, not a reason to prefer another scan.

## 4. Data structure

Files are machine extractions under `MISSION_DATA/flight_plan/`, with decisions under
`MISSION_DATA/review/`. They are UTF-8 TSV and deterministic, so reruns are byte-identical.

| File                                                                                      | One row per                                  | Key columns                                                                                                                                                                                                                                                                                                                                         |
| ----------------------------------------------------------------------------------------- | -------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `fp_pages.tsv`                                                                            | page (including `missing` and `blank` pages) | `page_id`, `seq`, `chapter`, `phase` (index section, such as "Lunar Orbit/Surface Phase"), `index_title`, `printed_title`, `page_type`, `vehicle` (`CSM`, `LM`, `both` or `-`), `pair_page_id`, `get_start`, `get_end`, `flight_day`, `rev`, `edition`, `page_date`, `lep_date`, `cdt_top`, all witness fields from §2, `has_annotations`, `status` |
| `fp_ruler.tsv`                                                                            | ruler label                                  | `page_id`, `witness`, `y_px`, `label`, `get_seconds`, fit slope/intercept, residual                                                                                                                                                                                                                                                                 |
| `fp_blocks.tsv`                                                                           | OCR block                                    | `block_id` (`3-126:b017`), `page_id`, `witness`, `bbox_px`, `bbox_norm`, `column` (`MCC-H`, `CDR`, `LMP`, `NOTES`, `CSM`, `LM`, `PAD` or `ruler`), `origin` (`printed` or `annotation`), `text` (reviewed), `readings` (engine→text), `kind`, `get_planned`, `get_basis` (`printed`, `ruler` or `footer`), `get_tolerance_s`                        |
| `fp_activities.tsv`                                                                       | planned activity (layer 1)                   | `activity_id` (`A15-FP-1043000-LM-PDI`), `title`, `category`, `crew` (`CDR`, `LMP`, `CMP`, `MCC`), `vehicle`, `planned_start`, `planned_end`, `parent_id` (phase → activity → step), `block_ids`, `page_ids`, `procedure_page_ids`. A rest period across several pages is one activity                                                              |
| `fp_events.tsv`                                                                           | planned point event                          | `event_id`, `name` (TIG, touchdown, AOS, LOS, sunrise, sunset, rev start, TV start…), `planned_get`, `vehicle`, `source` (`3-126:b031`, or Table 2-9 row), parsed burn fields (TIG, BT, ΔV, ullage, HA/HP)                                                                                                                                          |
| `fp_coverage.tsv`                                                                         | interval                                     | `kind` (`msfn`, `daylight` or `rev`), `vehicle`, `start`, `end`, `page_id`                                                                                                                                                                                                                                                                          |
| `fp_tables/2-0NN.tsv`                                                                     | table cell                                   | table ID, `row`, `col`, `text`, `bbox`, `page_id`                                                                                                                                                                                                                                                                                                   |
| `fp_glossary.tsv`                                                                         | abbreviation or symbol                       | `term`, `expansion`, `page_id`                                                                                                                                                                                                                                                                                                                      |
| `fp_updates.tsv`                                                                          | read-up change (layer 2 evidence)            | `update_id`, `cited_page_id`, `cited_get`, `change_text` (from transcript spans), `utterance_event_ids`, `annotation_block_ids`, `kind` (`time_change`, `added`, `deleted`, `pad`)                                                                                                                                                                  |
| `fp_actual.tsv`                                                                           | planned or unplanned activity (layer 3)      | `activity_id`, `status`, `actual_start`, `actual_end`, `start_basis`, `end_basis`, `uncertainty_s`, `evidence` (utterance event IDs, AFJ index rows, Mission Report rows), `slip_s`, `review_state`                                                                                                                                                 |
| `review/fp_ocr.tsv`, `review/fp_structure.tsv`, `review/fp_actuals.tsv`, `review/toc.tsv` | decision                                     | stable key, choice, reviewer, date, note, methods that agreed (`rule`, `ocr2`, `ai`, `human`)                                                                                                                                                                                                                                                       |

An interface export `MISSION_DATA/indexes/flightPlanData.json` joins these into a hierarchy:
phases, then activities, then steps. Each node keeps its page references and boxes. Example:

```json
{
  "activity_id": "A15-FP-1042855-LM-PDI",
  "title": "PDI",
  "category": "burn",
  "vehicle": "LM",
  "planned": {"start": "104:28:54.8", "basis": "printed", "source": "3-126:b031"},
  "actual":  {"start": "104:30:09", "basis": "afj_index", "status": "done",
              "evidence": ["afj:12e-day5_landing_prep:event:…", "AS15_TEC-p…"]},
  "burn": {"bt_s": 722.1, "dv_fps": 6700, "ullage": "4 JET 7.5 SEC"},
  "pages": [{"page_id": "3-126", "witness": "afj_gif",
             "bbox_norm": [0.34, 0.47, 0.42, 0.50],
             "pdf": "a15flightplan.pdf#page=…", "image": "A15/flightplan/full/3-126.png"},
            {"page_id": "5-009", "witness": "alsj_jpg", "bbox_norm": […]}]
}
```

The actual value above is illustrative. Every actual value comes from §5. A CSV view
(`flightPlanData.csv`, `GET|activity_id|level|vehicle|title|planned_get|page_id`) can follow,
once AiRT2 has a loader. AiRT2 has no flight-plan contract yet, so propose this one alongside
its `docs-plan/04-data-and-content-strategy.md`.

**Hosted page images:** `A15/flightplan/{full,medium,thumbnail}/<page_id>.png`, converted from
the chosen witness. Use lossless PNG for `full`, because the scans are line art. The ALSJ
witnesses go under `A15/flightplan/alsj/…`. Hosting follows §3 of the ingestion guide. Hosting
these scans is approved: the flight plan is public NASA material, and the project owner
collaborates on the AFJ and ALSJ (§8). Credit the AFJ and ALSJ scans in the source manifest
and on the page viewer.

## 5. Correcting the plan to the mission as flown

### 5.1 Evidence, in order of authority

1. **The Mission Report sequence of events** (`ap15mr.pdf`). Precise times for burns,
   undocking, landing, EVAs, lift-off, docking, jettison, the subsatellite, TEI, the CSM EVA,
   entry and splashdown. It is the reference for these, but it lists few activities.
2. **AFJ "Index to events" tables.** 40 chapters, about 280 rows, each with a GET and a
   journal anchor, for example "Separation (undocking) 100:39:37" in `12c-day5_lm_undock.html`.
   `md_journals` already emits one `event` block per chapter. Parse the rows into
   `scraped_data/journal_event_index.tsv`.
3. **Utterance events** (`utterance_events.tsv`), which carry the printed TEC/CM/LM GET, and
   the journal commentary. These are the only evidence for most activities: eat, sleep and
   wake, exercise, photography, P52s, LiOH changes, TV.
4. **Flight-plan read-ups** in the transcripts and journals. There are 722 "page 3-xxx"
   references and 138 "flight plan update" mentions in `journal_afj.tsv`, and 199 TEC rows
   mention the Flight Plan. These record how the plan was changed: new times, deletions,
   additions.
5. **Woods's annotations** (§3.5). Supporting evidence only.
6. **Later, the stage 06 audio** with mission-time maps, as a `measured` basis
   ([mission timing](../archive/docs_retired_20261126/14_2026-11-22_mission_timing_plan.md)). Never pool it with the printed
   GET as an independent witness.

Examples from the snapshots so far (more come from the Mission Report):

| Event              | Planned                                         | Plan source            | Actual         | Actual source                                                                          |
| ------------------ | ----------------------------------------------- | ---------------------- | -------------- | -------------------------------------------------------------------------------------- |
| CSM/LM undocking   | 100:13:56.1                                     | Table 2-9 (page 2-012) | 100:39:37      | AFJ index, `12c-day5_lm_undock.html`. A loose umbilical plug delayed it                |
| PDI TIG            | 104:28:54.8                                     | 3-126 notes box        | Mission Report | —                                                                                      |
| LM lunar touchdown | 104:40:57                                       | 3-126                  | 104:42:29      | ALSJ anchor `1044229`                                                                  |
| LM jettison        | 177:20:45 (printed), PAD 177:20:33 (annotation) | 3-288, Table 2-9       | 179:30:17      | AFJ index, `18tunnel_leak_lm_jett.html`. Delayed a revolution by the hatch-seal checks |

### 5.2 Method

**Anchor events.** For every planned event and activity boundary in `fp_events.tsv` and
`fp_activities.tsv`, collect candidates from sources 1–3 within a search window around the
planned time: ±2 h by default, and the whole flight day for sleep, eat and EVA boundaries.
Match by deterministic rules first:

- exact or glossary-normalised names: `UNDOCK`, `SEP`, `PDI`, `TOUCHDOWN`, `LIFT-OFF`,
  `TPI`, `DOCKING`, `JETT`, `TEI`, `MCC-n`;
- burn report phrases: "ignition", "shutdown", "burn complete", residuals;
- known call patterns: "Go for PDI", "Contact light", "cabin depress", "hatch open", "good
  morning" wake-up calls, "good night".

**AI matching with GLM-5.3-Flash.** This is for what the rules do not settle. One request covers
one planned activity. It sends the activity (title, crew, planned interval, page excerpt and its
procedure steps), the read-up updates that cite its page, and a window of utterance events. Each
utterance is sent as its ID, GET, speaker and spoken text, with commentary as context-only rows.
A strict flat schema is returned:

- `utterance_id`
- `relation`: `start`, `end`, `in_progress`, `completed_report`, `rescheduled`, `cancelled`,
  `not_evidence`
- `confidence`
- `quote_span`: token offsets in that utterance

The rules of §4 step 4 of the ingestion guide apply: IDs must exist, the code computes the
times, starts must precede ends, results must fall within the window, budgets, checkpoints and
the cache. The same model, in a separate pass, turns read-up exchanges into `fp_updates.tsv`
rows. Each row gives the cited page and time and a change kind, and the quoted spans come only
from the transcript.

**Status for every planned activity:**

- `done`
- `done_shifted`
- `partial`
- `cancelled` (for example EVA-3 stations dropped for time; confirm the list from the ALSJ)
- `moved` (to another time)
- `unconfirmed` (no evidence found in the communications or journals). The interface still
  shows the activity at its planned or slip-estimated time, marked "unconfirmed" (§8). It never
  appears as a TOC time

Unplanned activities found in the record become new `fp_actual.tsv` rows with
`status = unplanned` and no page reference. Examples are the water leak and chlorination-port
repair, the broken range/range-rate meter glass, the undocking plug, the tunnel and hatch
checks and the CSM EVA-day changes. The AI proposes them from the commentary and the AFJ event
index, and a person confirms them.

**The slip map.** Between accepted anchors, model the actual minus planned offset as a
piecewise-constant or linear function along each vehicle lane (CSM and LM). Its breakpoints
fall at anchors and at known replanning moments, from read-ups and console logs. Activities
with no direct evidence get `actual_* = planned + slip`, with `basis = slip_interpolated` and an
uncertainty that grows with the distance to the nearest anchor. They are shown as estimated and
unconfirmed, and never used as TOC times. A slip that is not monotonic, or an activity that would overlap its
successor, goes to review.

**Review.** Each automatic decision is recorded once in `review/fp_actuals.tsv`, keyed by
`activity_id`, as in the editorial-span review. Automatic acceptance needs a rule match, or a
high-confidence AI match whose cited utterance contains the expected keyword within tolerance.
Everything else is queued as TSV, with the page crop path and the utterance text. A re-scrape
or a new model reuses earlier decisions.

### 5.3 Clock basis

- **GET basis.** Use the printed GET of the transcripts, normalised as in the ingestion guide.
  The launch was on time (09:34 EDT, 13:34:00 GMT), so the planned GET and the flown GET share
  their origin. Page 1-009 describes GET synchronisation. Read it, and check the transcripts for
  any announced GET or clock update. Do not assume there was none. `time_offsets.tsv` already
  shows a median journal-to-TEC difference of 0 on the chapters checked.
- **The CDT label on each page** stays a planned wall-clock value. Never use it as evidence of
  actual time.

## 6. The table of contents

**Format.** The legacy `GET|level|title` files are written as `MISSION_DATA/A15_TOC.csv`, and
AiRT2 `indexes/TOCData.csv` as `timeId|level|label` (§3 of the ingestion guide). AiRT2 accepts
levels 1 and 2 only. Apollo 17's level-3 rows and description column map onto level 2 and the
label, so Apollo 15 uses levels 1 and 2. Keep the optional description in the provenance TSV
`A15_TOC.tsv`, with `toc_id`, the source `activity_id` or event, `get_basis` and evidence.

**Level 1** follows the earlier missions. This is the backbone. The plan defines what exists,
and §5 supplies when it happened:

- countdown, lift-off, staging, Earth-orbit insertion
- TLI, transposition and docking, LM ejection
- each midcourse correction that was performed
- every **crew sleep start** and **wake-up**, as in A11, A13 and A17
- SIM door jettison, LOI, DOI, DOI trim
- LM activation, undocking, CSM circularisation, PDI and landing
- SEVA; EVA-1, -2 and -3 prep, egress and **stations**, at level 1 as in A17, with the A17-style
  description of what was done at each station kept in `A15_TOC.tsv`
- lunar lift-off, TPI, docking, LM jettison
- the plane change, subsatellite launch and TEI
- the CSM EVA, the press conference, the lunar eclipse
- CM/SM separation, entry, splashdown, recovery

**Level 2** holds notable items. These are planned activities that happened and are worth
navigating to: TV transmissions (Table 2-7 and the AFJ video index), light-flash experiments,
UV and corona photography, landmark tracking, LRV deploy, ALSEP deploy, the flag and the
Genesis Rock. They also include unplanned events from §5 and memorable moments from the AFJ
index to events and the journal commentary. The AI may propose level-2 rows with a short
quoted title, but a person chooses the final wording, as for `A13_TOC.csv`.

**Rules.**

- **A TOC time is an actual time.** It is the first utterance or event of the item. The
  basis must be the Mission Report, the AFJ index, an utterance event or `measured`, never
  `planned` or `slip_interpolated`. An item without an actual time is held back.
- Labels are plain text. Quotes are verbatim from the published utterance. No unit
  conversions are inserted.
- Rows are sorted by GET, with no duplicates within 30 s at the same level.
- **Related outputs from the same timeline:** `missionStagesData.csv` (Countdown, Launch,
  Earth Orbit, Translunar, Lunar Orbit, Lunar Surface, Transearth, Entry) and
  `crewStatusData.csv` (who is in which vehicle, docked or undocked, sleeping, EVA) follow the
  A17 AiRT2 files. They are built from the same accepted actual intervals.

**The first output is a draft for review.** `toc` writes `A15_TOC.csv` and `A15_TOC.tsv` in the
A17 shape, at the level-1 depth above, together with `review/toc.tsv` listing each row's
evidence and alternatives. The project owner reviews the draft, and the recorded edits are
reapplied on every rebuild.

This replaces the seed in step 9 of the ingestion guide. Chapter titles and the AFJ index to
events remain inputs, and the flight-plan structure supplies the complete skeleton.

## 7. Steps and commands

These are the steps of `scripts/flight_plan/flight_plan.py`, implemented in
`scripts/flight_plan/md_flightplan.py` and registered in `scripts/layout.json`. Run them with `uv run python …`. Each
step reads only its predecessors' outputs, and reruns on unchanged inputs are byte-identical.

1. **`flight_plan.py snapshot`**
   - Fetch `fplanindex.html`, `a15.fltplan.html` and `a15docs.html` into `MISSION_DATA/journals/`.
   - Download the 464 GIFs, 98 ALSJ JPGs and both PDFs into
     `N:\Projects\Apollo\Apollo 15 Project\original_docs\flight_plan\{afj_gif,alsj_jpg,pdf}\`,
     recording them in `raw_manifest.tsv` (kind `flight_plan`).
   - Throttle requests. A changed upstream file is kept under a dated name.
2. **`flight_plan.py pages`**
   - Do the witness matching (§2).
   - Parse the LEP and index into `fp_pages.tsv`.
   - Resolve the page-count mismatch and list the missing pages.
3. **`flight_plan.py geometry`**
   - Find rules, boxes, columns and ruler ticks.
   - Classify the page type.
   - Write the ruler fit and the CDT check.
4. **`flight_plan.py ocr [--target all|pao|flight_plan]`**
   - **PAO pages first.** OXR reads the 1,238 PAO-edition pages first. They are the optional third
     reading for `sg_transcript.py sg pao` ([ingestion guide](16_2026-11-23_mission_data_ingestion.md),
     "Space-to-ground reels"), so rerun that afterwards.
   - Run OXR on the selected witnesses.
   - Import the Virtual AGC layer.
   - Combine the readings. Disagreements go to `review/fp_ocr.tsv`.
   - **GPU scheduling.** A run during 06 is allowed only when OXR is started with the capped
     override `scripts/flight_plan/compose.oxr-shared-gpu.yaml` (30% of VRAM), or with `--force`.
5. **`flight_plan.py structure [--backend hgx|strata] [--budget N]`**
   - Do the AI structuring (§3.4), with validation.
   - Write `fp_blocks`, `fp_activities`, `fp_events`, `fp_coverage`, `fp_tables` and
     `fp_glossary`.
   - Annotation detection (§3.5) happens here.
6. **`flight_plan.py updates`**
   - Extract the read-ups from the transcripts and journals, giving `fp_updates.tsv`.
7. **`flight_plan.py actual [--backend …]`**
   - Parse the AFJ event index.
   - Match against the Mission Report table and the AI evidence.
   - Fit the slip map. Write `fp_actual.tsv` and the review queue.
8. **`flight_plan.py media`**
   - Write the hosted page images (§4).
9. **`flight_plan.py toc`**
   - Write `A15_TOC.csv` and `A15_TOC.tsv`, and the `missionStagesData` and `crewStatusData`
     drafts from the accepted actual timeline plus `review/toc.tsv`.
10. **`flight_plan.py validate`**
    - **Pages:** every LEP page is present or recorded as missing, each page has one witness
      match, and the footer range contains every block GET on its page.
    - **Times:** the ruler residual is under one tick, and the CDT label agrees with
      GET + 13:34 GMT − 5 h.
    - **Continuity:** each lane ends one page where the next begins, and Chapter 3 events agree
      with Table 2-9 and the 5-series summary within the tolerance.
    - **Origin:** no annotation block has `origin = printed`.
    - **As flown:** no TOC row has a planned or interpolated basis, actual intervals do not
      overlap within a crew member's lane, and the GLM outputs reference existing IDs only.

**Tests** in `tests/`:

- golden pages 3-126, 3-288, 2-012 and 5-009, covering blocks, ruler, events and the burn box;
- the 3-288 annotation case;
- AI-output validators fed bad IDs, invented text and supplied times, which must be rejected;
- slip-map interpolation and monotonic checks;
- TOC release checks.

## 8. Decisions (2026-11-23)

1. **Default witness:** the AFJ scan, wherever it has the page in good condition. Where it is
   missing or damaged, use the next available source: the ALSJ scan (including summary pages
   5-9 to 5-15), then the Virtual AGC copy, then any first-generation scan found later. Spot
   checks show few annotations on the AFJ copy, so they are flagged per region (§3.5).
2. **Hosting:** host the AFJ and ALSJ scans on `media.apolloinrealtime.org`. The flight plan is
   public NASA material, and the project owner collaborates on the AFJ and ALSJ (§4).
3. **First-generation scan:** search for one (UHCL, NARA and others in §1), without blocking
   the pipeline. Start from the AFJ scan.
4. **TOC depth:** follow A17, with the EVA stations and every sleep and wake-up at level 1.
   Generate a draft for the project owner to review (§6).
5. **Activities without evidence:** the as-flown study of the transcripts decides each one.
   Any activity that cannot be found in the communications is still shown, at its planned or
   estimated time, marked "unconfirmed" (§5.2). It never supplies a TOC time.

## 9. Execution status (2026-11-23)

Code: `scripts/flight_plan/md_flightplan.py` (CLI and snapshot) with `md_fp_pages`, `md_fp_geometry`,
`md_fp_ocr`, `md_fp_structure`, `md_fp_actual`, `md_fp_media`, `md_fp_export` and `md_fp_assemble`
(which rebuilds `fp_pages.tsv` from every step's tables), `md_toc` and `md_validate`. Tests:
`tests/test_flight_plan.py`. Run `uv run python scripts/run.py sg_transcript.py …` first, then
`uv run python scripts/run.py flight_plan.py <step>` through `toc` and `validate`. Reruns on unchanged inputs are byte-identical (checked).

**Where things live.** Committed: `MISSION_DATA/flight_plan/fp_*.tsv`, `MISSION_DATA/A15_TOC.{csv,tsv}`,
`MISSION_DATA/indexes/{TOCData,missionStagesData,crewStatusData}.csv`, `indexes/flightPlanData.json`,
`MISSION_DATA/review/{toc,mission_report_soe}.tsv`. Not committed (`.local/mission_data/`): line and box
geometry `flight_plan/fp_rules.tsv`, OXR responses `flight_plan/ocr/`, crops, AI cache and diagnostics,
work queues `review/fp_*_queue.tsv`, `toc_held.tsv`, `toc_dropped.tsv`, `validate.tsv`.

| Step        | State                                   | Result                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                       |
| ----------- | --------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1 snapshot  | done                                    | `fplanindex.html`, `a15.fltplan.html` in the journal snapshots; 464 GIFs, 98 JPGs, both PDFs and 7 companion PDFs (Mission Report, Mission Operations Report, LSP, LM activation checklist, press kit, two console logs) in the raw folder and `raw_manifest.tsv`. `a15-csm-g+n-ssr-log{,-cleaned}.pdf` return 404 upstream. Searches are in `fp_searches.tsv`                                                                                                                                                                                                                                                                               |
| 2 pages     | done                                    | 393 AFJ PDF pages are pixel-identical to their GIF and 71 match by perceptual hash. The PDF stores most images bottom-up, so matching tries all eight orientations. **Page count resolved** (`fp_reconciliation.tsv`): PDF page 1 is the cover note, page 48 is an empty page, 3-302 is scanned twice (pages 359 and 360), and the PDF lacks 2-020 (the GIF has it). The Virtual AGC PDF has the same layout. All 98 ALSJ JPGs align by homography. The LEP (pages iv–v, checked on the scan) lists 4-1, 5-1…5-25 and 6-1. **Missing pages:** vi, x, 3-003, 3-004, 4-001, 5-001…5-008, 5-016…5-025 and 6-001 (24). 5-009…5-015 are ALSJ-only |
| 3 geometry  | done (first pass)                       | 464 pages typed: 285 CSM and 92 LM timelines, 23 procedures, 23 tables, 9 notes, 26 front matter, 6 blank. Timeline pages have one or more **strips** (3-143 has two half-hour rulers), and the rule's ticks can be on either side of it. 420 of 435 strips have a ruler fit from the Virtual AGC text layer. The CDT check agrees on 138 of 144 pages read, and the 6 mismatches are text-layer misreadings. These fits are replaced by OXR in step 4                                                                                                                                                                                       |
| 4 ocr       | **blocked**                             | A stage-06 `run --all-tapes` holds the GPU (ETA about 33 h on 2026-11-23), and the OXR container `lsc-oxr` is stopped. `flight_plan.py ocr` refuses to start while 06 runs. Afterwards: start OXR (`../lsc/scripts/setup_oxr.ps1`), score the sample at `--scale 1`, `2` and `3`, then run all pages                                                                                                                                                                                                                                                                                                                                       |
| 5 structure | implemented, waits for step 4           | Rules plus HGX GLM-5.3-Flash with the strict schema and validators. A smoke run on synthetic blocks for 3-126 completed through HGX (6 s/page). The model's `expansion` must be a glossary expansion, and a printed time more than 30 min from the ruler (for example an OCR `704:40:57`) is queued and never used                                                                                                                                                                                                                                                                                                                           |
| 6 updates   | done (rules)                            | 739 page citations from the TEC/CM/LM transcripts and the journals, 350 of them AFJ links to `fplan/*.gif`. `page 384` with the dash lost in the 1971 OCR is resolved to 3-084 or 3-384 by time. Change kinds are rule-based, and 357 are still `unknown` for the AI pass                                                                                                                                                                                                                                                                                                                                                                    |
| 7 actual    | anchors done; matching waits for step 5 | `fp_anchors.tsv`: 46 Mission Report rows, 50 AFJ index rows, 49 utterance rows (ALSJ chapter starts, sleep and wake per crew lane, countdown). **Mission Report Table 3-I is an image with no text layer.** Its 49 rows were keyed from the page image into `review/mission_report_soe.tsv` and marked `needs_check`. OXR becomes the second reading. `fp_actual.tsv` stays empty until activities exist                                                                                                                                                                                                                                     |
| 8 media     | done                                    | 1,707 PNGs under `media.apolloinrealtime.org/A15/flightplan/` (`full` native, `medium` 640 px, `thumbnail` 120 px), with ALSJ scans under `alsj/`. Recorded in `fp_media.tsv`, credits in `fp_credits.tsv`                                                                                                                                                                                                                                                                                                                                                                                                                                   |
| 9 toc       | draft                                   | `A15_TOC.csv`: 134 rows (82 level 1), all with actual bases. Held back: LM activation and recovery. For review in `MISSION_DATA/review/toc.tsv`, where decision columns persist across rebuilds                                                                                                                                                                                                                                                                                                                                                                                                                                              |
| 10 validate | running                                 | Passes on pages, LEP, ruler residuals and TOC rules. CDT has 6 warnings. Block, origin, continuity and overlap checks are `pending` until steps 4–5                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          |

**Next, in order:** (1) check `review/mission_report_soe.tsv` against the scan; (2) after 06, OCR the
scored sample and choose the scale; (3) full OCR, structure with `--backend hgx`, `updates --budget`,
`actual`, `toc`, `validate`; (4) review the TOC draft.
