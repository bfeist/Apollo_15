# Mission data ingestion: transcripts, commentary and photographs

**Status: plan.** No ingestion code exists yet. This guide describes how to bring the Apollo 15
mission record into the form used by the earlier Apollo in Real Time missions. The audio pipeline
(stages 01–08) is unaffected. The only shared work is the transcript index used for retiming
([mission timing](../archive/docs_retired_20261126/14_2026-11-22_mission_timing_plan.md)). This guide extends that index rather than
starting a second one.

**The division of labour.** This repository covers Apollo 15 mission data only. The 30-track
audio stages are part of it, and the website is a separate repository, `../AiRT2`.

- **This repository holds text.** It contains the journal HTML snapshots, the extracted and
  reviewed tables, the indexes and GET timing for every asset, and checksum manifests for every
  raw file kept outside Git.
- **Raw downloads go to the mission raw folder** `N:\Projects\Apollo\Apollo 15 Project`, never
  to Git. These are the transcript PDFs, photo indexes, IM-LDI scans and SIM-bay PNGs.
- **The Flickr Hasselblad masters stay where they are,** read only, in
  `N:\Projects\Apollo\Mission Data\Apollo Images from flickr\AS15`.
- **Assets are converted for hosting** into `N:\Projects\Apollo\media.apolloinrealtime.org\A15`.
  They are never copied into Git.

**The transcript goal is a verbatim record of the audio.** The Apollo Flight Journal (AFJ), the
Apollo Lunar Surface Journal (ALSJ) and the 1971 NASA transcripts correct and fill one another.
The published wording must contain only what was said. Unit conversions, expansions, inserted
words, stage directions and other editorial insertions are removed automatically. Each decision is
recorded once, so a re-scrape never needs another manual pass like the ones done for Apollo 11, 13
and 17 (§4).

## 1. Source inventory (checked 2026-11-23)

### Apollo Flight Journal — `https://apollojournals.org/afj/ap15fj/`

The [index](https://apollojournals.org/afj/ap15fj/index.html) links **49 chapter pages**, from
`01launch_to_earth_orbit.html` to `25b-day13_entry-splashdown.html`. Several days are split into
lettered parts. The index also links `a15images.html` (photography index by magazine and camera),
`a15docs.html` (flight plan, checklists and press kit), `a15videoindex.html` and `a15summary.html`.

The pages use the "new style" AFJ markup that the Apollo 13 scrapers handled:

```html
<div class="cc">
  <a name="0740951"></a><b>074:09:51 Allen:</b> And, 15, just out of interest,
  ...
</div>
<div class="onboard">...</div>
<!-- onboard recorder (DSE) -->
<div class="pao">This is Apollo Control at 65 hours. ...</div>
<!-- often untimed -->
<div class="comment">
  Editorial commentary ... <span class="attribution">1998 correspondence:</span>
</div>
<div class="photo_wrapper">
  <div class="photo">
    ...
    <div class="caption">AS15-xx-xxxxx - caption</div>
  </div>
</div>
<div class="video">
  <iframe src="https://www.youtube.com/embed/...?start=149"></iframe>
</div>
<div class="indented">
  <audio><source src="audio/a15_t-0021600.mp3" /></audio>
</div>
```

- Anchor names encode GET as `HHHMMSS`, with a leading `-` before launch (`-0000017`). Use the
  anchor when present and the bold `HHH:MM:SS` stamp otherwise.
- Speakers on the sampled pages include the crew (Scott, Irwin, Worden), the CapComs (Allen,
  Fullerton, Henize, Parker, Brand) and also `Goddard` and `Unknown`.
- PAO paragraphs often carry no stamp. Record the time as `inherited` from the preceding stamp.
  Such times are never used as timing anchors.
- **Editorial insertions are in square brackets** (§4).

### Apollo Lunar Surface Journal — `https://apollojournals.org/alsj/a15/`

The [mission page](https://apollojournals.org/alsj/a15/a15.html) links about **50 journal
chapters**, from `a15.landing.html` through the stand-up EVA and three EVAs to `a15.launch.html`.
Take the chapter list from the page itself rather than typing it. The page also links the
technical debrief `a15tecdbrf.html`, the photo index `a15.photidx.pdf`, the image library
`images15.html`, the surface checklist and the geology voice transcript.

The ALSJ markup is older and line-oriented:

```html
<a name="1452401"></a><b>145:24:01</b> Allen: And, Dave, give us a call as you
come up toward Spur...
<p>[Scott, from the 1996 review: "..."]</p>
```

- An utterance is `<b>GET</b> Name: text` and ends at `<p>`. Commentary is the bracketed text
  between utterances.
- **Within utterances, the ALSJ marks editorial insertions with parentheses**, for example
  `(Pause)`, `(To Jim)`, `(Reading)`, `(LM) O2 hoses` and `Suit Isolation (Valve)`. Parentheses can
  also be part of what was said: `CB(16)` is read aloud as a circuit-breaker designation (§4).
- Pages link audio clips (`a15a1452401.mp3`), images (`AS15-xx-xxxxx`, pans, LPI and ALSJ JPGs) and
  videos. Keep them as structured media references.

### Original NASA transcripts — `https://apollojournals.org/alsj/a15/a15trans.html`

| Edition                                  | File           |              Size | Notes                                                                                                                                   |
| ---------------------------------------- | -------------- | ----------------: | --------------------------------------------------------------------------------------------------------------------------------------- |
| Technical air-to-ground (TEC), MSC-04558 | `AS15_TEC.PDF` | 38.2 MB, 1,478 pp | Timing edition. Pages 784–910 are already indexed by `research/mission_timing/index_technical_transcript.py`. SHA-256 `c69e4881…f79849` |
| Public affairs (PAO)                     | `AS15_PAO.PDF` |           35.6 MB | Mission commentary. Text layer not yet checked                                                                                          |
| Command module onboard (CM)              | `AS15_CM.PDF`  |            8.9 MB | Onboard recorder. Text layer not yet checked                                                                                            |
| Lunar module onboard (LM)                | `AS15_LM.PDF`  |            3.3 MB | Onboard recorder. Text layer not yet checked                                                                                            |

The TEC prints `DD HH MM SS` elapsed time. The extractor keeps both that reading and the
normalized GET. The TEC introduction warns that clock updates can require deltas. Check whether
any span of the Apollo 15 transcripts needs a constant shift, like Apollo 17's +2:40:00
(`enactTECTimeChange.py`). Do not assume one. The 1971 transcripts contain no unit
conversions. They mark unintelligible speech as `(garble)` and include some stage notes.

### Photography

| Collection                                       |                                                           Frames | Raw source                                                                                                                                                                                                                                                                         | GET source                                                                                                                                                               |
| ------------------------------------------------ | ---------------------------------------------------------------: | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Hasselblad 70 mm, handheld (orbital and surface) | 2,395 JPGs, 9.6 GB, plus 263 records for magazines 83, 94 and 98 | `N:\…\Apollo Images from flickr\AS15`: full-resolution 4175² scans of magazines 81, 82, 84–93, 95–97 and 99. Magazines 83, 94 and 98 come from ALSJ high-resolution JPGs where they exist, otherwise from cropped [IM-LDI](https://tothemoon.im-ldi.com/gallery/Apollo/15/6) scans | Journal citations, AFJ `a15images.html`, NASA `a15.photidx.pdf`, interpolation within a magazine                                                                         |
| SIM bay metric (mapping) camera, `AS15-M-nnnn`   |                                                            3,376 | [ASU/IM-LDI Apollo scans](https://data.lroc.im-ldi.com/apollo/browse?camera=M&mission=15) (formerly `apollo.sese.asu.edu`, used by the Apollo 16 script)                                                                                                                           | Per-frame **UTC start time** on the view page, plus revolution, centre and corner coordinates, altitude and sun elevation                                                |
| SIM bay panoramic camera, `AS15-P-nnnn`          |                                                            1,529 | [ASU/IM-LDI pan scans](https://data.lroc.im-ldi.com/apollo/browse?camera=P&mission=15)                                                                                                                                                                                             | Revolution, forward/aft tilt and features on the view page. Times come from the [SIM photo index](https://apollo.sese.asu.edu/SUPPORT_DATA/ap15_sim_index.pdf) (19.9 MB) |

- **Fill magazines 83, 94 and 98.** The Flickr folder lacks them. The IM-LDI Apollo 15 gallery
  (pages `/gallery/Apollo/15/6` and `/7`) has every frame: 26, 133 and 104 records, including
  lettered variants such as `AS15-83-11218A`/`B` and the `AS15-98-chart` magazine chart. For the
  other magazines, its frame counts match the Flickr folder (for example 178 in magazine 81 and
  77 in magazine 91), so only these three need filling. Magazine 94 is the 250 mm lunar-orbit
  and transearth magazine. Magazine 98 is a lunar-orbit magazine. The AFJ index lists no
  magazine 83 frames.
  - **Source preference for each frame:**
    1. **ALSJ high-resolution JPG, where it exists.** ALSJ crops to the exposed frame and
       corrects the tone. IM-LDI keeps the film border, sprocket holes and handwritten labels
       and has flat contrast. Compare AS15-94-12741: ALSJ's `AS15-94-12741HR.jpg` is
       2301×2280 px. IM-LDI shows the whole film strip. ALSJ coverage of these magazines is
       thin. `images15.html` has HR versions of only four frames (AS15-94-12741, `-12810`,
       `-12823` and `-12836`) and none for 83 or 98. Also search every scraped ALSJ and AFJ
       chapter for further `AS15-83/94/98` links and take the largest version found. Do not
       upscale these images. If the ALSJ JPG is smaller than 4175 px, `full` is its native
       size.
    2. **Otherwise IM-LDI, always cropped to the image and tone corrected.** An IM-LDI scan is
       never published as delivered. Every hosted size (`full`, `medium` and `thumbnail`) is
       made from the cropped, tone-corrected version.
       - **Crop to the image.** Detect the exposed image rectangle inside the dark film base and
         crop to it. Remove the film border, sprocket holes, edge numbers and handwritten
         labels. Trim a small inset so no film edge remains, and correct any slight rotation of
         the frame in the scan. The result is square or nearly square, like the ALSJ and Flickr
         frames.
       - **Tone correct.** The 16-bit scans are flat. Measure levels inside the crop only, so the
         dark film base does not skew them. Set black and white points from low and high
         percentiles of the cropped image, then apply a gamma or contrast curve. Convert to
         8 bits only after the adjustment. Calibrate the curve on the ALSJ frames that exist in
         both sources (AS15-94-12741 and the other three) and on neighbouring Flickr frames from
         the same camera and film. Use one setting per magazine, so frames in a sequence match.
         Clip only a small, recorded fraction of pixels.
       - **Record.** Store the crop box, rotation, black and white points, curve and clipped
         fraction for each frame in the photo index, so every published image can be
         regenerated exactly from the raw scan.
       - **Checking the crop.** Use the ALSJ JPGs and the AFJ `photos/s/` thumbnails (385 px,
         77 of the magazine 94 frames and 26 of magazine 98) as references. Align each
         reference to its auto-crop and flag a frame when the crop boxes or aspect ratios
         disagree.
       - **Review.** Frames without a reference go to the review queue with a contact sheet.
         Charts and lettered variants are reviewed by hand.
  - **Record format.** Each gallery page embeds one JSON record per frame (HTML-escaped). A
    record holds `image_id`, `magazine`, `magazine_alt` (for example `S`), `camera_name`,
    `lens`, `film_type`, often-empty `get_time`/`gmt_time`/`subject` fields, and image paths
    under `https://tothemoon.im-ldi.com/data_a70/AS15/`:
    - `extra/<id>.{thumb,small,med}.png`
    - `processed/<id>.png`
    - `raw/<id>.tif`
  - **Download.** Scrape the records from the gallery pages rather than guessing URLs. Download
    `processed/<id>.png`, the extreme-resolution master (about 226 MB per frame, so roughly
    59 GB for 263 records), into `N:\Projects\Apollo\Apollo 15 Project\images\im-ldi\AS15\`.
    Download ALSJ JPGs into `images\alsj\AS15\`. Record each URL, size and SHA-256 in the Git
    manifest. The `med` PNG is 16-bit and about 3587×3034 px, including the film border.
  - **Convert.** For an IM-LDI source, write the cropped and tone-corrected image at 8 bits,
    resized to 4175 px on the long side for `full` when the source is larger.
  - **Other magazines.** Keep the IM-LDI record for every Apollo 15 frame as metadata, for
    example lens and film. A Flickr master is not replaced unless a frame is missing or
    damaged.
- Irregular Hasselblad names include variant suffixes in `AS15-82-11217a`, `AS15-92-12405a` and
  `AS15-95-12877A`–`12879A`, with both cases. There are also five `as15-NN-chart.jpg` magazine
  charts. Normalize IDs in the catalog (upper-case, with the variant kept as a separate field).
  Never rename source files.
- Metric times are UTC. Convert them with NASA range zero 1971-07-26 13:34:00 GMT, the same epoch
  as the timing plan. For example, AS15-M-1000 at 1971-07-31T23:55:34 is GET 130:21:34, during
  revolution 27.
- **SIM-bay downloads.** The full metric and pan scans are very large (0.5–1.2 GB raw, and
  6.5 GB JPEG 2000 per pan frame). Download only the published PNGs into the raw folder:

  | Camera    | PNG         | Size          |
  | --------- | ----------- | ------------- |
  | Metric    | `_MED`      | 4048² px      |
  | Metric    | `_SML`      | 1012² px      |
  | Panoramic | `_FULL_MED` | 14963×1616 px |
  | Panoramic | `_FULL_SML` | 3741×404 px   |

  Each PNG is about 14 MB, roughly 70 GB for both cameras. Keep the source URL and checksum in
  the manifest.

- **Film is out of scope.** 16 mm DAC and other motion-picture film are handled separately.

## 2. What the earlier missions did

Use these as references for formats and pitfalls, not as code to copy. Most are one-off scripts
with hard-coded paths.

| Task                      | Apollo 11                                                                                     | Apollo 13                                          | Apollo 16                                                                  | Apollo 17 (`Apollo17.org`)                                                     |
| ------------------------- | --------------------------------------------------------------------------------------------- | -------------------------------------------------- | -------------------------------------------------------------------------- | ------------------------------------------------------------------------------ |
| Transcript OCR → CSV      | `cleanTECcsv.py`, `cleanTECPhase2csv.py`, `clean_PAO.py`, `cleanOnboardcsv.py`                | `A13_TEC_raw.txt`                                  | —                                                                          | ABBYY OCR, `cleanTECcsv.py` (digit substitution, `MCC`→`CC`), `cleanPAOcsv.py` |
| Clock correction          | `convert_zulu_to_get.py`, `bulk_adjust_utterance_times.py`                                    | —                                                  | —                                                                          | `enactTECTimeChange*.py`                                                       |
| Merge TEC + PAO + onboard | `mergeTECPAOOnboard.py`                                                                       | hand-merged `A13_utterances.csv`                   | —                                                                          | hand-merged and listened `A17 master TEC and PAO utterances.csv`               |
| AFJ utterances            | —                                                                                             | `scrape_afj_utterances.py` (new-style)             | `scrape_afj_utterances.py` (old-style)                                     | —                                                                              |
| AFJ and ALSJ commentary   | `scrape_afj_commentary_*style.py`, `scrape_alsj_commentary.py`, `merge_scraped_commentary.py` | `scrape_afj_commentary_newstyle.py`                | `scrape_afj_commentary.py`, `scrape_alsj_*.py`                             | `scrape_*_commentary*.py` with `alsjCache/`                                    |
| Photo metadata            | `scrape_afj_photo_index.py`, `scrape_alsj_photoindex.py`                                      | `scrape_afj_photos_newstyle.py`, `A13_photos.xlsx` | `a16_photos_{flight,mapping,panoramic}.csv` from the SIM/70 mm indexes     | `photos.xlsx/.csv`, `mergeImageTablesSqlite.py`                                |
| Image conversion          | `move_used_images.py`                                                                         | `scrape_lpi_images.py`, `resize_lpi_images.py`     | `scrape_lpi_images.py`, `scrape_lpi_mapping_images.py` (ASU PNG → JPG q80) | `flickr_scraper.py`                                                            |
| Validation                | `errorcheckUtterancesDatafile.py`                                                             | `error check/`                                     | —                                                                          | `errorcheck{Utterances,Photos}Datafile.py`                                     |
| Website build             | `CreateAllHTML.py`                                                                            | `CreateAllHTML13.py`                               | `CreateAllHTML16.py`                                                       | `createAllHTML.py`                                                             |

The website build reads pipe-delimited files without headers:

```text
A13_utterances.csv      GET|type|who|text            type: T tech, P PAO, O onboard, C capcom; blank = section row
A11_merged_commentary   GET|source|who|text           source: AFJ | ALSJ | other
A13_photos.csv          GET|filename|alsj_file|media_file|description|source   ('skip' = unplaced)
a16_photos_mapping.csv  photo|GET|alt|…|rev|feature|camera|film
A13_TOC.csv             GET|level|title
```

The build writes `indexes/utteranceData.csv` and the like, with colons removed from the GET.

**The website is now `../AiRT2`.** It reads `public/{N}/indexes/*.csv`: `utteranceData`,
`commentaryData`, `photoData`, `TOCData`, `missionStagesData`, `crewStatusData`, `orbitData`,
`telemetryData`, `videoSegmentData` and `videoURLData`. Its loaders in `src/data/` normalize
per-mission schema differences, and its media URLs use `https://media.apolloinrealtime.org/A{N}`
(`docs-plan/04-data-and-content-strategy.md`). AiRT2 states that a replacement pipeline needs a
verified output contract compared against all three current missions. The Apollo 15 index files
in §3 are written to that contract, so an Apollo 15 entry in AiRT2 needs only a mission config
and a copy of the files. The legacy Apollo 11, 13 and 17 data and scripts are also preserved
there under `mission-data/` and `pipeline/`.

**Lessons from the legacy code:**

- Parse the document tree, not lines. Line-based regular expressions with hand-tracked multiline
  state dropped or merged blocks when markup wrapped. Several Apollo 16 chapter names even carry
  trailing spaces.
- Never strip to ASCII. Apollo 13's `.encode('ascii', 'ignore')` deleted `½`, `°`, curly quotes
  and accented names.
- Parse only from cached snapshots, so a parse can be reproduced after the journals change. Old
  `history.nasa.gov/afj` and `hq.nasa.gov/alsj` URLs now redirect to `apollojournals.org`.
- Map speakers from a census. The Apollo 16 scraper truncated unknown names with `who[0:3]`.
- `Apollo17.org/_Website/python/flickr_scraper.py` commits a Flickr API key. Read keys from the
  environment if one is ever needed.
- **Manual bracket cleanup was incomplete.** The published `A13_utterances.csv` still contains 174
  lines with brackets, including AFJ unit conversions such as `214,369 nautical miles [397,011 km]`.
  `A17 master TEC and PAO utterances.csv` has 44 such lines and `A11_merged_utterances.csv` has 3.
  This is the motivation for §4.

## 3. Repository and media layout

Mission text follows the earlier repos' `MISSION_DATA/` convention. The existing timing catalog
stays in `catalog/`.

| Location                                                                                 | Contents                                                                                                                                                                              |
| ---------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `MISSION_DATA/journals/ap15fj/`                                                          | AFJ chapter and index HTML as fetched. `manifest.tsv` holds the URL, final URL, fetch date and SHA-256                                                                                |
| `MISSION_DATA/journals/alsj15/`                                                          | ALSJ chapter and index HTML, with the same manifest                                                                                                                                   |
| `MISSION_DATA/raw_manifest.tsv`                                                          | One row per file in the raw folder (PDFs, IM-LDI scans, SIM-bay PNGs): relative path, source URL, fetch date, size, SHA-256, page or pixel count. The files themselves are not in Git |
| `MISSION_DATA/scraped_data/`                                                             | Machine extractions, one row per source block: `transcript_{tec,pao,cm,lm}.tsv`, `journal_{afj,alsj}.tsv`, `photos_{hasselblad,metric,panoramic}_source.tsv`                          |
| `MISSION_DATA/review/`                                                                   | Recorded decisions: `editorial_spans.tsv`, `wording.tsv`, `speakers.tsv` and `photo_times.tsv` (§4, §5)                                                                               |
| `MISSION_DATA/A15_utterances.csv`, `A15_commentary.csv`, `A15_photos.csv`, `A15_TOC.csv` | Generated master files in the legacy pipe format                                                                                                                                      |
| `MISSION_DATA/indexes/`                                                                  | Generated AiRT2 index files (`utteranceData.csv`, `commentaryData.csv`, `photoData.csv`, `TOCData.csv` and others), ready to copy into `../AiRT2/public/15/indexes/`                  |
| `MISSION_DATA/A15_*.tsv` beside each master                                              | The same rows with provenance columns: event ID, GET basis and contributing source rows                                                                                               |

`.gitignore` already excludes WAV, MP3 and ZIP files. Add no image or PDF files to the
repository.

**Raw folder** `N:\Projects\Apollo\Apollo 15 Project\` (it exists and is currently empty):

```text
original_docs/       AS15_TEC.PDF, AS15_PAO.PDF, AS15_CM.PDF, AS15_LM.PDF, a15.photidx.pdf, ap15_sim_index.pdf, ...
images/im-ldi/AS15/  processed Hasselblad PNGs for magazines 83, 94, 98
images/alsj/AS15/    ALSJ high-resolution JPGs for those magazines, where they exist
images/sim/metric/   AS15-M-nnnn_{MED,SML}.png
images/sim/pan/      AS15-P-nnnn_FULL_{MED,SML}.png
```

The extraction code reads these files through `MISSION_DATA/raw_manifest.tsv` and checks their
checksums. A missing or changed raw file stops the build.

**Hosted assets** in `N:\Projects\Apollo\media.apolloinrealtime.org\A15\` use Apollo 16's
`full`/`medium`/`thumbnail` folders for every collection:

```text
A15/images/flight/{full,medium,thumbnail}/AS15-<mag>-<frame>.jpg     4175² / 1280 / 120 px (Flickr masters; ALSJ or cropped IM-LDI for mags 83, 94, 98)
A15/images/mapping/{full,medium,thumbnail}/AS15-M-<nnnn>.jpg         4048² from _MED / 1012² from _SML / 120 px
A15/images/panoramic/{full,medium,thumbnail}/AS15-P-<nnnn>.jpg       14963×1616 from _FULL_MED / 3741×404 from _FULL_SML / 120 px wide
A15/images/supporting/{full,medium,thumbnail}/                       press, KSC and recovery photos cited by the journals
A15/MOCR_audio/<tape>_…_mp3_16/{*.mp3, audiowaveform_512/*.dat}      later, from the finished 30-track audio (A11/A13 pattern)
```

Sizes are long-side pixels. The IM-LDI Hasselblad masters are resized to 4175 px on the long
side for `full`, matching the Flickr scans. Conversion skips outputs that already exist with a
recorded source checksum. `MISSION_DATA/A15_photos.tsv` lists, for every hosted file, its media path, the source
path or URL, and the checksum.

## 4. Keeping only what was said

### What the journals insert

A quick regex census of six AFJ chapters and five ALSJ chapters found about 1,317 bracketed or
parenthesized spans within utterances, with 633 distinct texts. The classes, with examples, are:

| Class                              | AFJ `[...]`                                                                    | ALSJ `(...)`                                                           | Treatment in the published text                                                                  |
| ---------------------------------- | ------------------------------------------------------------------------------ | ---------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------ |
| Pause or no reply                  | `[Pause.]` ×152, `[Long pause.]` ×71, `[No answer.]`                           | `(Pause)` ×212, `(Long Pause)` ×56, `(Static; Long Pause)`             | Remove. Keep as an annotation, which also hints at audio gaps                                    |
| Unintelligible speech              | `[garble]` ×40                                                                 | `(Garbled)` ×25, `(Garbled under Joe)`                                 | Replace with `...`, as on the earlier missions. If audio review recovers the words, use them     |
| Audible non-speech                 | `[Laughter]`                                                                   | `(Laughing)` ×7, `(Laughs)`, `(Chuckling)`, `(Jim laughs)`             | Keep in place as an inline marker from the approved list below                                   |
| Unit conversion                    | `[58,282 km]`, `[1,113 m/s]`, `[35.9 kPa]`, `[…Fahrenheit, 18 to 21° Celsius]` | `(feet per second)`                                                    | Remove                                                                                           |
| Expansion or gloss                 | `[Antenna]`, `[FIDO]`, `[circuit breakers]`, `[nautical]`                      | `(Valve)`, `(LM)`, `(PLSS)`, `(Electro-Luminescent)`                   | Remove                                                                                           |
| Inserted word or grammar           | `[is]`, `[the]`, `[are]`, `[you should do it]`, `occasional[ly]`               | `(to)`, `(It will)`                                                    | Remove. Fragments attached to a word revert to the spoken form (`occasional[ly]` → `occasional`) |
| Stage direction or addressee       | `[correcting himself]`                                                         | `(To Jim)`, `(Responding to Joe)`, `(Reading)`, `(Before hearing Joe)` | Remove. Keep as an annotation                                                                    |
| Editorial note inside an utterance | rare                                                                           | long `( … )` notes, sometimes containing links                         | Move to commentary at the same GET                                                               |
| Literal notation that is spoken    | —                                                                              | `CB(16)`, `CB(11)` with no space before `(`                            | Keep                                                                                             |

Square brackets in the AFJ and parentheses in the ALSJ are editorial by default. The literal
notation is the exception and must be matched before the removal rules run.

**A square bracket is never in the audio** (2026-11-24). Every `[...]` span is an editor's
addition and is decided without review (`md_spans.square_bracket`): garble becomes `...`, a
non-speech marker becomes the approved marker, a pause, direction or note keeps its annotation,
and everything else is removed, even where the PDF prints the same words (mostly `[sic]`). The
SG-reel ASR confirms it (`research/mission_timing/journal_bracket_audio_check.py`, closed gaps
around 8,000 timing anchors): 0.7% of 678 AFJ `[...]` spans are heard, against 85% of short
spoken spans judged the same way. The few hits are function words (`[the]`, `[Do]`) that
Whisper tends to supply. ALSJ parentheses test the same way (1.1% of 1,067 heard) but stay with
the rules and the review queue.

**Garble and non-speech follow Apollo 11, 13 and 17.** Garble is published as `...`. Audible
non-speech stays inline, in parentheses and capitalized, at the point where it occurs:
`That's okay (Laughter).` or `(Laughter) I know it.` The published Apollo 17 master shows the
vocabulary in use:

| Marker        | Count |
| ------------- | ----: |
| `(Laughter)`  |   593 |
| `(Humming)`   |    45 |
| `(Chuckle)`   |    37 |
| `(Singing)`   |    31 |
| `(Cough)`     |    18 |
| `(Music)`     |    12 |
| `(Applause)`  |     6 |
| `(Whistling)` |     3 |

Apollo 11 and Apollo 13 use `(Laughter)` almost exclusively. Rules for
Apollo 15:

- **Approved list.** Only these markers may appear in published utterances.
- **Normalization.** Journal and transcript variants map onto the list: `[Laughter]`,
  `(laughter)`, `(Laughing)`, `(Laughs)`, `(laugh)`, `(Jim laughs)` and the Apollo 11 typo
  `(Laugher)` become `(Laughter)`; `(Chuckling)` becomes `(Chuckle)`; `(Coughing)` becomes
  `(Cough)`.
- **Attribution.** A marker naming another person, such as `(Jim laughs)`, keeps the marker on
  the utterance where it is heard. The speaker detail goes to an annotation.
- **Audio check.** A marker that the audio does not support is removed, like any other
  editorial span.

### How each span is decided

Three independent methods judge every span: pattern rules, alignment with the original NASA
transcript, and an AI classifier. Each method runs on its own, and they are combined in step 5.
The AI labels and aligns text, and in the wording review (§5) it picks one of the existing
readings. It never writes the published wording, so it cannot introduce words that are not in a
source.

1. **Parse each span with its class.** The journal extractor emits every utterance as an ordered
   list of `spoken` and `editorial` spans, never as cleaned text. Each editorial span carries its
   class, its exact source characters and a stable key: source, chapter, anchor, span index and a
   hash of the span text.
2. **Pattern rules.** Pattern tables cover the classes above: pause and no-answer phrases,
   garble variants, unit symbols and numbers with units, a glossary of known abbreviations,
   single function words, leading `To `/`Responding`/`Reading`, and the `CB(nn)` exception.
   Each span gets a rule label, or `none` when no rule or several rules match.
3. **Alignment with the original transcript.** Align the journal tokens to the printed TEC, CM
   or LM tokens for the same event (§5). Ignore case, punctuation, number formatting and
   `(garble)`. This yields one of three judgements:
   - **Insertion.** A bracketed span with no counterpart in the 1971 transcript is likely an
     insertion.
   - **Restoration.** A bracketed span that matches printed words may restore speech the editor
     marked as uncertain. Flag it.
   - **Unbracketed change.** Text outside brackets that differs from the printed transcript is
     either a journal restoration (often where 1971 printed `(garble)`) or an unbracketed edit.
     Alignment is the only method that sees these, so they go to the AI and review queue rather
     than being silently accepted.
4. **AI classification.** Send each utterance to a language model with its spans numbered, the
   aligned printed transcript row, the neighbouring utterances and the class table above. Ask
   for structured output only: for each span ID, a class, an action and a confidence. Ask for
   the same for any unbracketed token ranges that alignment flagged.
   - **Output check.** Code applies the actions and verifies that the result is a subsequence of
     the source tokens, apart from the garble marker. A response that adds, reorders or
     rewrites words is rejected.
   - **Model: HGX, as in `../artemisinrealtime`.** Use the OpenAI-compatible HGX endpoint with
     model alias `LLM-Alpha-512K`, which is GLM-5.3-Flash. It is fast and free for this
     project. Reuse the Artemis transport from
     `src/server-batch/1_comm/transcript_common.py` and `history_common.py`:
     - `POST {HGX_URL}/chat/completions` with streaming, `reasoning_effort: low` and temperature
       0.1.
     - `HGX_URL` and `HGX_API_KEY` come from `.env`, which `.gitignore` already excludes. Add
       both to a committed `.env.example`.
     - A request is valid only if it finishes with `finish_reason: stop`. Each invocation has a
       request budget.
     - Validated rows are checkpointed atomically, so an interrupted run resumes without
       repeating requests.
     - Diagnostics keep the system prompt, the user prompt and the response.
   - **Schema and batching.** Use a strict `json_schema` response format. HGX supports flat
     schemas, as in Artemis `single_classification.response_format`. Each row holds the span
     index, class enum, action enum and confidence. The array length must equal the number of
     spans sent, and any cross-field rules are checked locally. Send a window of utterances per
     request, as Artemis sends about 50 rows, with neighbouring utterances as context-only rows.
     Rejected or invalid rows are retried alone and stay explicit if still unresolved.
   - **Alternative backend: local Strata running Qwen.** Choose the backend on the command line
     with `--backend hgx` (the default) or `--backend strata`. There is no automatic
     switching. If the selected backend is unreachable or its settings are missing, the run stops
     and keeps its checkpoints, and you rerun with the other switch. Strata is an
     OpenAI-compatible server on this machine, configured as in
     `../artemisinrealtime/.env.example`:
     - `STRATA_URL=http://127.0.0.1:47831/v1`
     - `STRATA_MODEL=iq3_s` (Qwen3.8-Flash-Next IQ3_S, as listed by `/v1/models`)
     - `STRATA_API_KEY`, which may be empty

     The request goes to the same `chat/completions` call with the same prompt and schema.
     - **Startup check.** Probe the selected backend's `/models` before the first request and
       stop with a clear message if it does not answer.
     - **Provenance and caching.** Every label records its backend and model. The cache key
       includes both, so a Strata label is never reused as an HGX label, or the reverse.
     - **Schema support.** Check whether Strata accepts strict `json_schema`. If it accepts
       only `json_object`, send the schema in the prompt. The local validation is the same
       either way.
     - **Accuracy.** Measure Strata separately on the calibration corpus. Its automatic-apply
       thresholds may need to be stricter than HGX's. Where HGX and Strata labels for the same
       span disagree, treat that as a disagreement and send the span to review.

     Artemis's transcript stages accept only `--backend hgx`. Adding the `strata` choice is new
     code here.

   - **Provenance.** Record the model name and revision, the prompt hash and the response with
     each label. Cache responses by span key, model and prompt, so reruns are free and exactly
     reproducible.
   - **Utterance matching.** The same model can help where GET and text similarity do not
     settle which journal utterance corresponds to which printed row. Examples are split or
     merged transmissions, reordered overlapping calls and shifted stamps. It proposes links by
     row ID. The code checks them for ordering and time tolerance.

5. **Combine the judgements.** When the rules, the alignment and the AI agree, the action is
   applied automatically. Any disagreement, any low AI confidence and any flagged restoration
   goes to the review queue. Once stage 06 has run, the ASR words are a fourth witness, but
   never the authority (§6 step 7). A span that all text methods would remove but the ASR
   contains goes to the review queue, where a reviewer decides by listening.
6. **Record each decision once.** Store decisions in `MISSION_DATA/review/editorial_spans.tsv`,
   keyed by the stable span key. Columns: class, action (`remove`, `garble`, `annotate`,
   `to_commentary`, `keep`), reviewer and date, and a note. On a re-scrape, unchanged spans reuse
   their decisions. Only new or edited spans return to the queue. A review report lists the
   queued spans with neighbouring text and the corresponding printed transcript row, so they can
   be decided in a spreadsheet. Each row also records which methods agreed (`rule`, `pdf`, `ai`,
   `asr`, `human`).
7. **Publish from the spans.** The master `A15_utterances.csv` is assembled from spoken spans,
   `...` garble markers and approved non-speech markers only. A release check fails on any of
   these in an utterance:
   - a `[` or `]`
   - a parenthesis other than an approved non-speech marker or recorded literal notation such as
     `CB(16)`
   - a unit pattern such as `\d[\d,.]* ?(km|m/s|kg|kPa|°C|°F)`
     Commentary keeps its editorial text and links.

### Calibrating against the earlier missions

The manual cleanups for Apollo 11, 13 and 17 are a test corpus. Run the span extractor and rules
over the cached Apollo 13 AFJ pages (`Apollo_13/ap13fj/`) and the Apollo 16 and 17 ALSJ caches.
Compare the output with the corresponding published lines in `A13_utterances.csv` and
`A17 master TEC and PAO utterances.csv`. Where the hand-edited files dropped a span, the rules
should drop it. Spans the hand edits missed, such as the Apollo 13 `[… km]` conversions, show the
rules catching what manual review let through. Keep this comparison as a regression test.

Use the same corpus to measure the AI before trusting it on Apollo 15. For each class, report
the precision and recall of the rules, the alignment, the AI and the combined vote. This is
feasible because the earlier missions have both journal text and original transcripts. The
threshold for automatic application is set from these measurements, separately for each class.
For example, AI-only removals of inserted words may need human review even if unit conversions do
not. Repeat the measurement when the model or prompt changes.

## 5. Ingestion steps

Each step reads its predecessors' outputs. Rerunning a step on unchanged inputs must produce
byte-identical files, so Git diffs stay reviewable.

1. **Snapshot the sources.** Download the AFJ and ALSJ indexes, deriving chapter lists from their
   links. Download every chapter into `MISSION_DATA/journals/`, update its manifest, and commit
   each snapshot. A later fetch updates the files, and Git keeps the history of journal edits.
   Download the transcript PDFs and photo indexes into the raw folder's `original_docs/` and
   record them in `MISSION_DATA/raw_manifest.tsv`. A changed upstream PDF is saved alongside the
   old one under a dated name, never over it. Throttle requests.
2. **Extract the original transcripts.**
   - TEC: run the existing extractor over all 1,478 pages. First, change it to detect the
     `cc_lm`/`cc_cm` link context from each page rather than seeding it. Keep the existing
     regression and shared-stamp flags. Add flags for unreadable stamps and untimed continuation
     lines.
   - PAO, CM and LM: check each text layer on sample pages first. Where a layer fails, use the
     LSC OCR tools described in [OCR with the LSC tools](#ocr-with-the-lsc-tools). Keep every
     raw reading separate from its correction.
   - Treat the Apollo 17 `cleanTECcsv.py` digit-confusion substitutions and `MCC`→`CC` as flags,
     not silent rewrites.
3. **Extract the journals** into ordered blocks (AFJ `cc`, `onboard`, `pao`, `comment`, photo,
   video, audio; ALSJ utterances and bracketed commentary). Use an HTML parser. Keep UTF-8 with
   NFC normalization, inner HTML and media links resolved to absolute URLs. Split utterances into
   spoken and editorial spans as in §4. Attribute crew review quotes (`Scott, from the 1996
review:`) to the crew member, with the review as the source.
4. **Build the speaker map.** Count speaker labels across every source and map each one
   explicitly. Scott is CDR, Irwin LMP and Worden CMP. Map the CapComs, Goddard, PAO, Unknown and
   recovery forces as well. Stop on any unmapped label.
5. **Reconcile the witnesses into utterance events.** The printed TEC, CM and LM rows form the
   event skeleton, with PAO for commentary-only passages. Link journal utterances by GET within
   a tolerance and by ordered text similarity, using only the spoken spans. An event with an
   existing `event_id` in the timing catalog keeps it. Rows left unmatched or ambiguous go to
   the AI matching pass in §4 step 4. Its proposals are checked for order and time tolerance
   before they are used.
   - **Utterance breaks differ.** The journals, the PDFs and the ASR segment speech differently.
     A journal row often joins two printed rows of one speaker or splits one in two. After the
     one-to-one chain, a link grows over unlinked neighbouring rows of the same speaker (within
     90 s, skipping at most two interleaved rows of another conversation) while the joined text
     is at least 0.05 more similar and 60% of the added row's words occur in order on the other
     side (`group_printed`, `group_journal`). Two neighbouring links whose words cross the break
     join when the joined text is 0.1 more similar (`group_shifted`). Linked rows sharing a
     journal row form a group (`group_id`, its first printed event). The wording review compares
     whole groups (`group_events`), and only the group's own one-to-one link sets a journal GET.
     Onboard (CM/LM data-storage) and air-to-ground rows never join: the onboard audio is not in
     the TEC or on the SG reels, so the AFJ's onboard rows stay onboard. The review UI shows each
     group's texts combined and says how many rows it joins (`joined`). It offers no SG excerpt
     or ASR words for an onboard row.
   - **Non-speech and editors' marks in the PDF.** Printed `(laughter)`, `(Cough)` and `[sic]`
     are not compared as words, as the journals' bracketed ones are not. Spoken literal notation
     such as `CB(16)` is compared whole on both sides.
   - **PAO announcements span tape segments.** A PAO row may continue on the next segment, which
     repeats the last words of the one before. PAO links grow like the others (900 s, as PAO
     times are segment headers), and the repeat is dropped once when the rows are joined.
   - **PAO rows with inline speaker labels.** Where the text layer lost the speaker column, a row
     holds several turns (`... Over. SC (garble) PAO This is Apollo Control ...`). A label at the
     row start or after a sentence end splits it. Each PAO turn becomes its own row at the
     header time (`<event_id>-k<n>`); other speakers' turns, which are in the TEC, end the
     announcement and are not joined to it. `CAPCOM for the maroon team` and
     `CAPCOM Karl Henize has` remain text.
   - **Words moved across a break.** A reading may open with two or more words that the other
     witness ends its previous row with, or close with words that it opens its next row with.
     These words are not lost by either choice. They are compared without those words. When
     nothing else differs, the row is `break_shift` and the journal is accepted. The queue lists
     the moved words (`moved`) and the neighbouring printed and journal rows (`context_before`,
     `context_after`) for every row.
   - **Page headers read into a row** (`15 AIR-TO-GROUND VOICE TRANSCRIPTION`, `BEGIN LUNAR
     REV 63`, `END OF TAPE`) are dropped from the last row of a TEC, CM or LM page. Line-end
     hyphens in the PDFs (`measure- ment`) are joined before comparison.
   - **Misread stamps.** A printed row whose stamp is out of order with both print neighbours
     (within 120 s; `00 00 01 0B` read as :08 for :03) sorts at its predecessor for linking, so
     the misread does not swap two rows and break the chain.
   - **Rows at the top of a page.** Many TEC, CM and LM pages print their first row above the
     old y 109 body line (changed 2026-11-24). Such a line is a row when its stamp is readable
     (`r00`, `r00b`, so the pilot IDs below keep their numbers); its wrapped lines follow it. A
     lower-case text-column line from y 80 down, before the first row, continues the previous page.
   - **The journal readings shown for review drop every square bracket** (§4; `[garble]` shows
     as `...`) and unit conversions in any bracket (`(psi, 276 kPa)`). Other parentheses stay.
   For each event or group, align the printed tokens,
   the journal's spoken tokens and, later, the ASR words. Agreement becomes the proposed wording.
   Where they disagree, the journal reading is proposed (`proposed` column): the AFJ and ALSJ are
   curated retypings of the 1971 PDFs. Where the two journals differ, the one agreeing with the
   printed row is proposed, otherwise none. The ASR is never a wording source. A journal reading
   whose only differences supply words where the PDF has garble is accepted without review
   (`garble_filled`). Garble is `...` in the TEC, CM and LM (their legends; `***` is VOX clipping)
   and `(garble)` in the PAO edition. Readings with the same spoken words (`Rog`/`Roger`,
   `Yeah`/`Yes`, `one`/`1`, fillers, a trailing OCR `l` for `!`) are `spelling`, and the journal
   is accepted. Other disagreements become rows in `review/wording.tsv` with every reading and an audio locator.
   - **AI judgement** (`reconcile --ai`, `md_wording_ai.py`, HGX by default). The model reads
     each remaining row with its neighbouring rows and the differing word runs. It picks
     `printed`, `afj`, `alsj` or `listen`, giving a basis (`terminology`, `ocr_error`,
     `context`, `editor_tidying`, `utterance_break`, `audible_only`), a confidence and a short
     reason. A pick of a reading the row lacks is rejected.
   - **When a pick decides a row.** A pick with confidence of at least 0.8 decides the row
     (status `ai`) unless it would leave out three or more words that no neighbouring row holds.
     Such a row stays in review with the pick shown.
   - **Caching.** Judgements are cached by wording key, model and prompt hash in
     `.local/mission_data/ai/wording_<backend>.jsonl`. They are never written to
     `MISSION_DATA/review`. The UI shows every judgement and filters the AI-decided rows.
   The reviewer's choice, made by listening, overrides the AI. It is recorded there and reused on later builds.
   - **No row waits for review.** The wording is checked later, by listening through the whole
     mission. Each row's `resolved` column names the reading used, in this order: a human
     choice, a confident AI pick, or the proposed journal of an automatic kind. Any other row
     is `provisional`. It takes the AI pick, the proposed reading or the printed row, whichever
     comes first without losing words that no neighbouring row holds. If every reading loses
     words, it takes the one losing the fewest. Its `verify` column names the other readings
     and the reason, for that pass.
   The review UI (`sg_transcript.py review-ui`, `md_review_ui.py`) serves this queue, the
   editorial queue and the PAO word queue on 127.0.0.1 with the SG-reel excerpt of each row.
   It places a row by its own timing anchor, by the nearest anchor on a reel, or for PAO slots by
   the slot's context in the reel words. Saves go through each stage's recorder. The
   journal often recovers words the 1971 transcript marked `(garble)`. The printed transcript
   often has whole exchanges the journals omit. Both directions fill gaps.
6. **Merge the commentary.** Sort AFJ comments, ALSJ commentary, editorial notes moved out of
   utterances, and PAO announcements missing from the technical record by GET. Break ties by
   source, then document order (as `merge_scraped_commentary.py` did). Keep "comm break" notes as
   a kind, not as deleted text.
7. **Index the photographs.**
   - Hasselblad: inventory the Flickr folder with ID, magazine, frame, variant, size, pixel
     dimensions and SHA-256, using a cached hash file like `mission_sources.HashCache`. Join
     journal citations, the AFJ image index and the NASA photo index. Assign GET from a citation
     or by interpolation between cited frames of the same magazine, and record which.
   - Metric: read every IM-LDI view page. Record revolution, UTC start, coordinates, altitude,
     sun elevation, features and film. Convert UTC to GET.
   - Panoramic: read the view pages for revolution, tilt and features. Take times from
     `ap15_sim_index.pdf`.
   - Record manual placements in `review/photo_times.tsv`.
8. **Convert for hosting.** Scrape the IM-LDI gallery records and download the magazine 83, 94
   and 98 masters and the SIM-bay PNGs into the raw folder (§1). Then write the §3 media tree
   from the Flickr masters and those downloads. Skip outputs whose recorded source checksum is unchanged. Record each media path in the
   photo index.
9. **Build the table of contents.** Now `flight_plan.py toc`, run after this pipeline
   ([flight-plan guide](17_2026-11-23_flight_plan_ingestion.md) §6). Seed it from AFJ and ALSJ chapter titles and their first GET,
   with level-2 mission events from `a15summary.html` and the press-kit timeline. Then review it
   by hand, as for `A13_TOC.csv`.
10. **Validate.** The flight-plan and TOC checks are `flight_plan.py validate`. Port `errorcheckUtterancesDatafile.py` and `errorcheckPhotosDatafile.py` as
    tests. Check monotonic GET within each source, malformed stamps, runs of identical stamps
    (the A17 missing-audio check), unmapped speakers, the §4 bracket and unit release check,
    photo IDs without hosted files, and hosted files never referenced. Also check every hosted
    IM-LDI-derived image. It must have a recorded crop and tone setting. Its edges must contain
    no film-base border or sprocket holes, detected as a dark band or a regular hole pattern
    along any side.
11. **Write the master files and AiRT2 indexes** (§3). This is not part of `sg_transcript.py`.
    It belongs to `web_publish`, a separate, not yet designed step that packages the output of
    every stage for the web. Requirements so far: apply display conventions only there:
    the garble marker, link targets and `HHHMMSS` IDs. Check the `indexes/` files against the
    AiRT2 loaders' schemas for the existing missions before copying them into
    `../AiRT2/public/15/indexes/`.

### OCR with the LSC tools

Run OCR only where the embedded text layer is measurably not good enough. **OXR is the OCR
engine.** It is the newest approach in the `../lsc` (Lunar Sample Compendium) repository, which
at revision `77bdc8c` replaced its earlier Marker and Chandra table-repair path with local OXR.
OXR has not yet been tested on typewritten transcripts.

**Why OXR rather than Chandra OCR 2** (checked 2026-11-23):

- **Newer.** OXR-1.0 was published in October 2026 (Hugging Face model created 2026-10-02,
  GitHub repository 2026-10-05). Chandra OCR 2 was released in March 2026.
- **Better on the published benchmarks.**
  - [OXR's benchmark report](https://github.com/SoMarkAI/OXR/blob/main/docs/benchmark.md) ranks
    it first on olmOCR-bench, at 87.84 overall against Chandra OCR 2's 85.80.
  - It is also first on OmniDocBench and DocParsingBench, where Chandra OCR 2 places tenth
    (82.20 against 87.76).
  - The olmOCR-bench category closest to these 1971 typewritten scans is **Old Scans**, where
    OXR scores 59.32 against Chandra OCR 2's 51.10, the score that
    [Chandra's README](https://github.com/datalab-to/chandra) reports itself.
- **Caveats.**
  - These are vendor-reported numbers. OXR's report takes other models' scores from their own
    papers, model cards or the public leaderboards rather than rerunning them.
  - Chandra OCR 2 still scores higher on Long Tiny Text (93.7 against 90.95) and Base (99.9
    against 97.35).
  - Every model scores below 60% on Old Scans, so the scored sample below decides acceptance,
    not the benchmark.

**1. Decide per page whether OCR is needed.** Hand-key a scored sample from each edition. Cover
clean, faded, skewed and continuation pages, and include the pilot's known failures:

- PDF page 849, where the text layer reads `06 03 22 41` and the scan prints `06 03 12 41`.
- PDF page 787, where it reads `O1` and "Till".
- PDF page 873, where the printed page number is misread.

Score exact stamp digits, speaker, row association and dropped or invented rows. Then flag pages
for OCR automatically:

- **Stamp checks:** stamps that fail to parse, step backwards, or jump more than the page's
  local pacing allows.
- **Column structure:** a missing stamp or speaker column, and rows without a speaker.
- **Text quality:** a low dictionary hit rate.
- **Missing layer:** pages with no text layer.

Take a random sample of unflagged pages as well, to catch plausible but wrong digits.

**2. OCR with OXR.** OXR-1.0 is a local layout-and-recognition service.

- **Setup:** set it up with `../lsc/scripts/setup_oxr.ps1`. This uses Docker Desktop/WSL2,
  pinned OXR commit `c1a1dcb` and pinned weights `SoMarkAI/OXR-1.0@c708407`. The service is
  bound to `127.0.0.1:8010`.
- **Use the page-level client.** For transcripts, use the page-level pattern of
  `3_convert_pdfs_oxr.py` rather than whole-document upload. That client renders each selected
  page with pypdfium2 at a chosen DPI (72–300, default 180) and posts it to `/v1/parse/sync`.
  Whole-document upload renders internally at a fixed 150 DPI, which may be too coarse for faded
  typewritten digits. Compare 180 and 300 DPI on the scored sample.
- **Output:** the layout JSON gives block boxes and reading order. Map the stamp, speaker and
  text columns from block x-positions, as the existing extractor does with word positions.

- **Difficult rows:** rerun OXR on a tighter crop of the row at 300 DPI. The crop comes from the
  scan alone, and the text-layer reading is never part of the input.

**3. Combining the readings.** Each OCR'd page or row has up to three independent readings:

- the embedded text layer
- the existing [Internet Archive OCR](https://archive.org/download/Apollo15/AS15_TEC_djvu.txt)
- OXR

Combine them as follows:

- Agreement on stamp digits and speaker is accepted.
- Disagreement goes to the review queue with the row crop.
- Every timing anchor is still checked against the printed row, even when all readings agree.
- Chandra OCR 2 is not part of the pipeline. Add it only if the scored sample shows a class of
  rows that OXR misreads and Chandra reads correctly, such as long runs of tiny text. If it is
  added, follow LSC `2_prepare_table_repairs.py` (LM Studio, temperature 0, reasoning off,
  scan crop only).
- Store each reading with its engine, revision, DPI, page and box, and record the reviewer's
  choice separately. A source checksum and settings key lets reruns skip completed pages.

**4. Running the batch.** Adopt the LSC run discipline:

- **Resuming:** skip a page only when its source hash, settings and all outputs match. Retry
  transient HTTP errors up to three times.
- **Stopping:** stop when the service is unavailable, on a configuration error, or after three
  consecutive failed pages. An isolated failed page is recorded and the run continues.
- **Summary:** write a `run-summary.json` with page completion counts, and exit nonzero while
  pages remain incomplete.
- **Review:** keep review material as TSV and image crops, not HTML pages.
- **Scheduling:** OXR takes about 60% of the 4090's memory, and local Strata may also need the
  GPU. Schedule OCR outside stage 06 transcription runs, and do not run more than one
  GPU model at a time.
- **Licence:** OXR's custom licence adds commercial-use thresholds and attribution terms to
  Apache 2.0. Confirm that the terms suit the website's use, and record the engine and its
  licence in the source manifest.

## 6. Relationship to the retiming work

- The TEC index from step 2 and the event IDs from step 5 are the same records that
  [mission timing](../archive/docs_retired_20261126/14_2026-11-22_mission_timing_plan.md#build-a-searchable-transcript-index) uses.
  Keep the pilot files `research/mission_timing/catalog/pao_572aaa_*` and `research/mission_timing/catalog/spur_exchange.*` unchanged. Map their
  event IDs into the utterance events rather than renumbering them.
- Journal GETs are editorially corrected and sometimes adjusted by listening. They help
  identification and are not independent of the printed GET. Never pool them as separate timing
  observations.
- The stage 06 transcript is the audio witness for §4 and §5. After the mission-time fits are
  accepted, its word timings can also refine utterance onsets. Store those as a separate
  `measured` GET basis tied to the map revision used, and leave the printed and journal readings
  unchanged.

### Space-to-ground reels

**Status: plan (2026-11-23).** The space-to-ground (SG) reels are a second audio source next to
the 30-track.

**The reels.** `N:\Projects\Apollo\Mission Data\Apollo 15\Audio\Internet Archive` holds 45 WAVs
(128 GB). Forty-four are Apollo 15: `540-AAA` through `583-AAA`. `550-AAG` is the Apollo 11 landing
and is excluded.

- **Contents.** Each reel is a continuous space-to-ground recording. Its air-to-ground speech
  corresponds to the TEC, and its PAO announcements are in the PAO edition.
- **Index.** `N:\Projects\Apollo\Mission Data\NASA-Audio-Archive_Digital-Audio-File_Metadata.pdf`
  (PDF pages 21–22) gives each reel's date, a number range (for example 427–445 for 572-AAA) and,
  for some reels, the MET at start and end. Many MET fields are `000:00:00`, and the catalogue
  warns of start and end errors. The index only locates a reel approximately. It is not a timing
  reference.
- **Locating a reel.** The number ranges match the PAO edition's page-header segment numbers. For
  example, segment 41 begins at 4:11 GET, where 541-AAA begins. The PAO headers can therefore
  bound which part of the TEC to search.

**The timing comes from the TEC and the journals.** The PAO edition is used to recognize the PAO
interruptions and to give those passages their own text. PAO speech is never an anchor, and the
minute-resolution PAO header times are never fitted.

**Process.**

1. **Reel catalogue.** Record each reel in `MISSION_DATA/sg_reels.tsv`, which is committed. Each
   row holds:
   - the file, SHA-256, duration, rate and channels
   - the index's number range and MET
   - the expected GET span, bounded by the PAO segment headers

   Add the index PDF to `raw_manifest.tsv` by its external path.

2. **Transcribe** with a dedicated SG script in `scripts/sg-audio/`. Stage 07 is reserved for the
   30-track, so the SG work does not extend it.
   - `scripts/sg-audio/01_sg-internetarchive/sg_transcribe.py` uses the shared recognizer in `scripts/00_common/`.
   - It has its own transcript store and lock under `F:\Apollo_15_data\processing\sg`, outside the 30-track timing root. Each reel runs on
     its own source clock.
   - It may run alongside 30-track transcription while the GPU has headroom. It runs at lower
     priority and stops if 30-track throughput drops noticeably.
3. **Separate the PAO interruptions.** Match the ASR text to the PAO edition rows within the
   reel's span. Mark those source intervals as PAO and keep them out of the timing fit.
4. **Find anchors in the air-to-ground speech.** Match ASR segments to TEC rows and to the journal
   utterances linked to them.
   - Use the ordered text-and-time linker of step 5, against reel source seconds.
   - First find a coarse offset by letting distinctive matches vote, then narrow the window.
   - Anchors follow the timing plan: ungated recognition, an acoustically verified onset, and no
     collapsed or uncertain words.
   - For an event, the printed TEC GET and the journal GET are not independent observations. The
     journal GET can resolve an unreadable or mistyped printed stamp, and the choice is recorded.
5. **Fit one slow drift curve per reel** with `scripts/sg-audio/03_sg-internetarchive-respeed/sg_drift_fit.py`
   (`uv run --with matplotlib python scripts/run.py sg_drift_fit.py`). It is kept apart from
   stage 07. The transcript GETs are the reference; the ASR onsets (CTC-aligned word starts since
   2026-11-25) only measure the reel clock.
   - **Model.** `GET(t) = t + D(t) + c_k`. `D` is one polynomial across the whole file, with
     D(0) = 0: a line, or a parabola when blocked 5-fold cross-validation cuts the held-out error
     by at least 10% and 0.05 s. A curve whose rate changes by more than 6,000 ppm across the
     file is not used. The rate `1 + D'(t)` therefore changes only gradually, never from anchor
     to anchor. A line is a constant speed factor for that file. It is kept where the anchors
     show no change of rate, because adding a bend there predicts the held-out anchors worse.
     The respeed applies `D` alone.
   - **Segments.** `c_k` is one constant per continuous segment. Segments split only where the
     offset steps by more than 20 s between neighbouring anchors (the recorder stopped) and at
     the known edits in `catalog/sg_drift/known_edits.tsv` (the 4.279 s repeat on 572-AAA).
     A segment entered by a *backward* step of more than 20 s with at most four anchors is a
     replay or a wrong match, since a recorder stop only moves GET forward. Its anchors are dropped
     and listed (`replay_anchors_dropped_s`; three on 561-AAA).
     The constants place the respeeded audio on GET without bending `D`.
   - **Anchors.** `ok` anchors have weight 1 and `flagged` ones 0.5, in a Huber fit. A reel
     whose slope standard error exceeds 300 ppm takes the median rate of the well-measured
     reels. A reel without anchors (567, 568-AAA) gets that rate and no GET placement.
   - **Residual steps are reported, not fitted.** Steps of 3–4 s over runs of minutes appear in
     the residuals of some reels. On 564-AAA and 577-AAA they begin and end at 1971 TEC page
     boundaries (577-AAA pages 106/35–107/11 are 3.5 s late, then return), so they are steps in
     the printed GETs, which the journals copied. They are listed per reel
     (`residual_steps_s`) as leads for transcript timing, and split segments only with
     `--apply-steps`.
   - **Outputs.** `catalog/sg_drift/sg_drift_maps.json` (model, segments, errors, input
     checksums) and `sg_drift_summary.tsv`. Every anchor's fitted and held-out residual is in
     `.local/mission_data/sg/sg_drift_residuals.tsv`, and the plots are in `reports/sg_drift/`.
   - **Result (2026-11-24).** The median rate error of the 42 well-measured reels is 5,225 ppm,
     so the transfers run about 0.5% fast. 22 reels take a parabola and 20 a line. All 22
     parabolas speed up through the reel, by 800–5,700 ppm, typically from about 4,000 to
     7,000 ppm. A pooled curvature on the line reels predicted worse on 11 of 20, so those
     reels keep a constant rate. The held-out mean error is 0.4–0.9 s on 31 reels, and
     1.1–2.9 s on the lunar-surface reels with the transcript steps.
   - **Refit with aligned word times (2026-11-25, `sg-drift-v2`).**
     - *Anchors.* The anchors were rebuilt from the aligned IA transcripts. There are 5,593
       `ok` anchors where there were 3,117, and the rejections fall from 2,306 to 204. The fit uses
       7,499 anchors in all.
     - *Unchanged.* The residual to the local offset barely moves (median 0.38 s), because the
       whole-second printed GETs limit it. The median held-out error is 0.60 s, as before. The
       mission rate is 5,262 ppm. 23 reels take a parabola.
     - *Segmentation.* 546-AAA gains an early segment that now agrees with its PAO headers: the
       file starts at GET 30:24, where the old fit put 30:59. 547-AAA loses its spurious split.
     - *Checked by stage 07a.* On the IA-referenced 30-track tapes, the curve residuals and
       held-out errors of the 07a pilot are equal or smaller with the refit
       ([SG retiming](20_2026-11-25_sg_retime_30track.md#results-with-aligned-word-times-scratch-root-provisional)).
     - *Respeed.* The respeeded reels from step 6 were rendered from the old map. They are stale
       until re-rendered with `--replace`.
6. **Respeed the reels** with `scripts/sg-audio/03_sg-internetarchive-respeed/sg_respeed.py`
   (`uv run python scripts/run.py sg_respeed.py --reel all --workers 4`).
   - **Output.** Each output sample at time `u` takes the source at `t`, where `u = t + D(t)`.
     The output is in `F:\Apollo_15_data\processing\sg\respeed\<reel>.wav`, in the
     source's 24-bit PCM, 48 kHz stereo.
   - **Kernel.** The resampler is a numba Kaiser-windowed sinc with 64 taps and a 2,048-phase
     table, from one global inverse map.
     - **Accuracy.** Against exact sinusoids up to 10 kHz, the error is -108 dB. The stage 07b
       FIR with 8x oversampling and a cubic spline gives -96 dB.
     - **Speed.** It is about 70 times faster, so a 3 h reel takes about a minute and the
       44 reels take 16 minutes.
   - **Provenance.** `<reel>.json` holds the source and map checksums, the curve, each
     segment's output-time span and GET placement (GET = output seconds + constant), and the
     clipped-sample count. Overshoot on full-scale source samples is clipped: 0–3 samples per
     reel, and 177 on 544-AAA.
   - **Results.** The outputs are 24–72 s longer than the sources. 567 and 568-AAA use the
     mission rate.
   - **Reversed reels.** 553, 581 and 582-AAA were rewritten on 2026-11-23 to play forward;
     their earlier copies were reversed. The forward files are the originals for this
     pipeline. The catalogue, the ASR and the maps all use their current checksums.

   Ingestion consumes only the accepted map revisions.

7. **Use the words as a witness.** Through an accepted map, each ASR word gets a GET, and the ASR
   becomes the audio witness in §4 step 5 and §5 step 5.
   - **The human transcripts win on wording.** The journals (curated retypings of the PDFs)
     and the 1971 PDFs are the authority, the journal first. Whisper output is never published,
     and the ASR never overrules a human reading. An ASR token that disagrees with them is treated
     as a recognition error. Spelling corrections are kept. The ASR counts only when it does one
     of these:
     - confirms a human reading, or picks between OCR readings of the same typed page where
       the text layer is not a real word (an OCR misread such as `ail`/`All`)
     - shows that a bracketed span was or was not spoken (restorations)
     - shows that a printed-only or journal-only row exists in the audio
   - **PAO announcements gain a measured time (pending, after the SG audio is respeeded).** A
     mapped ASR onset gives a PAO announcement a `measured` GET basis instead of its header time.
     Until then, PAO rows keep their header times.
   - **Not an independent timing source.** The maps are fitted to TEC and journal times, so mapped
     ASR times are never pooled with them as separate timing observations.

**Execution status (2026-11-24): steps 1, 3, 4, 5 and the respeed done; PAO measured times are next.**
The matching is part of the transcript pipeline, `scripts/sg-audio/02_sg-transcript/md_sg.py`, run with
`uv run python scripts/run.py sg_transcript.py sg {reels,pao,anchors,all}` after `reconcile`.
Retiming the audio from the anchors (step 5) is separate work in `scripts/sg-audio/`. Reruns on
unchanged inputs are byte-identical (checked).

- **Reel catalogue** (`MISSION_DATA/sg_reels.tsv`, committed). It holds 44 reels with their
  checksums from the transcript store and the index PDF's segment range and MET. Its GET span
  runs from the reel's first PAO segment header to the next reel's. Misread headers are dropped
  by keeping the longest GET-increasing chain of segment headers. The index PDF is in
  `raw_manifest.tsv` by its external path (kind `sg_index`). Several spans are longer than their
  reel (540-AAA covers 5.7 h of GET in 3.2 h of audio). The reels are edited commentary rather
  than continuous recordings, so the offset jumps within a reel.
- **The PAO edition transcribes these reels.** Its segment numbers are the index's number
  ranges, and it contains the CAPCOM and crew air-to-ground as well as PAO announcements.
- **PAO readings and word correction** (`scripts/sg-audio/02_sg-transcript/md_pao.py`, run inside `sg pao`).
  - **Readings.** Each PAO-edition row has two OCR readings: the PDF text layer and the Internet
    Archive's own OCR of the same 1,238-page scan (`original_docs/internet_archive/AS15_PAO_djvu.xml`,
    in `raw_manifest.tsv` as kind `ocr_reading`). The two are aligned per page. Each
    disagreement is a slot (`scraped_data/pao_slots.tsv`).
  - **IA speaker labels.** IA reads the speaker column into the text, so those words are
    dropped from its reading first.
  - **Placing rows on reels.** Every row, not only PAO announcements, is matched to the ASR of
    its reel (3-word runs, `sg/sg_pao_rows.tsv`). This places 9,844 of the 17,092 rows, 65% of
    the rows whose reels exist. Segments from 576 onward belong to reels 584–601, which are not
    in the archive.
  - **Deciding a slot.**
    - The typed page is the authority. The ASR between the slot's aligned neighbours confirms
      the text layer when it equals it (`asr`) or is at least 0.8 similar to it and clearly
      closer (`asr_near`). It may pick the IA or OXR reading the same way only when the text
      layer is not made of known words (an OCR misread such as `a1`/`Al`). Between two
      plausible typed words it never decides (changed 2026-11-24).
    - A reading that equals the text layer confirms it (`agree`). This covers a garbled IA
      phrase that is dropped as an alignment slip while OXR matches the text layer.
    - Otherwise a journal vocabulary decides when exactly one reading consists of known words
      (`vocab`). OCR readings that differ only in spacing count as one.
    - When nothing else decides, the text layer is kept if it is made of known words (`typed`)
      or if no other reading is (`no_better`). Only an unknown text layer against known,
      unresolved alternatives goes to review (changed 2026-11-25; this cut the queue from
      1,048 slots to 25).
    - Word splits (`ye s`/`Yes`) take the joined form.
    - A replacement whose two phrases are under 0.5 similar is an alignment slip and keeps the
      text layer. So does an unwitnessed word that only one reading has.
    - The ASR never supplies wording. A correction always takes one of the two printed readings.
  - **Result.** 12,342 slots. 960 rows are corrected in
    `scraped_data/transcript_pao_corrected.tsv`. 1,150 undecided slots are queued in
    `review/pao_words.tsv`. Choices are saved to `MISSION_DATA/review/pao_word_choices.tsv` and
    reused.
  - **OXR as an optional third reading.** `flight_plan.py ocr` writes the readings, with the PAO
    pages first (`.local/mission_data/ocr/pao/oxr_180/`). When they exist, `sg pao` uses them:
    two OCR readings that agree outvote the third, before the ASR and vocabulary rules. Pages
    without OXR use the other two readings, so the transcript pipeline runs either way and is
    rerun after the OCR step.
  - **OXR beside 06 (2026-11-24).** OXR runs beside 06 with vLLM capped at 30% of VRAM
    (`scripts/flight_plan/compose.oxr-shared-gpu.yaml`). Peak use was 19.8 of 24.5 GB, at
    about 7 s per page. The OCR step's GPU guard allows a run during 06 only with that cap or
    `--force`.
- **PAO separation** (`.local/mission_data/sg/sg_pao.tsv`). An ASR segment is PAO when at least
  half its tokens fall in 3-word or longer runs shared with a corrected PAO-speaker row of the
  reel's segment range (`pao_text`). It is also PAO when it opens with "This is Apollo Control"
  or a similar phrase (`pao_phrase`). This gives 891 intervals. They are never anchors.
- **Coarse offsets** (`sg_offsets.tsv`). Distinctive word 4-grams vote for a GET-minus-reel
  offset, taken from events at most two in the reel's span ±2 h. The local mode is taken on a
  60 s grid from votes within ±10 min, keeping up to two peaks for the edits. On 572-AAA the
  result is 145:24:51, which is the pilot's offset.
- **Anchors** (`sg_anchors.tsv`, summary in `sg_summary.tsv`).
  - **Matching.** Each non-PAO ASR segment is matched (difflib, comparable tokens) against the
    air-to-ground events in its predicted GET window ±45 s. The events are TEC rows plus
    journal-only rows, with journal spoken tokens where linked. An anchor is an utterance whose
    first 3 tokens (or all of a 2-token utterance) match in order. Its onset is the Whisper
    start of the first word.
  - **GET choice.** Each row records the printed GET, the journal GET and which was used
    (`get_choice`).
  - **Judging.** Each anchor is judged against a Theil–Sen line through up to 4 clean neighbours
    on each side within ±30 min, which follows playback drift. More than 3 s off the line is an
    `outlier`.
  - **Rejected timing.** A word in a segment with `collapsed_timing` was a median 13 s off its
    neighbours, and a `long` word spanning a VAD gap 6 s. These are `rejected`.
    `zero_duration`, `clipped` and `low_word_p` words keep a good start (93–100% within 3 s, as
    for unflagged words) and are `flagged`, not rejected.
- **Result.**

  | Status | Anchors |
  | --- | ---: |
  | ok | 3,145 |
  | flagged | 2,218 |
  | rejected | 2,294 |
  | outlier | 357 |
  | isolated | 59 |

  The `ok` residuals are 0.40 s at the median, 1.17 s at p90 and 2.42 s at p99, about the
  whole-second GET quantization. On 572-AAA, 20 of the 31 hand-picked pilot onsets are usable
  anchors, at a median 0.15 s from the hand pick. Usable anchors closer than 2 min to the next
  one span 48 h of the 132 h of audio.
- **Reversed reels.** 553-AAA, 581-AAA and 582-AAA were stored reversed in the archive. The
  local copies were reversed back and resaved on 2026-11-24, then transcribed again. The masters
  on `N:` are still reversed.
  - **Choosing the transcript.** Their old transcripts stay in the store under other
    identities. `md_sg` uses the identity whose recorded size and modification time match the
    WAV.
  - **Result.** They now give 87, 143 and 89 anchors, with offsets inside their PAO-header
    spans.
- **Reels without anchors.**
  - 567-AAA and 568-AAA are crew sleep periods with PAO only.
  - 541-AAA and 544-AAA have few anchors (sparse early-cruise traffic).
- **To improve coverage:** realign the 2,294 rejected onsets, for example by CTC forced
  alignment of the collapsed segments, as the pilot's `ctc_start`.

## 7. Code location

Put the code in `scripts/sg-audio/02_sg-transcript/`, registered in `scripts/layout.json`, with tests in
`tests/`. Run it with `uv run python …`. The existing numbered audio stages (`scripts/00_common/`
through `scripts/30-track/09_irig/`) are planned to move to `scripts/30track/`. That move is separate
work: update `layout.json`, `pipeline.py`, `run.py` and the frozen-runtime path adapter
together, and keep the frozen denoise runtime's internal layout unchanged.

## 8. Decisions (2026-11-23)

1. **Hosted sizes:** use `full`/`medium`/`thumbnail` for every collection (§3).
2. **Hasselblad magazines 83, 94 and 98:** use the better-cropped ALSJ high-resolution JPGs where
   they exist. Fill the rest from the IM-LDI gallery's extreme-resolution scans, scraped from its
   embedded records and auto-cropped to the exposed frame (§1).
3. **Film:** handled separately and out of scope here.
4. **Garble and non-speech:** garble is `...`. Laughter and other audible non-speech follow the
   Apollo 11, 13 and 17 inline `(Laughter)` convention, with an approved list (§4).
5. **PDFs and other raw downloads:** keep them in `N:\Projects\Apollo\Apollo 15 Project`, not in
   Git. Keep their manifest in Git (§3).
6. **Code:** `scripts/sg-audio/02_sg-transcript/`. The numbered audio folders will move to `scripts/30track/`.
   This repository is for Apollo 15 mission data. The website is `../AiRT2` (§2, §7).
7. **HGX model:** `LLM-Alpha-512K`, which is GLM-5.3-Flash.
