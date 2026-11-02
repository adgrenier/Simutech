# Handoff: integrate stage 1 into a platform with a dashboard

For a fresh agent. Read this first, then the pointers. Written 2026-11-02.

## Goal of the next session

Take the finished **stage 1** program (50 Hz Raw files -> 1-second Summary files + quality reports) and integrate it into a platform with a dashboard. The **PI push (stage 2)** is not built; whether the dashboard reads the Summary files directly or goes through PI is a decision for the owner (see Open questions).

## What exists (do not re-derive)

- Spec: GitHub issue #1 (`adgrenier/Simutech`). Tickets #2 to #11 are closed except #10 (see below). Vocabulary: `GLOSSARY.md`. Decisions: `docs/adr/0001`, `0002`, `0003`. Research: `docs/research/vibration-1s-condensing-and-pi-ingest.md` (PI ingest options, standards at abstract level only) and `docs/research/band-proposal-tower-fft.md` (bands, with the owner's decision recorded).
- Code: `src/simutech/` (entry points `condense`, `compare`, CLI `python -m simutech`). Tests: `tests/` (60 passing). Setup, config keys and output layout: `README.md` and `config.json`.
- Branch: `integration/stage1` on `origin`. No pull request yet; `master` only has the setup commit.
- Output data contract (the thing a dashboard consumes), as built:
  - One Parquet per Logger per UTC day, long format: `timestamp_utc`, `channel`, `mean`, `ac_rms`, `min`, `max`, `std`, `n_valid`, `n_clipped`, `stuck`, plus `band_<name>_rms` columns when bands are configured.
  - `dominant_<Logger>.parquet`: one row per 10-minute window per Channel (`window_start_utc`, `channel`, `dominant_hz`, `amplitude`, `n_valid`).
  - Per-Logger HTML report and a cross-Logger `comparison.html`, built from report data returned by `condense()` / `compare()`.
  - Check the README for the current exact schema before relying on it.

## State of the data (facts learned, not in the repo docs)

- Loggers: Hub (15 channels), Tower_Top (3), Tower_Bot (2, data folder is `Tower_Bottom`), Nacelle (12 from program 009; 15 before 2021-08-24). Share: `M:\0373-03\Logger_Data` (programs in `Logger_Programs`).
- Two file formats: headerless Raw files (2020 to 2025, clock fixed UTC-4, `-7999` = missing) and TOA5 files from 2024-09 onward (5-minute files in the Logger root, `NAN` = missing, timestamps believed UTC). The UTC belief is inferred from a file's write time and is **not yet confirmed by the owner** (ADR 0003). A Nacelle 2026 TOA5 file was not found.
- Hub channels B1_Root_Flap, B1_Mid_Flap, B3_Root_Flap are always missing, B2_Mid_Flap and B3_Mid_Flap mostly missing (expect health flags, not bugs).
- Values are raw (mV/V or mV), no calibration. Bands in config: A 0.10-0.30 Hz and B 0.30-0.40 Hz on tower bend channels, 0.10-0.35 Hz on Hub Rot_Az. Rated rotor rpm range and which bend channel is fore-aft are unknown.
- The default clipping threshold (7900) is an implementer's choice, not a measured ADC limit; the programs read at +-5000 mV, so the real saturation level is probably lower. Ask the owner.
- A sample of real output (Hub, Tower_Top, Tower_Bot, 2025-08-17, a few files each) was generated for the owner; regenerate rather than rely on it.

## Not done

- **#10 (open):** the real one-week Tower_Bot review and the 90-day run were not performed. Timing seen: copying 126 files (140 MB) took 14 s, condensing them locally about 10 s. Parallel workers gave no gain on a local copy; share latency was not measured under the final code.
- **Stage 2:** the PI push. Needs PI server name, tag naming pattern, AF SDK availability on the run host, and archive coverage for backdated data (see the research note; its AVEVA claims came from search excerpts and need rechecking on the live pages).
- Scheduling of periodic runs, a Nacelle real-file check, and cross-Logger charts (the comparison page is a table).

## Gotchas

- The share is slow: listing a large Logger folder takes minutes. Never `ls` or recurse whole Logger folders; use the cached listing (`listing_cache_hours`) and known file names.
- The repo path contains spaces and sits in OneDrive; scripts that split paths on spaces break, and git worktree cleanup hit permission errors until the folders were deleted by hand.
- `compare()` and the CLI use a 90-day default window unless `start_date` is set.
- A re-run after an interrupted run is safe (checkpoint is written last), but changing band settings means reprocessing the affected days.

## Open questions for the owner (ask before building)

1. Which platform and dashboard, and does it read Parquet/files, a database, or PI?
2. Are TOA5 timestamps UTC (confirm), and is the Nacelle channel list right?
3. How often should the periodic run happen, and on which host?
4. Which Summary columns and which alarms or thresholds matter on the dashboard (none are defined)?

## Suggested skills

- `mattpocock-skills:grilling` to settle the platform and dashboard decisions above, one round at a time.
- `mattpocock-skills:to-spec`, then `mattpocock-skills:to-tickets` and `mattpocock-skills:implement-spec` for the integration work (issue tracker and labels are configured in `docs/agents/`).
- `mattpocock-skills:tdd` inside implementation tickets; `mattpocock-skills:diagnosing-bugs` for data or timestamp surprises.
- `dataviz` and `artifact-design` if the dashboard is built as a page or artifact; `anthropic-skills:nergica-branding` if the output is Nergica-branded.
- `mattpocock-skills:domain-modeling` to keep `GLOSSARY.md` and ADRs current.
