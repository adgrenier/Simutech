"""The condensing run: config + Raw folder in, Summary files out.

Summary table (one Parquet file per Logger per UTC day, long format, one row
per Channel per UTC second that has at least one valid sample):

    timestamp_utc  datetime64[ns]  start of the UTC second (naive, UTC)
    channel        str             Channel name from the config
    mean, min, max float64         over valid samples, in logged units
    ac_rms         float64         RMS with the mean removed (population)
    std            float64         sample standard deviation (n - 1; NaN if n == 1)
    n_valid        int64           valid samples in the second (-7999 excluded)

Seconds with no valid sample for a Channel have no row.
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

MISSING = -7999
CARD_FOLDER = "card"  # copy of the Logger's memory card, never read
DATE_IN_NAME = re.compile(r"_(\d{4})_(\d{2})_(\d{2})(?:_|\.)")
N_META = 5  # table id, year, day-of-year, HHMM, seconds


@dataclass
class CondenseResult:
    summary_files: list[Path] = field(default_factory=list)
    skipped: list[Path] = field(default_factory=list)  # Raw files that matched no mapping


def condense(config, raw_folder, logger: str) -> CondenseResult:
    """Condense one Logger's Raw files into Summary files.

    Raw files are found in `raw_folder` (default: the Logger's `folder` setting) and
    all its subfolders (year and weekly folders), except folders named `Card`.

    `config` is a dict or the path of a JSON config file (see config.example.json).
    Re-running over the same files rewrites identical output.
    """
    if not isinstance(config, dict):
        config = json.loads(Path(config).read_text(encoding="utf-8"))
    cfg = config["loggers"][logger]
    mappings = _mappings(cfg)
    offset = pd.Timedelta(hours=cfg["clock_offset_hours"])
    pattern = cfg["file_pattern"].lower()

    result = CondenseResult()
    frames: list[list[pd.DataFrame]] = [[] for _ in mappings]
    for path in _discover(Path(raw_folder if raw_folder is not None else cfg["folder"]), pattern):
        df = _read_raw(path)
        match = None if df is None else _match(mappings, _file_date(path), df.shape[1] - N_META)
        if match is None:
            result.skipped.append(path)
            continue
        frames[match].append(df)

    summaries = []
    for (_, _, names), group in zip(mappings, frames):
        if group:
            raw = pd.concat(group, ignore_index=True)
            stamps = _second_starts(raw) - offset  # logger clock -> UTC
            summaries.append(_summarise(raw.iloc[:, N_META:].set_axis(names, axis=1), stamps))

    out_dir = Path(config["output_folder"]) / logger
    if summaries:
        summary = pd.concat(summaries, ignore_index=True)
        summary = summary.sort_values(["timestamp_utc", "channel"], kind="stable").reset_index(drop=True)
        out_dir.mkdir(parents=True, exist_ok=True)
        for day, part in summary.groupby(summary.timestamp_utc.dt.strftime("%Y-%m-%d")):
            path = out_dir / f"{logger}_{day}.parquet"
            part.reset_index(drop=True).to_parquet(path, index=False)
            result.summary_files.append(path)
    return result


def _mappings(cfg) -> list[tuple]:
    """(first date, last date, channel names) per Channel mapping; dates are inclusive, None = open."""
    if "channel_mappings" not in cfg:
        return [(None, None, [c["name"] for c in cfg["channels"]])]
    return [(pd.Timestamp(m["from"]) if m.get("from") else None,
             pd.Timestamp(m["to"]) if m.get("to") else None,
             [c["name"] for c in m["channels"]]) for m in cfg["channel_mappings"]]


def _file_date(path: Path) -> pd.Timestamp | None:
    """Logger-clock date in the file name (`..._YYYY_MM_DD_...`), if it has one."""
    m = DATE_IN_NAME.search(path.name)
    try:
        return pd.Timestamp(int(m[1]), int(m[2]), int(m[3])) if m else None
    except ValueError:
        return None


def _match(mappings, date, n_channels) -> int | None:
    """Index of the first mapping whose date range holds `date` and whose Channel count fits."""
    for i, (first, last, names) in enumerate(mappings):
        if date is None:
            in_range = first is None and last is None
        else:
            in_range = (first is None or date >= first) and (last is None or date <= last)
        if in_range and len(names) == n_channels:
            return i
    return None


def _discover(root: Path, pattern: str) -> list[Path]:
    """Files under `root` matching the Logger's name pattern; Card copies are skipped."""
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d.lower() != CARD_FOLDER)
        found += [Path(dirpath) / f for f in sorted(filenames) if fnmatch.fnmatch(f.lower(), pattern)]
    return found


def _read_raw(path: Path) -> pd.DataFrame | None:
    """Parse a headerless Raw file; None if it cannot be read as one."""
    try:
        df = pd.read_csv(path, header=None)
    except (pd.errors.EmptyDataError, pd.errors.ParserError, UnicodeDecodeError):
        return None
    return df if df.shape[1] > N_META else None


def _second_starts(raw: pd.DataFrame) -> pd.Series:
    """Start of each row's whole second, on the Logger's own clock."""
    year, doy, hhmm, sec = (raw.iloc[:, i] for i in (1, 2, 3, 4))
    whole = np.round(sec.to_numpy() * 100).astype(np.int64) // 100  # 20 ms steps, exact
    days = pd.to_timedelta(doy.to_numpy() - 1, unit="D")
    clock = pd.to_timedelta((hhmm // 100).to_numpy(), unit="h") + pd.to_timedelta(
        (hhmm % 100).to_numpy(), unit="min") + pd.to_timedelta(whole, unit="s")
    jan1 = pd.to_datetime(year.astype(str) + "-01-01")
    return pd.Series(jan1 + days + clock, index=raw.index)


def _summarise(values: pd.DataFrame, stamps: pd.Series) -> pd.DataFrame:
    parts = []
    for name in values.columns:
        x = values[name].where(values[name] != MISSING).astype(float)
        d = pd.DataFrame({"timestamp_utc": stamps, "x": x}).dropna(subset=["x"])
        g = d.groupby("timestamp_utc")["x"]
        d["dev2"] = (d["x"] - g.transform("mean")) ** 2
        s = g.agg(mean="mean", min="min", max="max", std="std", n_valid="count")
        s["ac_rms"] = np.sqrt(d.groupby("timestamp_utc")["dev2"].mean())
        s = s.reset_index()
        s.insert(1, "channel", name)
        parts.append(s)
    out = pd.concat(parts, ignore_index=True)
    out["n_valid"] = out["n_valid"].astype("int64")
    out = out[["timestamp_utc", "channel", "mean", "ac_rms", "min", "max", "std", "n_valid"]]
    return out.sort_values(["timestamp_utc", "channel"], kind="stable").reset_index(drop=True)
