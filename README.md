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

## Tests

```
python -m pytest
```

Tests run the condensing run end-to-end on synthetic Raw files.
