# Mission audio inventory and NARA Apollo 15 recordings

`scripts/audio_inventory/` lists which NASA mission audio is held where. It reads the JSC audio-archive
metadata PDF, the Internet Archive `nasaaudiocollection` and the National Archives catalog (Record Group 255
sound recordings). It also fetches the Apollo 15 recordings that NARA has digitized.

```
uv run python scripts/run.py audio_inventory.py all        # jsc, ia, nara, join, a15, timeline
uv run python scripts/run.py audio_inventory.py download   # digitized NARA records covering SG gaps
uv run python scripts/run.py audio_inventory.py download --all   # every digitized Apollo 15 record
```

Tables and cached API responses are written to `.local/mission_data/audio_inventory/`. They are local
working files and are not committed. `--refresh` fetches the responses again. Audio is saved to
`A15_NARA_AUDIO_ROOT` (default `F:/_repos/Apollo_15_data/raw/audio/NARA/<localID>/`). Each file is checked
against the S3 `x-amz-meta-sha256` header and recorded in `a15_nara_downloads.tsv`.

## Sources and keys

- **JSC PDF** (`jsc_index.tsv`, 2,897 rows). The cells are read from word coordinates, because plain text
  extraction puts the file-name column in the wrong place.
- **Internet Archive** (`ia_files.tsv`). The script lists the original audio files of the historical
  sub-collections, items named after a tape and items that name a programme. Recent ISS material is skipped.
- **NARA** (`nara_records.tsv`). Each mission is searched with quoted phrases (`"Apollo 15"`, `"GT-4"`, ...)
  and results are sorted by naId so that pages are stable. The script uses the Catalog API v2 when
  `NARA_API_KEY` is set; otherwise it uses the catalog site's own search endpoint.
- **The Internet Archive files match the JSC numbering. NARA's numbering is separate.** NARA numbers each series
  on its own: `255-NWSa-540-AAA` is an STS-9 briefing, while JSC `540-AAA` is Apollo 15 Mission Commentary
  1–40. A NARA record therefore joins a JSC tape only when the missions also agree. Only nine records do.
  `inventory.tsv` has one row per key. `inventory_summary.md` gives the counts for each mission.

`a15_get_timeline.md` has one row per hour of GET, because the JSC and NARA tape boundaries do not line up.
Each row lists every JSC reel, NARA flight tape and GOSS reel that overlaps the hour. It also gives the share
of the hour covered by our edited SG audio (measured reel offsets) and the share we hold, including downloaded
flight tapes. Runs of hours below 25 % held are summarized as gaps at the top.

## Apollo 15 on GET (`a15_nara_get.md`)

- **Flight tapes.** "Historical Apollo 15 Flight Tape N" (`255-PAOa-608-AAA`..`655-AAA`) are consecutive
  6-hour GMT blocks from 1971-07-26 12:00, four tracks each. One track carries timecode. GET is taken from
  each tape's position in the series, because some titles are wrong (628 says tape 23). The scope note's
  GMT hour is checked against that position.
- **GOSS voice tapes.** These (`255-AVTa-15-03xxx`) give `Recorded Start/Stop Time GET DDD:HH:MM:SS`, with
  days first. When one printed time is a typo, the reel is taken as 95 minutes from whichever printed time
  continues the previous reel.
- **Coverage.** "In our SG reels" is the share of each record's range inside the nominal spans of
  `sg_reels.tsv` (GET -001:30 to 199:16). Reels 584–601-AAA, which cover the rest of the mission, are in
  neither archive.
- **Digitized at NARA.** Flight tapes 608 and 640–655, CSM Vox 602–605, and DSE 03413–03414 are digitized.
  All 74 files (26.5 GB) were downloaded and verified on 2026-11-24.
- **Not digitized.** Flight tapes 609–639 (GET 004:26–190:26) and all GOSS tapes are not digitized. After
  286:26, only GOSS 15-03184..03190 cover the mission up to splashdown at 295:11:53.
