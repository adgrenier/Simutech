# Simutech

Stage 1 condenses each Logger's 50 Hz Raw files into 1 s Summary files (see `GLOSSARY.md`, `docs/adr/`).

## Running the condensing run

```
pip install -e ".[test]"
python -c "from simutech import condense; r = condense('config.json', 'M:/path/to/raw', 'Tower_Bot'); print(r)"
```

Copy `config.example.json` to `config.json` and edit it: Logger folder, file name pattern,
`clock_offset_hours` (fixed, no daylight saving; -4 per ADR 0002), ordered Channel names and
units, and `output_folder` (keep it local, not on the storage server). The run writes
`<output_folder>/<Logger>/<Logger>_<UTC date>.parquet`. Raw files whose column count does not
match the Channel list are skipped and returned in `result.skipped`. The table layout is
documented in the docstring of `src/simutech/condense.py`.

Each run also writes a self-contained quality report, `<output_folder>/<Logger>/<Logger>_report.html`
(daily trends, full-second share, -7999 share, stuck and clipping counts, skipped files). Its
data is returned as `result.report` and documented in `src/simutech/report.py`. The -7999 share is
per Channel, from sample counts kept in `report_samples.json` (per Raw file and Channel, written
with each batch); it stays within 0..100 % and `counts_consistent` is false if the counts do not
cover the Summary files (for instance after deleting that file).

### All four Loggers

`config.json` holds the real settings for Hub, Nacelle, Tower_Top and Tower_Bot, derived from the
Logger programs (see its `_comment` entries for what was and was not verified). Raw files are
searched under each Logger's `folder` and all subfolders (year and `Week_NN`), skipping `Card`
folders and names that do not match `file_pattern`. A Logger can list `channel_mappings`
(`from`/`to` dates, inclusive, read from the Raw file name, plus `channels`) instead of a single
`channels` list; a file is mapped by its date and its column count, otherwise it is skipped and
listed in `result.skipped`. `raw_folder` may be `None` to use the Logger's `folder`.

### Two Raw file formats (headerless and TOA5)

From 2024-09 the Loggers write TOA5 files (four header lines: metadata, field names, units,
processing; then `TIMESTAMP`, `RECORD` and the Channels; missing values are the text `NAN`).
Each file is detected on its own (first line starts with `"TOA5"`), so both formats can sit in the
same Logger folders. A TOA5 file's Channel names and units are read from its header and must equal
a mapping in the config (and its date range); otherwise it is skipped and listed. `NAN` counts as
missing like -7999, and the sub-second `TIMESTAMP` (e.g. `18:29:56.12`) is used directly. Clock
offsets are per format: `clock_offset_hours` for headerless files (default -4, ADR 0002) and
`toa5_clock_offset_hours` for TOA5 files (default 0, i.e. UTC, ADR 0003). Duplicate seconds, the
checkpoint, bands and the report work across both formats. `file_pattern` may be a string or a list
of patterns (e.g. `tow_bot_*.csv` matches `tow_bot_data_*.csv` too).

### Data health and incremental runs

Optional data-health settings (per Logger or top level): `stuck_window_seconds` (default 5,
consecutive zero-std seconds that set `stuck`) and `clipping_threshold` (default 7900, absolute
value counted in `n_clipped`). Duplicate seconds across Raw files keep the later file's rows.

Runs are incremental and idempotent. `<output_folder>/<Logger>/checkpoint.json` records the
Raw files already condensed (name, size, modification time); later runs only process new or
changed files and merge their Summaries into the daily files, so a rerun changes nothing.
Optional settings: `start_date` (config top level, or `condense(..., start_date=...)`) ignores
earlier Raw files and seconds; per-Logger `settle_seconds` (default 0) treats a Raw file
modified more recently than that, or with an incomplete last row, as still being written
(`result.pending`, retried next run). A file that cannot be parsed goes in `result.failed`
and the run continues. `result.summary` holds the end-of-run counts. Moving `start_date`
earlier redoes the files it previously cut.

### Backlog-scale run (all Loggers)

```
python -m simutech --config config.json                 # all Loggers, window = start_date or 90 days back
python -m simutech --logger Tower_Bot --raw-folder SAMPLE_DIR --start-date 2026-03-10
```

Prints one progress line per Raw file and a summary per Logger; exits 1 if a file failed.
`--refresh-listing` re-lists the Raw folders. Listing the storage server takes minutes, so
`listing_cache_hours` (config.json: 24) keeps each Logger's file listing in
`<output_folder>/<Logger>/listing.json`; files added after it was made appear once it expires
or with `--refresh-listing`. `workers` (default 8) reads Raw files in parallel. Moving
`start_date` earlier only adds the Raw files (and first seconds) the old start date cut.

Raw files are condensed in batches (`batch_files`, default 144, per Logger or top level), so only one
batch is in memory at a time; band state is carried between batches and the output equals an
unbatched run. Every batch ends with a commit: Summary and dominant files are written through
temporary files, the band state goes to a new `bandstate-<id>.npz`, and the checkpoint, which names
that state file, is written last. A crash before the checkpoint write leaves the previous checkpoint
and band state, and the rerun gives the same output as a clean run; finished batches are kept.
A Summary second delivered again replaces only the rows of the Channels it contains. A dominant
window already written is kept if a later file only fills it partly.

### Band-limited RMS and dominant frequency

Channels may carry `"bands": [{"name": "A", "low_hz": 0.10, "high_hz": 0.30}, ...]` in the config
(see `config.json`; edges confirmed by the owner, see `docs/research/band-proposal-tower-fft.md`).
Each band adds a Summary column `band_<name>_rms`: the RMS of a continuous Butterworth band-pass
filter (state carried across seconds, Raw files and incremental runs in `bandstate-<id>.npz`, reset after
a gap or -7999 stretch longer than `band_reset_gap_seconds`, default 1) over a rolling
`band_window_seconds` window (default 10), published once per UTC second. Each Channel with bands
also gets one row per UTC-aligned 10-minute window in `dominant_<Logger>.parquet`
(`dominant_hz`, `amplitude`, searched between the Channel's lowest and highest band edge). Details
in `src/simutech/bands.py`. Needs `scipy`.

### Cross-Logger comparison page

`python -m simutech.compare config.json [Logger ...]` runs the condensing run for each Logger
(default: all in the config) and writes `<output_folder>/comparison.html`: every Logger's data
health and daily-mean trend side by side, linking to each Logger's own report. Its data is
returned by `simutech.compare.compare(...).data` and documented in `src/simutech/compare.py`.
Like `python -m simutech` it covers the last 90 days by default (`--start-date YYYY-MM-DD`, or
`start_date` in the config, to change it) and prints one progress line per Raw file.

## Tests

```
python -m pytest
```

Tests run the condensing run end-to-end on synthetic Raw files.
