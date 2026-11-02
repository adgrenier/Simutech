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

## Tests

```
python -m pytest
```

Tests run the condensing run end-to-end on synthetic Raw files.
